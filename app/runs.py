"""Run tracking: one record per profile / manual / timer run.

A run starts when the controller reports a process running and ends when it
stops. Every telemetry row logged during the run carries its run_id, so a run
can be charted, summarised and exported on its own. Run records are small and
are kept when old telemetry partitions are purged (the samples go, the summary stays).
"""
import logging
import threading
import time

import psycopg2.extras

log = logging.getLogger(__name__)

SCHEMA = """
ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS run_id bigint;
CREATE INDEX IF NOT EXISTS telemetry_run_idx ON telemetry (run_id, ts);

CREATE TABLE IF NOT EXISTS runs (
    id          bigserial PRIMARY KEY,
    started_at  timestamptz NOT NULL,
    ended_at    timestamptz,
    mode        text NOT NULL,              -- profile | manual | timer
    profile     smallint,                   -- profile number (profile mode)
    label       text,                       -- lot / batch / job
    notes       text,
    started_by  text,                       -- api:<key>, web:<user>, 'front panel'
    end_reason  text,                       -- completed | aborted | stopped | error | interrupted ...
    end_approx  boolean NOT NULL DEFAULT false,
    resumed     integer NOT NULL DEFAULT 0, -- monitor restarts / reconnects during the run
    stats       jsonb,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS runs_started_idx ON runs (started_at DESC);

CREATE TABLE IF NOT EXISTS app_state (     -- small persistent values, e.g. the label for the next run
    key        text PRIMARY KEY,
    value      text,
    updated_at timestamptz NOT NULL DEFAULT now()
);
"""

STATS_SQL = """
WITH t AS (
    SELECT ts, pv, sp, hl_pv, power, profile_status, alarm_bits, hl_status, sensor_break, active_segment,
           lead(ts) OVER (ORDER BY ts) AS nts,
           (profile_status IN (1, 2, 3) OR manual_status = 1 OR timer_status = 1) AS running
    FROM telemetry WHERE run_id = %s
)
SELECT count(*)                                         AS samples,
       min(pv)                                          AS pv_min,
       max(pv)                                          AS pv_max,
       avg(pv) FILTER (WHERE running)                   AS pv_avg,
       max(sp) FILTER (WHERE running)                   AS sp_max,
       max(pv - sp) FILTER (WHERE running)              AS max_over_sp,   -- SP is only meaningful while running
       max(hl_pv)                                       AS hl_pv_max,
       avg(power) FILTER (WHERE running)                AS power_avg,
       coalesce(bit_or(alarm_bits), 0)                  AS alarm_bits,
       coalesce(bool_or((hl_status & 4) <> 0), false)   AS hl_tripped,
       coalesce(bool_or(sensor_break), false)           AS sensor_break,
       max(active_segment)                              AS last_segment,
       coalesce(sum(extract(epoch FROM nts - ts)) FILTER (WHERE profile_status = 2), 0) AS held_s,
       min(ts) AS first_ts, max(ts) AS last_ts
FROM t
"""

RUN_COLS = "id, started_at, ended_at, mode, profile, label, notes, started_by, end_reason, end_approx, resumed, stats"


def run_mode(s):
    """Which kind of run the snapshot shows, or None when nothing is running."""
    mode = int(s.get("current_mode") or 0)
    if mode == 4 and s.get("profile_status") in (1, 2, 3):      # running / held / delayed
        return "profile"
    if mode == 2 and s.get("manual_status") == 1:
        return "manual"
    if mode == 3 and s.get("timer_status") == 1:
        return "timer"
    return None


def end_reason(mode, s):
    if mode == "profile":
        if int(s.get("current_mode") or 0) != 4:
            return "stopped (mode changed)"
        return {6: "completed", 5: "aborted", 4: "error", 0: "stopped"}.get(s.get("profile_status"), "stopped")
    if mode == "timer":
        return "completed" if (s.get("timer_remaining") or 0) <= 0 else "stopped"
    return "stopped"


def _clean(v):
    if isinstance(v, float):
        return round(v, 2)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    try:                       # Decimal from avg()/sum()
        return round(float(v), 2) if v is not None and not isinstance(v, (bool, int, str)) else v
    except (TypeError, ValueError):
        return v


class RunTracker:
    PENDING_TTL = 120          # s: a start command / next-run label applies to a run starting this soon

    def __init__(self, db, cfg):
        self.db, self.cfg = db, cfg
        self.lock = threading.Lock()
        self.run = None        # {"id", "mode", "profile", "started_at", "last_running_ts", "restored"}
        self._loaded = False
        self._pending_start = None     # (actor, monotonic)
        self.next_label = None         # (label, monotonic)
        self._pending_stats = None

    # ---------- hooks from the API ----------
    def note_start(self, actor, label=None):
        with self.lock:
            self._pending_start = (actor, time.monotonic())
            if label:
                self.next_label = (label, time.monotonic())
                self._save_next_label()

    def set_next_label(self, label):
        with self.lock:
            self.next_label = (label, time.monotonic()) if label else None
            self._save_next_label()

    def _save_next_label(self):
        """Persist the pending label so it survives a monitor restart."""
        try:
            label = self.next_label[0] if self.next_label else None
            self._exec("""INSERT INTO app_state (key, value) VALUES ('next_run_label', %s)
                          ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""", (label,))
        except Exception as e:
            log.warning("could not store next-run label: %s", e)

    def pending_label(self):
        """Label waiting for the next run (kept until a run starts or it is cleared)."""
        with self.lock:
            return self.next_label[0] if self.next_label else None

    def current(self):
        with self.lock:
            return dict(self.run) if self.run else None

    # ---------- called by the poller for every snapshot ----------
    def update(self, s):
        """Track run start/end. Returns the run_id to store with this snapshot (or None)."""
        if not self.db.connected:
            return self.run["id"] if self.run else None
        with self.lock:
            if not self._loaded:
                self._load_open_run()
            mode = run_mode(s)
            profile = int(s.get("selected_profile") or 0) if mode == "profile" else None
            ts = s["ts"]
            stamp = None

            if self.run and self.run.get("restored"):
                # monitor restarted (or DB came back) while a run was open
                if mode == self.run["mode"] and profile == self.run["profile"]:
                    self.run["restored"] = False
                    self._exec("UPDATE runs SET resumed = resumed + 1, updated_at = now() WHERE id = %s", (self.run["id"],))
                    log.info("Resumed run %s", self.run["id"])
                else:
                    self._close(s, "interrupted (monitor offline when it ended)", approx=True)

            if self.run and (mode != self.run["mode"] or profile != self.run["profile"]):
                stamp = self.run["id"]          # the stop/end sample belongs to the run that ended
                self._close(s, end_reason(self.run["mode"], s))

            if mode and not self.run:
                self._start(s, mode, profile)

            if self.run:
                if self.run["id"] is None:      # start insert failed earlier (DB hiccup) - retry
                    self._insert_run(self.run)
                self.run["last_running_ts"] = ts
                stamp = self.run["id"]
            return stamp

    # ---------- internals ----------
    def _exec(self, sql, args=(), fetch=False):
        with self.db._conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, args)
            return cur.fetchone() if fetch else None

    def _load_open_run(self):
        try:
            st = self._exec("SELECT value FROM app_state WHERE key = 'next_run_label'", fetch=True)
            if st and st["value"] and not self.next_label:
                self.next_label = (st["value"], time.monotonic())
            row = self._exec("SELECT id, started_at, mode, profile FROM runs WHERE ended_at IS NULL "
                             "ORDER BY started_at DESC LIMIT 1", fetch=True)
            # any older open runs are leftovers: close them at their last sample
            self._exec("""UPDATE runs r SET ended_at = coalesce((SELECT max(ts) FROM telemetry WHERE run_id = r.id), r.started_at),
                                     end_reason = 'interrupted', end_approx = true, updated_at = now()
                          WHERE ended_at IS NULL AND id <> %s""", (row["id"] if row else -1,))
        except Exception as e:
            log.warning("could not load open run: %s", e)
            return
        self._loaded = True
        if row:
            self.run = {"id": row["id"], "mode": row["mode"], "profile": row["profile"],
                        "started_at": row["started_at"], "last_running_ts": None, "restored": True}

    def _start(self, s, mode, profile):
        now = time.monotonic()
        started_by = "front panel"
        if self._pending_start and now - self._pending_start[1] < self.PENDING_TTL:
            started_by = self._pending_start[0]
        label = self.next_label[0] if self.next_label else None
        had_label = self.next_label is not None
        self._pending_start = self.next_label = None
        if had_label:
            self._save_next_label()             # consumed
        run = {"id": None, "mode": mode, "profile": profile, "started_at": s["ts"], "last_running_ts": s["ts"],
               "restored": False, "label": label, "started_by": started_by}
        self.run = run
        self._insert_run(run)

    def _insert_run(self, run):
        try:
            row = self._exec("INSERT INTO runs (started_at, mode, profile, label, started_by) "
                             "VALUES (%s,%s,%s,%s,%s) RETURNING id",
                             (run["started_at"], run["mode"], run["profile"], run.get("label"), run.get("started_by")),
                             fetch=True)
            run["id"] = row["id"]
            log.info("Run %s started: %s%s by %s", run["id"], run["mode"],
                     f" #{run['profile']}" if run["profile"] is not None else "", run.get("started_by"))
            self.db.log_event("system", "run_started", {"run": run["id"], "mode": run["mode"],
                                                        "profile": run["profile"], "label": run.get("label"),
                                                        "by": run.get("started_by")})
        except Exception as e:
            log.warning("could not record run start: %s", e)

    def _close(self, s, reason, approx=False):
        run, self.run = self.run, None
        if run["id"] is None:
            return
        ended = s["ts"]
        last = run.get("last_running_ts")
        gap = max(3 * self.cfg.POLL_INTERVAL, 15)
        if last is not None and (ended - last).total_seconds() > gap:
            ended, approx = last, True          # stop happened while we weren't watching
        elif last is None:
            approx = True
            try:
                r = self._exec("SELECT max(ts) AS t FROM telemetry WHERE run_id = %s", (run["id"],), fetch=True)
                ended = r["t"] or run["started_at"]
            except Exception:
                ended = run["started_at"]
        try:
            self._exec("UPDATE runs SET ended_at = %s, end_reason = %s, end_approx = %s, updated_at = now() WHERE id = %s",
                       (ended, reason, approx, run["id"]))
            log.info("Run %s ended: %s", run["id"], reason)
            self.db.log_event("system", "run_ended", {"run": run["id"], "reason": reason},
                              ok=not reason.startswith(("aborted", "error", "interrupted")))
        except Exception as e:
            log.warning("could not record run end: %s", e)
        run["closed_reason"] = reason
        self._pending_stats = run["id"]   # computed after the final sample is inserted

    def finish_stats(self):
        """Store summary stats for a run that just ended (after its last row was written)."""
        rid = self._pending_stats
        if rid is None:
            return
        self._pending_stats = None
        try:
            stats = compute_stats(self.db, rid)
            self._exec("UPDATE runs SET stats = %s, updated_at = now() WHERE id = %s",
                       (psycopg2.extras.Json(stats), rid))
        except Exception as e:
            log.warning("could not store stats for run %s: %s", rid, e)


# ---------------------------------------------------------------- queries
def compute_stats(db, run_id):
    with db._conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(STATS_SQL, (run_id,))
        row = cur.fetchone() or {}
    return {k: _clean(v) for k, v in row.items()}


def _row(r):
    out = dict(r)
    if out.get("started_at") and out.get("ended_at"):
        out["duration_s"] = int((out["ended_at"] - out["started_at"]).total_seconds())
    return out


def list_runs(db, limit=50, offset=0, mode=None, profile=None, result=None, q=None, start=None, end=None):
    where, args = [], []
    if mode:
        where.append("mode = %s"); args.append(mode)
    if profile is not None:
        where.append("profile = %s"); args.append(profile)
    if result == "running":
        where.append("ended_at IS NULL")
    elif result:
        where.append("end_reason LIKE %s"); args.append(result + "%")
    if q:
        where.append("(label ILIKE %s OR notes ILIKE %s)"); args += [f"%{q}%", f"%{q}%"]
    if start:
        where.append("started_at >= %s"); args.append(start)
    if end:
        where.append("started_at < %s"); args.append(end)
    w = ("WHERE " + " AND ".join(where)) if where else ""
    with db._conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SELECT count(*) AS n FROM runs {w}", args)
        total = cur.fetchone()["n"]
        cur.execute(f"SELECT {RUN_COLS} FROM runs {w} ORDER BY started_at DESC LIMIT %s OFFSET %s", args + [limit, offset])
        rows = [_row(r) for r in cur.fetchall()]
    return total, rows


def get_run(db, run_id):
    with db._conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SELECT {RUN_COLS} FROM runs WHERE id = %s", (run_id,))
        r = cur.fetchone()
    if not r:
        return None
    r = _row(r)
    if r["ended_at"] is None or not r.get("stats"):
        r["stats"] = compute_stats(db, run_id)      # live stats for the running run
    return r


def update_run(db, run_id, label=None, notes=None):
    sets, args = [], []
    if label is not None:
        sets.append("label = %s"); args.append(label or None)
    if notes is not None:
        sets.append("notes = %s"); args.append(notes or None)
    if not sets:
        return get_run(db, run_id)
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE runs SET {', '.join(sets)}, updated_at = now() WHERE id = %s", args + [run_id])
        if cur.rowcount == 0:
            return None
    return get_run(db, run_id)


def run_telemetry(db, run_id, max_points=2000):
    with db._conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT count(*) AS n, min(ts) AS a, max(ts) AS b FROM telemetry WHERE run_id = %s", (run_id,))
        meta = cur.fetchone()
        if meta["n"] <= max_points:
            cur.execute("SELECT ts, pv, sp, hl_pv, power, profile_status, active_segment, segment_type, alarm_bits, hl_status "
                        "FROM telemetry WHERE run_id = %s ORDER BY ts", (run_id,))
        else:
            bucket = max(1, int((meta["b"] - meta["a"]).total_seconds() / max_points))
            cur.execute("""SELECT date_bin(make_interval(secs => %s), ts, timestamptz '2000-01-01') AS ts,
                                  avg(pv)::real AS pv, avg(sp)::real AS sp, avg(hl_pv)::real AS hl_pv, avg(power)::real AS power,
                                  max(profile_status) AS profile_status, max(active_segment) AS active_segment,
                                  max(segment_type) AS segment_type, max(alarm_bits) AS alarm_bits, max(hl_status) AS hl_status
                           FROM telemetry WHERE run_id = %s GROUP BY 1 ORDER BY 1""", (bucket, run_id))
        return cur.fetchall()


def iter_run_csv(db, run_id, columns):
    """Yield CSV lines for every sample of a run (server-side cursor, constant memory)."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    yield buf.getvalue()
    with db._conn() as conn, conn.cursor(name=f"run_csv_{run_id}") as cur:
        cur.itersize = 2000
        cur.execute(f"SELECT {', '.join(columns)} FROM telemetry WHERE run_id = %s ORDER BY ts", (run_id,))
        for row in cur:
            buf.seek(0); buf.truncate()
            w.writerow([v.isoformat() if hasattr(v, "isoformat") else v for v in row])
            yield buf.getvalue()

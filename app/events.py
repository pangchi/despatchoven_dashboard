"""Event log: categories, severities, queries, CSV export and process-event detection.

Every event has
  category  control | process | run | settings | access | system
  severity  info | warning | alarm
Process events (alarms, high-limit trips, sensor breaks, front-panel changes)
are detected by comparing consecutive controller snapshots.
"""
import csv
import io
import json
import logging
import time

import psycopg2.extras

from . import registers as R

log = logging.getLogger(__name__)

CATEGORIES = ("control", "process", "run", "settings", "access", "system")
SEVERITIES = ("info", "warning", "alarm")

SCHEMA = """
ALTER TABLE events ADD COLUMN IF NOT EXISTS category text;
ALTER TABLE events ADD COLUMN IF NOT EXISTS severity text;
UPDATE events SET category = 'system' WHERE category IS NULL AND action IN
    ('startup', 'purge', 'purge_check', 'comm_lost', 'comm_restored', 'db_connected');
UPDATE events SET category = 'process' WHERE category IS NULL AND action IN
    ('alarm_on', 'alarm_off', 'high_limit_tripped', 'high_limit_reset', 'sensor_break', 'sensor_ok');
UPDATE events SET category = 'control' WHERE category IS NULL AND action IN
    ('set_setpoint', 'set_mode', 'manual', 'timer', 'profile', 'relay', 'set_high_limit');
UPDATE events SET category = 'run' WHERE category IS NULL AND action LIKE 'run\\_%';
UPDATE events SET category = 'settings' WHERE category IS NULL AND action LIKE 'settings\\_%';
UPDATE events SET category = 'access' WHERE category IS NULL AND action IN ('login', 'logout', 'login_failed');
UPDATE events SET category = 'system' WHERE category IS NULL;
UPDATE events SET severity = CASE WHEN ok THEN 'info' ELSE 'warning' END WHERE severity IS NULL;
CREATE INDEX IF NOT EXISTS events_cat_ts_idx ON events (category, ts);
CREATE INDEX IF NOT EXISTS events_sev_ts_idx ON events (severity, ts) WHERE severity <> 'info';
"""

PROCESS_ACTIONS = {
    "alarm_on", "alarm_off", "high_limit_tripped", "high_limit_reset", "sensor_break", "sensor_ok",
    "hl_sensor_open", "hl_sensor_ok", "mode_changed", "setpoint_changed", "profile_held",
    "profile_resumed", "segment_changed",
}

_CATEGORY = {
    "startup": "system", "purge": "system", "purge_check": "system", "comm_lost": "system",
    "comm_restored": "system", "db_connected": "system",
    "set_setpoint": "control", "set_mode": "control", "manual": "control", "timer": "control",
    "profile": "control", "relay": "control", "set_high_limit": "control",
    "login": "access", "logout": "access", "login_failed": "access", "login_locked": "access",
    "api_key_rejected": "access", "api_locked": "access", "login_unlocked": "access", "local_access": "access",
}
_SEVERITY = {
    "high_limit_tripped": "alarm", "sensor_break": "alarm", "hl_sensor_open": "alarm",
    "alarm_on": "warning", "comm_lost": "warning", "login_failed": "warning", "login_locked": "alarm",
    "api_key_rejected": "warning", "api_locked": "alarm",
}


def classify(action, ok=True, category=None, severity=None):
    if category is None:
        if action in _CATEGORY:
            category = _CATEGORY[action]
        elif action.startswith("run_"):
            category = "run"
        elif action.startswith("settings_"):
            category = "settings"
        elif action in PROCESS_ACTIONS:
            category = "process"
        else:
            category = "system"
    if severity is None:
        severity = _SEVERITY.get(action, "info" if ok else "warning")
    return category, severity


# ---------------------------------------------------------------- queries
COLS = "id, ts, category, severity, source, action, detail, ok"


def _where(f):
    where, args = [], []
    if f.get("start"):
        where.append("ts >= %s"); args.append(f["start"])
    if f.get("end"):
        where.append("ts <= %s"); args.append(f["end"])
    if f.get("categories"):
        where.append("category = ANY(%s)"); args.append(list(f["categories"]))
    if f.get("severities"):
        where.append("severity = ANY(%s)"); args.append(list(f["severities"]))
    if f.get("source"):
        where.append("source ILIKE %s"); args.append(f"%{f['source']}%")
    if f.get("action"):
        where.append("action = %s"); args.append(f["action"])
    if f.get("q"):
        where.append("(action ILIKE %s OR source ILIKE %s OR detail::text ILIKE %s)")
        args += [f"%{f['q']}%"] * 3
    if f.get("failed_only"):
        where.append("NOT ok")
    return ("WHERE " + " AND ".join(where)) if where else "", args


def query(db, filters, limit=100, offset=0, with_total=False):
    w, args = _where(filters)
    with db._conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        total = None
        if with_total:
            cur.execute(f"SELECT count(*) AS n FROM events {w}", args)
            total = cur.fetchone()["n"]
        cur.execute(f"SELECT {COLS} FROM events {w} ORDER BY ts DESC, id DESC LIMIT %s OFFSET %s",
                    args + [limit, offset])
        return total, cur.fetchall()


def counts(db, filters):
    """Event counts per severity for the same filters (for the summary chips)."""
    w, args = _where({k: v for k, v in filters.items() if k != "severities"})
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT severity, count(*) FROM events {w} GROUP BY severity", args)
        return {s: n for s, n in cur.fetchall()}


def iter_csv(db, filters, max_rows=200000):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "category", "severity", "source", "event", "ok", "detail"])
    yield buf.getvalue()
    where, args = _where(filters)
    with db._conn() as conn, conn.cursor(name="events_csv") as cur:
        cur.itersize = 2000
        cur.execute(f"SELECT ts, category, severity, source, action, ok, detail FROM events {where} "
                    f"ORDER BY ts DESC, id DESC LIMIT %s", args + [max_rows])
        for ts, cat, sev, src, action, ok, detail in cur:
            buf.seek(0); buf.truncate()
            w.writerow([ts.isoformat(), cat, sev, src, action, "yes" if ok else "no",
                        json.dumps(detail, default=str) if detail is not None else ""])
            yield buf.getvalue()


# ---------------------------------------------------------------- process events


class ProcessEventDetector:
    """Compares consecutive snapshots and emits events for state changes.

    Changes that a remote command just caused (and that the API already logged)
    are not logged a second time: the API calls note_remote(kind) first.
    """
    SUPPRESS_S = 15

    def __init__(self):
        self.prev = None
        self._remote = {}            # kind -> monotonic time of the API command

    def note_remote(self, kind):
        self._remote[kind] = time.monotonic()

    def _remote_recent(self, kind):
        t = self._remote.get(kind)
        return t is not None and time.monotonic() - t < self.SUPPRESS_S

    def reset(self):
        self.prev = None

    def check(self, s):
        """Return a list of (action, detail, severity) for changes since the last snapshot."""
        out = []
        p = self.prev
        self.prev = s
        pv = s.get("pv")
        first = p is None
        if first:
            # report anything already abnormal when monitoring starts
            p = {"alarm_bits": 0, "hl_tripped": False, "sensor_break": False, "hl_status": 0}

        bits, pbits = int(s.get("alarm_bits") or 0), int(p.get("alarm_bits") or 0)
        for n in range(1, 6):
            m = 1 << (n - 1)
            if bits & m and not pbits & m:
                out.append(("alarm_on", {"alarm": n, "pv": pv, **({"at_startup": True} if first else {})}, "warning"))
            elif pbits & m and not bits & m:
                out.append(("alarm_off", {"alarm": n, "pv": pv}, "info"))

        if s.get("hl_tripped") and not p.get("hl_tripped"):
            out.append(("high_limit_tripped", {"hl_pv": s.get("hl_pv"), "limit": s.get("hl_alarm_value"),
                                               "pv": pv, **({"at_startup": True} if first else {})}, "alarm"))
        elif p.get("hl_tripped") and not s.get("hl_tripped"):
            out.append(("high_limit_reset", {"hl_pv": s.get("hl_pv")}, "info"))

        if s.get("sensor_break") and not p.get("sensor_break"):
            out.append(("sensor_break", {"sensor": "control thermocouple"}, "alarm"))
        elif p.get("sensor_break") and not s.get("sensor_break"):
            out.append(("sensor_ok", {"sensor": "control thermocouple", "pv": pv}, "info"))

        hl_open, phl_open = int(s.get("hl_status") or 0) & 1, int(p.get("hl_status") or 0) & 1
        if hl_open and not phl_open:
            out.append(("hl_sensor_open", {"sensor": "high-limit thermocouple"}, "alarm"))
        elif phl_open and not hl_open:
            out.append(("hl_sensor_ok", {"sensor": "high-limit thermocouple"}, "info"))

        if first:
            return out

        mode, pmode = int(s.get("current_mode") or 0), int(p.get("current_mode") or 0)
        if mode != pmode and not self._remote_recent("mode"):
            out.append(("mode_changed", {"from": R.CURRENT_MODE.get(pmode, pmode), "to": R.CURRENT_MODE.get(mode, mode)}, "info"))

        sp, psp = s.get("manual_sp"), p.get("manual_sp")
        if sp is not None and psp is not None and abs(sp - psp) >= 0.05 and not self._remote_recent("setpoint"):
            out.append(("setpoint_changed", {"from": psp, "to": sp}, "info"))

        if mode == 4 and pmode == 4:
            st, pst = s.get("profile_status"), p.get("profile_status")
            if st == 2 and pst in (1, 3) and not self._remote_recent("profile"):
                out.append(("profile_held", {"profile": s.get("selected_profile"), "segment": s.get("active_segment"), "pv": pv}, "info"))
            elif pst == 2 and st in (1, 3) and not self._remote_recent("profile"):
                out.append(("profile_resumed", {"profile": s.get("selected_profile"), "segment": s.get("active_segment")}, "info"))
            seg, pseg = s.get("active_segment"), p.get("active_segment")
            if st in (1, 2, 3) and pst in (1, 2, 3) and seg != pseg and seg:
                out.append(("segment_changed", {"profile": s.get("selected_profile"), "segment": seg,
                                                "type": R.SEGMENT_TYPE.get(int(s.get("segment_type") or 0), "?"),
                                                "sp": s.get("working_sp"), "pv": pv}, "info"))
        return out

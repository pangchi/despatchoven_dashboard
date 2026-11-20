"""PostgreSQL storage.

Telemetry lives in a table partitioned by day. Purging drops whole
partitions, which returns space to the filesystem immediately (a plain
DELETE would not shrink the files without VACUUM FULL).
"""
import json
import logging
import threading
from datetime import date, datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
import psycopg2.pool

log = logging.getLogger(__name__)

COLUMNS = ["ts", "state", "pv", "sp", "hl_pv", "power", "current_mode", "manual_status", "timer_status",
           "profile_status", "selected_profile", "active_segment", "segment_type", "relay_bits",
           "alarm_bits", "hl_status", "sensor_break", "run_id"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry (
    ts               timestamptz NOT NULL,
    state            text        NOT NULL,      -- 'active' | 'idle'
    pv               real,
    sp               real,
    hl_pv            real,
    power            real,
    current_mode     smallint,
    manual_status    smallint,
    timer_status     smallint,
    profile_status   smallint,
    selected_profile smallint,
    active_segment   smallint,
    segment_type     smallint,
    relay_bits       smallint,
    alarm_bits       smallint,
    hl_status        smallint,
    sensor_break     boolean
) PARTITION BY RANGE (ts);

CREATE INDEX IF NOT EXISTS telemetry_ts_idx ON telemetry (ts);

CREATE TABLE IF NOT EXISTS events (
    id      bigserial PRIMARY KEY,
    ts      timestamptz NOT NULL DEFAULT now(),
    source  text NOT NULL,          -- api key name, 'web:<user>', 'system'
    action  text NOT NULL,
    detail  jsonb,
    ok      boolean NOT NULL DEFAULT true
);
CREATE INDEX IF NOT EXISTS events_ts_idx ON events (ts);
"""


def _part_name(d):
    return f"telemetry_{d:%Y%m%d}"


class DatabaseUnavailable(psycopg2.OperationalError):
    pass


class Database:
    def __init__(self, cfg):
        self.cfg = cfg
        self.pool = None
        self._known_parts = set()
        self._lock = threading.Lock()

    def connect(self):
        pool = psycopg2.pool.ThreadedConnectionPool(
            1, 5, host=self.cfg.DB_HOST, port=self.cfg.DB_PORT, dbname=self.cfg.DB_NAME,
            user=self.cfg.DB_USER, password=self.cfg.DB_PASSWORD, connect_timeout=5,
            application_name="protocol3-monitor")
        self.pool = pool
        try:
            self.init_schema()
        except Exception:
            self.pool = None
            pool.closeall()
            raise

    @property
    def connected(self):
        return self.pool is not None

    def close(self):
        pool, self.pool = self.pool, None
        if pool is not None:
            pool.closeall()

    def _conn(self):
        if self.pool is None:
            raise DatabaseUnavailable("database not connected - check Settings > Database")
        return _PooledConn(self.pool)

    def init_schema(self):
        from .events import SCHEMA as EVENTS_SCHEMA
        from .lockout import SCHEMA as LOCKOUT_SCHEMA
        from .runs import SCHEMA as RUNS_SCHEMA
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(SCHEMA)
            cur.execute(RUNS_SCHEMA)
            cur.execute(EVENTS_SCHEMA)
            cur.execute(LOCKOUT_SCHEMA)
        self.ensure_partitions()

    # ---------- partitions ----------
    def ensure_partitions(self, days_ahead=2):
        today = datetime.now(timezone.utc).date()
        for i in range(-1, days_ahead + 1):
            self.ensure_partition(today + timedelta(days=i))

    def ensure_partition(self, d):
        name = _part_name(d)
        if name in self._known_parts:
            return
        with self._lock, self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF telemetry "
                f"FOR VALUES FROM (%s) TO (%s)",
                (f"{d.isoformat()} 00:00+00", f"{(d + timedelta(days=1)).isoformat()} 00:00+00"))
        self._known_parts.add(name)

    def list_partitions(self):
        """Oldest first: [(name, date, bytes)]"""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT c.relname, pg_total_relation_size(c.oid)
                FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
                JOIN pg_class p ON p.oid = i.inhparent
                WHERE p.relname = 'telemetry' ORDER BY c.relname""")
            out = []
            for name, size in cur.fetchall():
                try:
                    d = datetime.strptime(name[len("telemetry_"):], "%Y%m%d").date()
                except ValueError:
                    continue
                out.append((name, d, size))
            return out

    def drop_partition(self, name):
        if not name.startswith("telemetry_") or not name[10:].isdigit():
            raise ValueError("refusing to drop unexpected table " + name)
        with self._lock, self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {name}")
        self._known_parts.discard(name)

    # ---------- writes ----------
    def insert_telemetry(self, row):
        ts = row["ts"]
        self.ensure_partition(ts.astimezone(timezone.utc).date())
        vals = [row.get(c) for c in COLUMNS]
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"INSERT INTO telemetry ({','.join(COLUMNS)}) VALUES ({','.join(['%s'] * len(COLUMNS))})", vals)

    def log_event(self, source, action, detail=None, ok=True, severity=None, category=None):
        from .events import classify
        category, severity = classify(action, ok, category, severity)
        try:
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute("INSERT INTO events (source, action, detail, ok, category, severity) VALUES (%s,%s,%s,%s,%s,%s)",
                            (source, action, json.dumps(detail, default=str) if detail is not None else None, ok,
                             category, severity))
        except Exception:
            log.exception("failed to log event %s", action)

    def purge_events_before(self, ts):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM events WHERE ts < %s", (ts,))
            return cur.rowcount

    # ---------- reads ----------
    def telemetry(self, start, end, max_points=2000):
        """Return rows between start/end, averaged into buckets if there are too many."""
        span = max((end - start).total_seconds(), 1)
        with self._conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT count(*) AS n FROM telemetry WHERE ts BETWEEN %s AND %s", (start, end))
            n = cur.fetchone()["n"]
            if n <= max_points:
                cur.execute(f"SELECT {','.join(COLUMNS)} FROM telemetry WHERE ts BETWEEN %s AND %s ORDER BY ts",
                            (start, end))
            else:
                bucket = max(1, int(span / max_points))
                cur.execute("""
                    SELECT date_bin(make_interval(secs => %s), ts, timestamptz '2000-01-01') AS ts,
                           avg(pv)::real AS pv, avg(sp)::real AS sp, avg(hl_pv)::real AS hl_pv,
                           avg(power)::real AS power, max(alarm_bits) AS alarm_bits,
                           max(hl_status) AS hl_status, count(*) AS samples
                    FROM telemetry WHERE ts BETWEEN %s AND %s
                    GROUP BY 1 ORDER BY 1""", (bucket, start, end))
            return cur.fetchall()

    def events(self, limit=100, start=None, end=None):
        where, args = [], []
        if start:
            where.append("ts >= %s"); args.append(start)
        if end:
            where.append("ts <= %s"); args.append(end)
        w = ("WHERE " + " AND ".join(where)) if where else ""
        with self._conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT id, ts, category, severity, source, action, detail, ok FROM events {w} ORDER BY ts DESC LIMIT %s",
                        args + [limit])
            return cur.fetchall()

    def stats(self):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_database_size(current_database())")
            db_bytes = cur.fetchone()[0]
            cur.execute("SELECT min(ts), max(ts) FROM telemetry")
            oldest, newest = cur.fetchone()
        return {"db_bytes": db_bytes, "oldest": oldest, "newest": newest,
                "partitions": len(self.list_partitions())}

    def ping(self):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")


class _PooledConn:
    """Context manager: borrow a connection, commit on success, rollback on error."""

    def __init__(self, pool):
        self.pool = pool
        self.conn = None

    def __enter__(self):
        self.conn = self.pool.getconn()
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            broken = self.conn.closed or (exc_type is not None and isinstance(exc, psycopg2.OperationalError))
            self.pool.putconn(self.conn, close=broken)
        return False

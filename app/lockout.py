"""Login lockout after consecutive failed attempts.

Counters ("subjects") are kept per
  ("user", <username>)   dashboard account (only the configured user name is tracked)
  ("ip", <address>)      dashboard sign-ins from one address
  ("api", <address>)     API-key requests from one address (separate, so a misconfigured
                         script with an old key cannot lock the operator out of the dashboard)

After LOCKOUT_ATTEMPTS consecutive failures a subject is locked. Each further lock
lasts twice as long as the one before (LOCKOUT_MINUTES, x2, x4 ... up to
LOCKOUT_MAX_MINUTES). A successful sign-in clears the failure count; the lock level
falls back to zero after LOCKOUT_RESET_HOURS without failures.

State lives in PostgreSQL (table auth_lockouts) so a restart does not clear locks;
while the database is unavailable an in-memory copy is used.
"""
import logging
import threading
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_lockouts (
    scope         text NOT NULL,           -- user | ip | api
    subject       text NOT NULL,
    failures      integer NOT NULL DEFAULT 0,
    level         integer NOT NULL DEFAULT 0,
    locked_until  timestamptz,
    last_failure  timestamptz,
    PRIMARY KEY (scope, subject)
);
"""

SCOPES = ("user", "ip", "api")


def _now():
    return datetime.now(timezone.utc)


class Lockouts:
    def __init__(self, db, cfg):
        self.db, self.cfg = db, cfg
        self._mem = {}                  # (scope, subject) -> dict, used when the DB is down
        self._dirty = None              # subjects with a failure count, so success() rarely touches the DB
        self._lock = threading.Lock()

    # ---------------- storage ----------------
    def _use_db(self):
        return getattr(self.db, "connected", False)

    def _get(self, cur, scope, subject, for_update=True):
        if cur is None:
            r = self._mem.get((scope, subject))
            return dict(r) if r else None
        cur.execute("SELECT failures, level, locked_until, last_failure FROM auth_lockouts "
                    "WHERE scope = %s AND subject = %s" + (" FOR UPDATE" if for_update else ""), (scope, subject))
        r = cur.fetchone()
        return dict(zip(("failures", "level", "locked_until", "last_failure"), r)) if r else None

    def _put(self, cur, scope, subject, rec):
        if cur is None:
            self._mem[(scope, subject)] = dict(rec)
            return
        cur.execute("""INSERT INTO auth_lockouts (scope, subject, failures, level, locked_until, last_failure)
                       VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (scope, subject) DO UPDATE SET failures = EXCLUDED.failures,
                         level = EXCLUDED.level, locked_until = EXCLUDED.locked_until,
                         last_failure = EXCLUDED.last_failure""",
                    (scope, subject, rec["failures"], rec["level"], rec["locked_until"], rec["last_failure"]))

    def _tx(self, fn):
        """Run fn(cur) in a DB transaction, or fn(None) against memory when the DB is down."""
        with self._lock:
            if self._use_db():
                try:
                    with self.db._conn() as conn, conn.cursor() as cur:
                        return fn(cur)
                except Exception as e:
                    log.warning("lockout store unavailable, using memory: %s", e)
            return fn(None)

    # ---------------- policy ----------------
    def _duration(self, level):
        base = max(1, int(self.cfg.LOCKOUT_MINUTES))
        cap = max(base, int(self.cfg.LOCKOUT_MAX_MINUTES))
        return min(cap, base * (2 ** max(0, level - 1)))

    def _decayed(self, rec, now):
        """Forget old failures and lock levels after LOCKOUT_RESET_HOURS of quiet."""
        if rec and rec.get("last_failure") and now - rec["last_failure"] > timedelta(hours=self.cfg.LOCKOUT_RESET_HOURS):
            rec["failures"], rec["level"] = 0, 0
        return rec

    # ---------------- API ----------------
    def check(self, subjects):
        """Return (scope, subject, seconds_left) of the first active lock among subjects, or None."""
        if not self.cfg.LOCKOUT_ENABLED:
            return None
        now = _now()

        def fn(cur):
            for scope, subject in subjects:
                rec = self._get(cur, scope, subject, for_update=False)
                if rec and rec.get("locked_until") and rec["locked_until"] > now:
                    return scope, subject, int((rec["locked_until"] - now).total_seconds()) + 1
            return None
        return self._tx(fn)

    def failure(self, subjects):
        """Count a failed attempt. Returns {"locked": [(scope, subject, minutes, level)], "remaining": n}."""
        if not self.cfg.LOCKOUT_ENABLED:
            return {"locked": [], "remaining": None}
        now, limit = _now(), max(1, int(self.cfg.LOCKOUT_ATTEMPTS))

        def fn(cur):
            locked, remaining = [], limit
            for scope, subject in subjects:
                rec = self._decayed(self._get(cur, scope, subject), now) or {"failures": 0, "level": 0, "locked_until": None}
                rec["failures"] += 1
                rec["last_failure"] = now
                self._mark_dirty(cur, (scope, subject))
                if rec["failures"] >= limit:
                    rec["level"] += 1
                    minutes = self._duration(rec["level"])
                    rec["locked_until"] = now + timedelta(minutes=minutes)
                    rec["failures"] = 0
                    locked.append((scope, subject, minutes, rec["level"]))
                    remaining = 0
                else:
                    remaining = min(remaining, limit - rec["failures"])
                self._put(cur, scope, subject, rec)
            return {"locked": locked, "remaining": remaining}
        return self._tx(fn)

    def _mark_dirty(self, cur, key):
        if self._dirty is None:
            self._load_dirty(cur)
        self._dirty.add(key)

    def _load_dirty(self, cur):
        self._dirty = set(k for k, r in self._mem.items() if r.get("failures"))
        if cur is not None:
            cur.execute("SELECT scope, subject FROM auth_lockouts WHERE failures > 0")
            self._dirty |= set(cur.fetchall())

    def success(self, subjects):
        """A good sign-in clears the consecutive-failure count (the lock level decays with time)."""
        if self._dirty is not None and not any(tuple(s) in self._dirty for s in subjects):
            return                      # nothing to clear: no DB round trip
        def fn(cur):
            if self._dirty is None:
                self._load_dirty(cur)
            for scope, subject in subjects:
                self._dirty.discard((scope, subject))
                if cur is None:
                    rec = self._mem.get((scope, subject))
                    if rec:
                        rec["failures"] = 0
                else:
                    cur.execute("UPDATE auth_lockouts SET failures = 0 WHERE scope = %s AND subject = %s "
                                "AND failures > 0", (scope, subject))
        self._tx(fn)

    def active(self):
        """Current locks and subjects with failures, newest first."""
        now = _now()

        def fn(cur):
            if cur is None:
                rows = [(s, k, r["failures"], r["level"], r["locked_until"], r.get("last_failure"))
                        for (s, k), r in self._mem.items()]
            else:
                cur.execute("SELECT scope, subject, failures, level, locked_until, last_failure FROM auth_lockouts "
                            "WHERE locked_until > now() OR failures > 0 ORDER BY last_failure DESC NULLS LAST")
                rows = cur.fetchall()
            out = []
            for scope, subject, failures, level, until, last in rows:
                is_locked = bool(until and until > now)
                if not is_locked and not failures:
                    continue
                out.append({"scope": scope, "subject": subject, "failures": failures, "level": level,
                            "locked": is_locked, "locked_until": until if is_locked else None,
                            "seconds_left": int((until - now).total_seconds()) if is_locked else 0,
                            "last_failure": last})
            return out
        return self._tx(fn)

    def unlock(self, scope=None, subject=None):
        """Clear one subject (scope + subject), every subject of a scope, or everything. Returns rows cleared."""
        def fn(cur):
            if cur is None:
                keys = [k for k in self._mem if (scope is None or k[0] == scope) and (subject is None or k[1] == subject)]
                for k in keys:
                    del self._mem[k]
                self._dirty = None
                return len(keys)
            where, args = [], []
            if scope:
                where.append("scope = %s"); args.append(scope)
            if subject:
                where.append("subject = %s"); args.append(subject)
            cur.execute("DELETE FROM auth_lockouts" + (" WHERE " + " AND ".join(where) if where else ""), args)
            self._dirty = None
            return cur.rowcount
        return self._tx(fn)


def client_ip(request, cfg):
    """The caller's address. Forwarded headers are trusted only behind a configured reverse proxy:
    otherwise anyone could send a different fake address with every attempt and never be locked."""
    if getattr(cfg, "TRUST_PROXY", False):
        real = (request.headers.get("X-Real-IP") or "").strip()
        if real:
            return real[:64]
        fwd = [p.strip() for p in (request.headers.get("X-Forwarded-For") or "").split(",") if p.strip()]
        if fwd:
            return fwd[-1][:64]          # the entry added by our own proxy
    return request.remote_addr or "?"


def fmt_wait(seconds):
    m = (seconds + 59) // 60
    return f"{m} minute{'s' if m != 1 else ''}" if seconds >= 60 else f"{seconds} seconds"


# ---------------------------------------------------------------- passwordless local access
def parse_networks(text):
    """'localhost, 192.168.1.0/24, 10.0.0.5' -> list of ip_network. Raises ValueError on bad input."""
    import ipaddress
    nets = []
    for item in (text or "").replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        if item.lower() == "localhost":
            nets += [ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128")]
        else:
            nets.append(ipaddress.ip_network(item, strict=False))
    return nets


def ip_in_networks(ip, nets):
    import ipaddress
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if getattr(addr, "ipv4_mapped", None):           # ::ffff:192.168.1.5
        addr = addr.ipv4_mapped
    return any(addr.version == n.version and addr in n for n in nets)


def host_allowed(host_header, cfg):
    """Passwordless access only when the page was opened by IP, localhost, this machine's
    name or a name listed in ALLOWED_HOSTS. Stops DNS-rebinding: a malicious website whose
    domain is pointed at the Pi would otherwise be 'local' in a workshop browser."""
    import ipaddress
    import socket
    host = (host_header or "").strip().lower()
    if host.startswith("["):                          # [::1]:8000
        host = host[1:host.find("]")] if "]" in host else host
    elif host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    me = socket.gethostname().lower()
    allowed = {"localhost", me, me + ".local"}
    allowed |= {h.strip().lower() for h in (getattr(cfg, "ALLOWED_HOSTS", "") or "").split(",") if h.strip()}
    return host in allowed

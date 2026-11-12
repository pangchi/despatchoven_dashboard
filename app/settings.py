"""Settings editable from the web UI.

Values are validated here, written back to .env (comments and order kept,
previous file saved as .env.bak) and applied to the running app without a
restart. Secrets used for authentication (SECRET_KEY, API_KEYS,
WEB_PASSWORD_HASH) and the high-limit write gate are NOT editable here.
"""
import glob
import os
import re
import tempfile
import time
from types import SimpleNamespace

from . import registers as R

BAUD_RATES = [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200]

# key, section, label, type, options/limits, help
FIELDS = [
    ("SERIAL_PORT", "serial", "Serial port", "port", {}, "RS-485 adapter. Prefer /dev/serial/by-id/... (stable across reboots). SIMULATOR = no hardware."),
    ("SERIAL_BAUD", "serial", "Baud rate", "choice", {"options": BAUD_RATES}, "Must match the controller: Configuration Menu > Comms > Data rate."),
    ("SERIAL_PARITY", "serial", "Parity", "choice", {"options": ["N", "E", "O"]}, "N = none, E = even, O = odd."),
    ("SERIAL_STOPBITS", "serial", "Stop bits", "choice", {"options": [1, 2]}, ""),
    ("MODBUS_ADDRESS", "serial", "Modbus address", "int", {"min": 1, "max": 247}, "Controller's Comms > Modbus address."),
    ("SERIAL_TIMEOUT", "serial", "Timeout (s)", "float", {"min": 0.05, "max": 5, "step": 0.05}, "Wait per request."),
    ("MODBUS_RETRIES", "serial", "Retries", "int", {"min": 0, "max": 5}, "Extra attempts before a read counts as failed."),

    ("DB_HOST", "db", "Host", "str", {"max_len": 255}, "localhost, an IP/hostname, or a socket folder such as /var/run/postgresql."),
    ("DB_PORT", "db", "Port", "int", {"min": 1, "max": 65535}, ""),
    ("DB_NAME", "db", "Database", "str", {"max_len": 63, "required": True}, ""),
    ("DB_USER", "db", "User", "str", {"max_len": 63, "required": True}, ""),
    ("DB_PASSWORD", "db", "Password", "secret", {"max_len": 255}, "Leave blank to keep the current password."),

    ("POLL_INTERVAL", "sampling", "Live poll (s)", "float", {"min": 0.5, "max": 60, "step": 0.5}, "How often the controller is read for the dashboard."),
    ("LOG_INTERVAL_ACTIVE", "sampling", "Log every (s) - active", "float", {"min": 1, "max": 3600, "step": 1}, "Database rate while a process runs."),
    ("LOG_INTERVAL_IDLE", "sampling", "Log every (s) - idle", "float", {"min": 1, "max": 86400, "step": 1}, "Database rate while idle."),
    ("IDLE_PV_THRESHOLD", "sampling", "Active above PV", "optfloat", {"min": -200, "max": 2000, "step": 1}, "Count as active while the oven is hotter than this (cool-down). Blank = off."),
    ("IDLE_HOLDOFF", "sampling", "Active hold-off (s)", "float", {"min": 0, "max": 86400, "step": 10}, "Stay at the active rate this long after a run stops."),

    ("PURGE_THRESHOLD_PCT", "storage", "Purge when disk above (%)", "float", {"min": 10, "max": 99, "step": 1}, ""),
    ("PURGE_TARGET_PCT", "storage", "Purge down to (%)", "float", {"min": 5, "max": 98, "step": 1}, "Must be below the purge level."),
    ("RETENTION_DAYS", "storage", "Keep days (0 = until disk full)", "int", {"min": 0, "max": 3650}, ""),
    ("PURGE_CHECK_INTERVAL", "storage", "Check every (s)", "float", {"min": 30, "max": 86400, "step": 30}, ""),
    ("DISK_CHECK_PATH", "storage", "Disk to watch", "str", {"max_len": 255, "required": True}, "Filesystem holding the PostgreSQL data."),

    ("CONTROL_ENABLED", "control", "Remote control enabled", "bool", {}, "Off = monitor only; all control commands are refused."),
    ("SETPOINT_MIN", "control", "Setpoint min", "optfloat", {"min": -200, "max": 2000, "step": 1}, "Extra limit on top of the controller's own SP min. Blank = controller limit only."),
    ("SETPOINT_MAX", "control", "Setpoint max", "optfloat", {"min": -200, "max": 2000, "step": 1}, "Blank = controller limit only."),
    ("LOG_LEVEL", "control", "App log level", "choice", {"options": ["DEBUG", "INFO", "WARNING", "ERROR"]}, ""),
]
SECTIONS = [
    ("serial", "Controller connection (RS-485 / Modbus RTU)"),
    ("db", "Database (PostgreSQL)"),
    ("sampling", "Sampling & logging"),
    ("storage", "Storage & purge"),
    ("control", "Control & safety"),
]
FIELD = {f[0]: f for f in FIELDS}
SERIAL_KEYS = {"SERIAL_PORT", "SERIAL_BAUD", "SERIAL_PARITY", "SERIAL_STOPBITS", "MODBUS_ADDRESS",
               "SERIAL_TIMEOUT", "MODBUS_RETRIES"}
DB_KEYS = {"DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"}


class SettingsError(ValueError):
    pass


# ---------------------------------------------------------------- read
def describe(cfg):
    """Schema + current values for the UI."""
    sections = []
    for sid, title in SECTIONS:
        fields = []
        for key, sec, label, typ, opts, help_ in FIELDS:
            if sec != sid:
                continue
            value = getattr(cfg, key, None)
            f = {"key": key, "label": label, "type": typ, "help": help_, **opts}
            if typ == "secret":
                f["value"] = ""
                f["is_set"] = bool(value)
            else:
                f["value"] = value
            fields.append(f)
        sections.append({"id": sid, "title": title, "fields": fields})
    return {
        "sections": sections,
        "env_file": str(cfg.ENV_FILE),
        "readonly": {"ALLOW_HIGH_LIMIT_WRITE": cfg.ALLOW_HIGH_LIMIT_WRITE,
                     "API keys": len(cfg.API_KEYS), "WEB_USERNAME": cfg.WEB_USERNAME},
    }


# ---------------------------------------------------------------- validate
def _coerce(key, raw):
    _, _, label, typ, opts, _ = FIELD[key]
    if typ in ("str", "port", "secret"):
        v = "" if raw is None else str(raw).strip()
        if any(c in v for c in "\r\n\0"):
            raise SettingsError(f"{label}: invalid characters")
        if len(v) > opts.get("max_len", 255):
            raise SettingsError(f"{label}: too long")
        if opts.get("required") and not v:
            raise SettingsError(f"{label} is required")
        return v
    if typ == "bool":
        if isinstance(raw, bool):
            return raw
        if str(raw).lower() in ("true", "1", "yes", "on"):
            return True
        if str(raw).lower() in ("false", "0", "no", "off"):
            return False
        raise SettingsError(f"{label}: expected true/false")
    if typ == "choice":
        for o in opts["options"]:
            if str(raw).strip().upper() == str(o).upper():
                return o
        raise SettingsError(f"{label}: must be one of {', '.join(map(str, opts['options']))}")
    if typ == "optfloat" and (raw is None or str(raw).strip() == ""):
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise SettingsError(f"{label}: must be a number")
    if v != v or v in (float("inf"), float("-inf")):
        raise SettingsError(f"{label}: must be finite")
    if typ == "int":
        if v != int(v):
            raise SettingsError(f"{label}: must be a whole number")
        v = int(v)
    if "min" in opts and v < opts["min"]:
        raise SettingsError(f"{label}: minimum is {opts['min']}")
    if "max" in opts and v > opts["max"]:
        raise SettingsError(f"{label}: maximum is {opts['max']}")
    return v


def validate(cfg, data):
    """Return {key: new_value} for keys that actually change. Raises SettingsError."""
    if not isinstance(data, dict):
        raise SettingsError("JSON object expected")
    unknown = set(data) - set(FIELD)
    if unknown:
        raise SettingsError("not editable here: " + ", ".join(sorted(unknown)))
    changes = {}
    for key, raw in data.items():
        if FIELD[key][3] == "secret" and (raw is None or str(raw) == ""):
            continue                       # blank secret = keep current
        v = _coerce(key, raw)
        if v != getattr(cfg, key, None):
            changes[key] = v
    merged = lambda k: changes.get(k, getattr(cfg, k, None))
    if merged("PURGE_TARGET_PCT") >= merged("PURGE_THRESHOLD_PCT"):
        raise SettingsError("'Purge down to' must be below 'Purge when disk above'")
    if merged("LOG_INTERVAL_IDLE") < merged("LOG_INTERVAL_ACTIVE"):
        raise SettingsError("Idle log interval should not be shorter than the active one")
    lo, hi = merged("SETPOINT_MIN"), merged("SETPOINT_MAX")
    if lo is not None and hi is not None and lo >= hi:
        raise SettingsError("Setpoint min must be below setpoint max")
    if "DISK_CHECK_PATH" in changes and not os.path.isdir(changes["DISK_CHECK_PATH"]):
        raise SettingsError("Disk to watch: folder does not exist")
    if "SERIAL_PORT" in changes and changes["SERIAL_PORT"].upper() == "SIMULATOR":
        changes["SERIAL_PORT"] = "SIMULATOR"
    elif "SERIAL_PORT" in changes and not changes["SERIAL_PORT"].startswith("/dev/"):
        raise SettingsError("Serial port must be a /dev/... path or SIMULATOR")
    return changes


def env_text(value):
    """Format a value for .env (quoted when needed)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value == int(value):
        value = int(value)
    s = str(value)
    if re.search(r"[\s#'\"$\\`]", s):
        if "'" in s:
            raise SettingsError("values containing a single quote are not supported")
        return f"'{s}'"
    return s


# ---------------------------------------------------------------- write
def write_env(path, changes):
    """Update keys in .env in place, keeping comments and order. Atomic, keeps .env.bak."""
    path = str(path)
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    done = set()
    for i, line in enumerate(lines):
        m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m and m.group(1) in changes:
            key = m.group(1)
            lines[i] = f"{key}={env_text(changes[key])}"
            done.add(key)
    for key, v in changes.items():
        if key not in done:
            lines.append(f"{key}={env_text(v)}")
    d = os.path.dirname(os.path.abspath(path))
    if not os.access(d, os.W_OK) or (os.path.exists(path) and not os.access(path, os.W_OK)):
        raise PermissionError(f"{path} is not writable by user {os.getenv('USER') or os.getuid()}")
    if os.path.exists(path):
        with open(path) as src, open(path + ".bak", "w") as bak:
            bak.write(src.read())
        os.chmod(path + ".bak", 0o600)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".env.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------- serial helpers
def list_ports(current=None):
    ports, seen = [], set()
    for p in sorted(glob.glob("/dev/serial/by-id/*")):
        target = os.path.realpath(p)
        ports.append({"path": p, "label": f"{os.path.basename(p)}  ({os.path.basename(target)})", "stable": True})
        seen.add(target)
    for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*", "/dev/ttyAMA*", "/dev/ttyS0", "/dev/serial0"):
        for p in sorted(glob.glob(pattern)):
            real = os.path.realpath(p)
            if real in seen and p != "/dev/serial0":
                continue
            ports.append({"path": p, "label": p if real == p else f"{p}  ({os.path.basename(real)})", "stable": False})
            seen.add(real)
    if current and current != "SIMULATOR" and current not in {p["path"] for p in ports}:
        ports.append({"path": current, "label": f"{current}  (current - not found)" if not os.path.exists(current) else current,
                      "stable": current.startswith("/dev/serial/by-id/")})
    ports.append({"path": "SIMULATOR", "label": "SIMULATOR (no hardware)", "stable": True})
    return ports


def serial_params(cfg, data):
    """Candidate serial settings from the request, falling back to the current config."""
    p = SimpleNamespace(**{k: getattr(cfg, k) for k in SERIAL_KEYS})
    for key in SERIAL_KEYS:
        if key in data and data[key] not in (None, ""):
            setattr(p, key, _coerce(key, data[key]))
    if str(p.SERIAL_PORT).upper() == "SIMULATOR":
        p.SERIAL_PORT = "SIMULATOR"
    elif not str(p.SERIAL_PORT).startswith("/dev/"):
        raise SettingsError("Serial port must be a /dev/... path or SIMULATOR")
    p.simulator = p.SERIAL_PORT == "SIMULATOR"
    return p


def _open(params):
    from .controller import ModbusController, SimulatedController
    if params.simulator:
        return SimulatedController(params)
    ctl = ModbusController(params)
    try:
        ctl.connect()
    except Exception as e:
        raise SettingsError(f"cannot open {params.SERIAL_PORT}: {e}")
    return ctl


def test_serial(params):
    """Read PV and the controller info with the given settings."""
    t0 = time.monotonic()
    ctl = _open(params)
    try:
        pv = ctl.read(R.READ_MAP["pv"])
        info = ctl.read_info()
        mode = int(ctl.read(R.READ_MAP["current_mode"]))
        return {"ok": True, "pv": pv, "units": info.get("units_label", ""),
                "sp_min": info.get("sp_min"), "sp_max": info.get("sp_max"),
                "mode": R.CURRENT_MODE.get(mode, str(mode)),
                "ms": int((time.monotonic() - t0) * 1000)}
    finally:
        ctl.close()


def detect_serial(params, deadline_s=40):
    """Try common baud/parity combinations at the given address; first answer wins."""
    if params.simulator:
        return {"found": True, "SERIAL_BAUD": params.SERIAL_BAUD, "SERIAL_PARITY": params.SERIAL_PARITY, "tried": 0}
    order = [params.SERIAL_BAUD] + [b for b in (19200, 9600, 38400, 57600, 115200, 4800, 2400, 1200)
                                   if b != params.SERIAL_BAUD]
    parities = [params.SERIAL_PARITY] + [p for p in ("N", "E", "O") if p != params.SERIAL_PARITY]
    tried, skipped, t_end = 0, 0, time.monotonic() + deadline_s
    if not params.simulator and not os.path.exists(params.SERIAL_PORT):
        raise SettingsError(f"{params.SERIAL_PORT} does not exist - is the adapter plugged in?")
    for baud in order:
        for parity in parities:
            if time.monotonic() > t_end:
                return {"found": False, "tried": tried, "error": "time limit reached"}
            cand = SimpleNamespace(**vars(params))
            cand.SERIAL_BAUD, cand.SERIAL_PARITY = baud, parity
            cand.MODBUS_RETRIES, cand.SERIAL_TIMEOUT = 0, min(params.SERIAL_TIMEOUT, 0.4)
            try:
                ctl = _open(cand)
            except SettingsError:
                if not os.path.exists(params.SERIAL_PORT) or not os.access(params.SERIAL_PORT, os.R_OK | os.W_OK):
                    raise               # port missing / no permission: no point scanning
                skipped += 1            # this adapter can't do this baud/parity - try the next one
                continue
            tried += 1
            try:
                pv = ctl.read(R.READ_MAP["pv"])
                return {"found": True, "SERIAL_BAUD": baud, "SERIAL_PARITY": parity, "pv": pv, "tried": tried,
                        "skipped": skipped}
            except Exception:
                pass
            finally:
                ctl.close()
    return {"found": False, "tried": tried,
            "error": f"no answer at address {params.MODBUS_ADDRESS} - check wiring (19 -, 20 +, 21 common), "
                     "address, and that Modbus is enabled (firmware 2.3+)"}


# ---------------------------------------------------------------- database helpers
def db_params(cfg, data):
    p = SimpleNamespace(**{k: getattr(cfg, k) for k in DB_KEYS})
    for key in DB_KEYS:
        if key in data and data[key] not in (None, ""):
            setattr(p, key, _coerce(key, data[key]))
    return p


def test_db(params):
    import psycopg2
    t0 = time.monotonic()
    try:
        conn = psycopg2.connect(host=params.DB_HOST, port=params.DB_PORT, dbname=params.DB_NAME,
                                user=params.DB_USER, password=params.DB_PASSWORD, connect_timeout=5,
                                application_name="protocol3-monitor-test")
    except psycopg2.Error as e:
        raise SettingsError(str(e).strip().splitlines()[-1] if str(e).strip() else "connection failed")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_setting('server_version'), to_regclass('telemetry') IS NOT NULL")
            version, has_table = cur.fetchone()
            cur.execute("SELECT has_schema_privilege('public', 'CREATE')")
            can_create = cur.fetchone()[0]
        return {"ok": True, "server_version": version, "telemetry_table": has_table,
                "can_create": can_create, "ms": int((time.monotonic() - t0) * 1000)}
    finally:
        conn.close()

"""Configuration loaded from the .env file (and the process environment).

Credentials never live in code: everything sensitive comes from .env,
which is excluded from version control (see .gitignore / .env.example).
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


def _bool(name, default=False):
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name, default):
    v = os.getenv(name)
    return int(v) if v not in (None, "") else default


def _float(name, default):
    v = os.getenv(name)
    return float(v) if v not in (None, "") else default


def _optfloat(name):
    v = os.getenv(name)
    return float(v) if v not in (None, "") else None


class Config:
    def __init__(self, env_file=None):
        self.ENV_FILE = Path(env_file or os.getenv("P3_ENV_FILE") or BASE_DIR / ".env")
        load_dotenv(self.ENV_FILE, override=False)

        # --- Flask / web login ---
        self.SECRET_KEY = os.getenv("SECRET_KEY", "")
        self.WEB_USERNAME = os.getenv("WEB_USERNAME", "admin")
        self.WEB_PASSWORD_HASH = os.getenv("WEB_PASSWORD_HASH", "")
        self.SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", False)

        # --- API keys (comma separated). Optional "name:key" form for audit logs ---
        self.API_KEYS = {}
        for i, item in enumerate(filter(None, (k.strip() for k in os.getenv("API_KEYS", "").split(",")))):
            if ":" in item:
                name, key = item.split(":", 1)
            else:
                name, key = f"key{i + 1}", item
            self.API_KEYS[name.strip()] = key.strip()

        # --- PostgreSQL ---
        self.DB_HOST = os.getenv("DB_HOST", "localhost")
        self.DB_PORT = _int("DB_PORT", 5432)
        self.DB_NAME = os.getenv("DB_NAME", "protocol3")
        self.DB_USER = os.getenv("DB_USER", "protocol3")
        self.DB_PASSWORD = os.getenv("DB_PASSWORD", "")

        # --- Serial / Modbus RTU ---
        # SERIAL_PORT=SIMULATOR runs a simulated oven (no hardware needed)
        self.SERIAL_PORT = os.getenv("SERIAL_PORT", "/dev/ttyUSB0")
        self.SERIAL_BAUD = _int("SERIAL_BAUD", 19200)
        self.SERIAL_PARITY = os.getenv("SERIAL_PARITY", "N").upper()[:1]  # N/E/O
        self.SERIAL_STOPBITS = _int("SERIAL_STOPBITS", 1)
        self.SERIAL_TIMEOUT = _float("SERIAL_TIMEOUT", 0.5)
        self.MODBUS_ADDRESS = _int("MODBUS_ADDRESS", 1)
        self.MODBUS_RETRIES = _int("MODBUS_RETRIES", 2)

        # --- Sampling ---
        self.POLL_INTERVAL = _float("POLL_INTERVAL", 2.0)          # live display poll (s)
        self.LOG_INTERVAL_ACTIVE = _float("LOG_INTERVAL_ACTIVE", 5.0)   # DB write while running (s)
        self.LOG_INTERVAL_IDLE = _float("LOG_INTERVAL_IDLE", 60.0)      # DB write while idle (s)
        self.IDLE_PV_THRESHOLD = _optfloat("IDLE_PV_THRESHOLD")    # PV above this => active even if stopped
        self.IDLE_HOLDOFF = _float("IDLE_HOLDOFF", 300.0)          # stay "active" this long after stop (s)

        # --- Disk purge ---
        self.DISK_CHECK_PATH = os.getenv("DISK_CHECK_PATH", "/")
        self.PURGE_THRESHOLD_PCT = _float("PURGE_THRESHOLD_PCT", 80.0)
        self.PURGE_TARGET_PCT = _float("PURGE_TARGET_PCT", 75.0)
        self.PURGE_CHECK_INTERVAL = _float("PURGE_CHECK_INTERVAL", 600.0)
        self.RETENTION_DAYS = _int("RETENTION_DAYS", 0)            # 0 = keep until disk pressure

        # --- Control safety ---
        # --- Login security ---
        self.LOCKOUT_ENABLED = _bool("LOCKOUT_ENABLED", True)
        self.LOCKOUT_ATTEMPTS = _int("LOCKOUT_ATTEMPTS", 5)          # consecutive failures before a lock
        self.LOCKOUT_MINUTES = _int("LOCKOUT_MINUTES", 2)            # first lock; doubles each time
        self.LOCKOUT_MAX_MINUTES = _int("LOCKOUT_MAX_MINUTES", 60)
        self.LOCKOUT_RESET_HOURS = _float("LOCKOUT_RESET_HOURS", 24.0)
        self.TRUST_PROXY = _bool("TRUST_PROXY", False)               # true only behind nginx/caddy etc.
        # Dashboard without password from these addresses: "localhost", CIDRs, single IPs (blank = off)
        self.LOCAL_NETWORKS = os.getenv("LOCAL_NETWORKS", "").strip()
        self.LOCAL_ACCESS = (os.getenv("LOCAL_ACCESS", "control").strip().lower() or "control")  # control | view
        self.ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "").strip()  # extra host names for passwordless access

        self.CONTROL_ENABLED = _bool("CONTROL_ENABLED", True)
        self.ALLOW_HIGH_LIMIT_WRITE = _bool("ALLOW_HIGH_LIMIT_WRITE", False)
        self.SETPOINT_MIN = _optfloat("SETPOINT_MIN")
        self.SETPOINT_MAX = _optfloat("SETPOINT_MAX")

        self.START_POLLER = _bool("START_POLLER", True)
        self.LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

    @property
    def simulator(self):
        return self.SERIAL_PORT.upper() == "SIMULATOR"

    def validate(self):
        problems = []
        if len(self.SECRET_KEY) < 16:
            problems.append("SECRET_KEY missing or shorter than 16 characters")
        if not self.API_KEYS:
            problems.append("API_KEYS is empty - the remote API will reject every request")
        for name, key in self.API_KEYS.items():
            if len(key) < 24:
                problems.append(f"API key '{name}' is shorter than 24 characters")
        if not self.WEB_PASSWORD_HASH:
            problems.append("WEB_PASSWORD_HASH empty - web login disabled (run: python -m app.tools hash-password)")
        if self.PURGE_TARGET_PCT >= self.PURGE_THRESHOLD_PCT:
            problems.append("PURGE_TARGET_PCT must be below PURGE_THRESHOLD_PCT")
        return problems

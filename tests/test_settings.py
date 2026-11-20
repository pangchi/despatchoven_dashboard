"""Settings validation and .env writing (no hardware or database): pytest -q"""
import os
import stat

import pytest

from app import settings as S


class Cfg:
    SERIAL_PORT = "/dev/ttyUSB0"; SERIAL_BAUD = 19200; SERIAL_PARITY = "N"; SERIAL_STOPBITS = 1
    MODBUS_ADDRESS = 1; SERIAL_TIMEOUT = 0.5; MODBUS_RETRIES = 2
    DB_HOST = "localhost"; DB_PORT = 5432; DB_NAME = "protocol3"; DB_USER = "protocol3"; DB_PASSWORD = "pw"
    POLL_INTERVAL = 2.0; LOG_INTERVAL_ACTIVE = 5.0; LOG_INTERVAL_IDLE = 60.0; IDLE_PV_THRESHOLD = None; IDLE_HOLDOFF = 300.0
    PURGE_THRESHOLD_PCT = 80.0; PURGE_TARGET_PCT = 75.0; RETENTION_DAYS = 0; PURGE_CHECK_INTERVAL = 600.0
    DISK_CHECK_PATH = "/"; CONTROL_ENABLED = True; SETPOINT_MIN = None; SETPOINT_MAX = None; LOG_LEVEL = "INFO"
    LOCKOUT_ENABLED = True; LOCKOUT_ATTEMPTS = 5; LOCKOUT_MINUTES = 2; LOCKOUT_MAX_MINUTES = 60
    LOCKOUT_RESET_HOURS = 24.0; TRUST_PROXY = False


def test_only_changed_values_returned():
    ch = S.validate(Cfg, {"SERIAL_BAUD": "19200", "SERIAL_PARITY": "e", "LOG_INTERVAL_IDLE": "120"})
    assert ch == {"SERIAL_PARITY": "E", "LOG_INTERVAL_IDLE": 120.0}


def test_blank_secret_keeps_password():
    assert S.validate(Cfg, {"DB_PASSWORD": ""}) == {}
    assert S.validate(Cfg, {"DB_PASSWORD": "new"}) == {"DB_PASSWORD": "new"}


@pytest.mark.parametrize("body", [
    {"SECRET_KEY": "x"}, {"API_KEYS": "a:b"}, {"ALLOW_HIGH_LIMIT_WRITE": True},   # not editable
    {"SERIAL_BAUD": 12345}, {"MODBUS_ADDRESS": 0}, {"MODBUS_ADDRESS": "1.5"},
    {"SERIAL_PORT": "COM3"}, {"PURGE_TARGET_PCT": 85}, {"LOG_INTERVAL_IDLE": 2},
    {"SETPOINT_MIN": 300, "SETPOINT_MAX": 200}, {"DISK_CHECK_PATH": "/no/such/dir"},
    {"DB_NAME": ""}, {"DB_HOST": "a\nb"}, {"POLL_INTERVAL": "nan"},
    {"LOCKOUT_ATTEMPTS": 0}, {"LOCKOUT_MINUTES": 30, "LOCKOUT_MAX_MINUTES": 10},
])
def test_rejects_bad_input(body):
    with pytest.raises(S.SettingsError):
        S.validate(Cfg, body)


def test_optional_float_blank_is_none():
    c = type("C", (Cfg,), {"IDLE_PV_THRESHOLD": 50.0})
    assert S.validate(c, {"IDLE_PV_THRESHOLD": ""}) == {"IDLE_PV_THRESHOLD": None}


def test_simulator_normalised():
    assert S.validate(Cfg, {"SERIAL_PORT": "simulator"}) == {"SERIAL_PORT": "SIMULATOR"}


def test_write_env_keeps_comments_order_and_backup(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# header\nSECRET_KEY=abc\n\n# serial\nSERIAL_BAUD=19200\nWEB_PASSWORD_HASH='scrypt:1$x$y'\n")
    S.write_env(env, {"SERIAL_BAUD": 9600, "IDLE_PV_THRESHOLD": None, "DISK_CHECK_PATH": "/mnt/my data",
                      "CONTROL_ENABLED": False, "LOG_INTERVAL_ACTIVE": 5.0})
    text = env.read_text().splitlines()
    assert text[:6] == ["# header", "SECRET_KEY=abc", "", "# serial", "SERIAL_BAUD=9600",
                        "WEB_PASSWORD_HASH='scrypt:1$x$y'"]
    assert "IDLE_PV_THRESHOLD=" in text
    assert "DISK_CHECK_PATH='/mnt/my data'" in text
    assert "CONTROL_ENABLED=false" in text
    assert "LOG_INTERVAL_ACTIVE=5" in text
    assert (tmp_path / ".env.bak").read_text().startswith("# header")
    assert stat.S_IMODE(os.stat(env).st_mode) == 0o600


def test_written_env_reads_back(tmp_path, monkeypatch):
    from dotenv import dotenv_values
    env = tmp_path / ".env"
    env.write_text("DB_PASSWORD=old\n")
    S.write_env(env, {"DB_PASSWORD": "p@ss w$rd#1", "SERIAL_PORT": "/dev/serial/by-id/usb-FTDI_FT232R-if00-port0"})
    vals = dotenv_values(env)
    assert vals["DB_PASSWORD"] == "p@ss w$rd#1"
    assert vals["SERIAL_PORT"] == "/dev/serial/by-id/usb-FTDI_FT232R-if00-port0"


def test_list_ports_always_offers_simulator():
    ports = S.list_ports("/dev/ttyGONE")
    assert ports[-1]["path"] == "SIMULATOR"
    assert any(p["path"] == "/dev/ttyGONE" for p in ports)

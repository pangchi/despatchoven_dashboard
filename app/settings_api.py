"""Settings endpoints (dashboard login only) and live apply."""
import logging
import os
import threading
from types import SimpleNamespace

from flask import Blueprint, current_app, g, jsonify, request

from . import settings as S
from .auth import session_api
from .config import Config
from .controller import make_controller
from .db import Database

log = logging.getLogger(__name__)
bp = Blueprint("settings_api", __name__, url_prefix="/api/v1/settings")
_apply_lock = threading.Lock()


def _svc():
    return current_app.extensions["p3"]


def _json():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise S.SettingsError("JSON object body required")
    return data


@bp.errorhandler(S.SettingsError)
def _bad(e):
    return jsonify(error=str(e)), 400


@bp.errorhandler(TimeoutError)
def _busy(e):
    return jsonify(error="controller busy, try again"), 503


@bp.get("")
@session_api
def get_settings():
    return jsonify(S.describe(_svc().cfg))


@bp.get("/serial-ports")
@session_api
def serial_ports():
    return jsonify(ports=S.list_ports(_svc().cfg.SERIAL_PORT))


@bp.post("/test-serial")
@session_api
def test_serial():
    svc = _svc()
    params = S.serial_params(svc.cfg, _json())
    with _apply_lock, svc.poller.paused("connection test"):
        svc.controller.close()          # free the port; the poller reopens it afterwards
        try:
            res = S.test_serial(params)
        except S.SettingsError:
            raise
        except Exception as e:
            res = {"ok": False, "error": str(e)}
    svc.db.log_event(g.actor, "settings_test_serial",
                     {"port": params.SERIAL_PORT, "baud": params.SERIAL_BAUD, "ok": res.get("ok")}, ok=res.get("ok", False))
    return jsonify(res)


@bp.post("/detect-serial")
@session_api
def detect_serial():
    svc = _svc()
    params = S.serial_params(svc.cfg, _json())
    with _apply_lock, svc.poller.paused("auto-detect"):
        svc.controller.close()
        res = S.detect_serial(params)
    svc.db.log_event(g.actor, "settings_detect_serial", {"port": params.SERIAL_PORT, **res}, ok=res["found"])
    return jsonify(res)


@bp.post("/test-db")
@session_api
def test_db():
    res = S.test_db(S.db_params(_svc().cfg, _json()))
    return jsonify(res)


@bp.post("")
@session_api
def save_settings():
    svc = _svc()
    cfg = svc.cfg
    with _apply_lock:
        changes = S.validate(cfg, _json())
        if not changes:
            return jsonify(ok=True, changed=[], message="nothing changed")

        # 1. a new database must work before anything is written
        new_db = None
        if changes.keys() & S.DB_KEYS:
            dbp = S.db_params(cfg, {k: v for k, v in changes.items() if k in S.DB_KEYS})
            S.test_db(dbp)
            tmp = SimpleNamespace(**{**vars(cfg), **vars(dbp)})
            new_db = Database(tmp)
            try:
                new_db.connect()            # creates tables / partitions if missing
            except Exception as e:
                raise S.SettingsError(f"database: {e}")

        # 2. persist
        try:
            S.write_env(cfg.ENV_FILE, changes)
        except PermissionError as e:
            if new_db:
                new_db.close()
            return jsonify(error=f"{e} - fix ownership: sudo chown <service user> {cfg.ENV_FILE}"), 500

        # 3. apply to the running app
        for k, v in changes.items():
            os.environ[k] = "" if v is None else ("true" if v is True else "false" if v is False else str(v))
        fresh = Config(cfg.ENV_FILE)
        cfg.__dict__.update(fresh.__dict__)       # every module holds this same cfg object

        if "LOG_LEVEL" in changes:
            logging.getLogger().setLevel(cfg.LOG_LEVEL)

        if changes.keys() & S.SERIAL_KEYS:
            with svc.poller.paused("applying settings"):
                new_ctl = make_controller(cfg)
                svc.poller.replace_controller(new_ctl)
                svc.controller = new_ctl

        if new_db:
            new_db.cfg = cfg
            old_db = svc.db
            svc.db = svc.poller.db = new_db
            with svc.runs.lock:
                svc.runs.db, svc.runs._loaded = new_db, False   # re-read the open run from the new DB
            try:
                old_db.close()
            except Exception:
                pass
            with svc.poller.state_lock:
                svc.poller.db_ok, svc.poller.db_error = True, None

        detail = {k: ("***" if k == "DB_PASSWORD" else v) for k, v in changes.items()}
        svc.db.log_event(g.actor, "settings_saved", detail)
        log.info("%s changed settings: %s", g.actor, ", ".join(changes))
        return jsonify(ok=True, changed=sorted(changes), settings=S.describe(cfg))

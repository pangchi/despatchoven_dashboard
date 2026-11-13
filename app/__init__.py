"""Protocol 3 remote monitor - Flask application factory."""
import logging
import os
from datetime import date, datetime, timedelta

from flask import Flask
from flask.json.provider import DefaultJSONProvider

from .config import Config
from .controller import make_controller
from .db import Database
from .poller import Poller
from .runs import RunTracker

def _read_version():
    """Single source of truth: the VERSION file in the project root."""
    try:
        from pathlib import Path
        return (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip() or "unknown"
    except OSError:
        return "unknown"


__version__ = _read_version()
log = logging.getLogger("protocol3")


class _JSON(DefaultJSONProvider):
    def default(self, o):
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        return super().default(o)


class Services:
    def __init__(self, cfg, db, controller, poller, runs):
        self.cfg, self.db, self.controller, self.poller, self.runs = cfg, db, controller, poller, runs


def create_app(cfg=None):
    cfg = cfg or Config()
    logging.basicConfig(level=cfg.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for p in cfg.validate():
        log.warning("config: %s", p)
    if len(cfg.SECRET_KEY) < 16:
        raise RuntimeError("SECRET_KEY must be set in .env (python -m app.tools gen-secret)")

    app = Flask(__name__)
    app.json = _JSON(app)
    app.config.update(
        SECRET_KEY=cfg.SECRET_KEY, P3=cfg,
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=cfg.SESSION_COOKIE_SECURE,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    )

    db = Database(cfg)
    db_error = None
    try:
        db.connect()
    except Exception as e:
        # Start anyway so the dashboard and Settings page stay reachable; the poller retries.
        db_error = str(e).strip().splitlines()[0] if str(e).strip() else repr(e)
        log.error("database unavailable at start: %s", db_error)
    controller = make_controller(cfg)
    runs = RunTracker(db, cfg)
    poller = Poller(controller, db, cfg, runs)
    if db_error:
        poller.db_ok, poller.db_error = False, db_error
    app.extensions["p3"] = Services(cfg, db, controller, poller, runs)

    from . import api, runs_api, settings_api, web
    app.register_blueprint(api.bp)
    app.register_blueprint(runs_api.bp)
    app.register_blueprint(settings_api.bp)
    app.register_blueprint(web.bp)

    import psycopg2

    @app.errorhandler(psycopg2.Error)
    def _db_down(e):
        msg = str(e).strip().splitlines()[0] if str(e).strip() else "database error"
        return {"error": f"database unavailable: {msg}"}, 503

    @app.context_processor
    def _inject_version():
        return {"app_version": __version__}

    @app.url_defaults
    def _static_cache_bust(endpoint, values):
        # /static/app.js?v=1.4.1 - browsers fetch fresh JS/CSS after every update
        if endpoint == "static":
            values.setdefault("v", __version__)

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-App-Version", __version__)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp

    # Only one poller may own the serial port: run under ONE worker process.
    if cfg.START_POLLER:
        poller.start()
        log.info("Protocol 3 monitor %s started (port %s)", __version__, cfg.SERIAL_PORT)
    db.log_event("system", "startup", {"version": __version__, "port": cfg.SERIAL_PORT})
    return app

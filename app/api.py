"""REST API (v1). All endpoints except /health need an API key or web session."""
import logging
from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, g, jsonify, request

from . import registers as R
from .auth import api_auth
from .controller import ControllerError
from .purge import disk_usage, run_purge

log = logging.getLogger(__name__)
bp = Blueprint("api", __name__, url_prefix="/api/v1")


class BadRequest(Exception):
    pass


def _svc():
    return current_app.extensions["p3"]


def _body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise BadRequest("JSON object body required")
    return data


def _num(data, key, lo=None, hi=None):
    if key not in data:
        raise BadRequest(f"'{key}' is required")
    try:
        v = float(data[key])
    except (TypeError, ValueError):
        raise BadRequest(f"'{key}' must be a number")
    if v != v or v in (float("inf"), float("-inf")):
        raise BadRequest(f"'{key}' must be finite")
    if lo is not None and v < lo:
        raise BadRequest(f"'{key}' must be >= {lo}")
    if hi is not None and v > hi:
        raise BadRequest(f"'{key}' must be <= {hi}")
    return v


def _label(data):
    """Optional lot / batch label for the run a start command begins."""
    from .runs_api import MAX_LABEL, BadRequest as RunBad, clean_text
    try:
        return clean_text(data.get("label"), "label", MAX_LABEL) or None
    except RunBad as e:
        raise BadRequest(str(e))


def _note_start(label):
    _svc().runs.note_start(g.actor, label)


def _choice(data, key, options):
    v = data.get(key)
    if v not in options:
        raise BadRequest(f"'{key}' must be one of: {', '.join(options)}")
    return v


def _command(action, detail, fn):
    """Run a control action with audit logging and uniform error handling."""
    svc = _svc()
    cfg = svc.cfg
    if not cfg.CONTROL_ENABLED:
        return jsonify(error="remote control disabled (CONTROL_ENABLED=false)"), 403
    try:
        result = fn(svc.controller)
    except BadRequest as e:
        svc.db.log_event(g.actor, action, {**detail, "error": str(e)}, ok=False)
        return jsonify(error=str(e)), 400
    except ControllerError as e:
        svc.db.log_event(g.actor, action, {**detail, "error": str(e)}, ok=False)
        return jsonify(error=f"controller: {e}"), 502
    svc.db.log_event(g.actor, action, {**detail, "result": result})
    svc.poller.request_log()
    log.info("%s %s %s -> %s", g.actor, action, detail, result)
    return jsonify(ok=True, action=action, result=result)


@bp.errorhandler(BadRequest)
def _bad(e):
    return jsonify(error=str(e)), 400


# ---------------- read ----------------
@bp.get("/health")
def health():
    st = _svc().poller.status()
    return jsonify(ok=st["comm_ok"] and st["db_ok"], comm_ok=st["comm_ok"], db_ok=st["db_ok"])


@bp.get("/status")
@api_auth
def status():
    return jsonify(_svc().poller.status())


@bp.get("/telemetry")
@api_auth
def telemetry():
    now = datetime.now(timezone.utc)
    try:
        if request.args.get("from"):
            start = datetime.fromisoformat(request.args["from"])
            end = datetime.fromisoformat(request.args["to"]) if request.args.get("to") else now
            start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
            end = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
        else:
            minutes = float(request.args.get("minutes", 60))
            start, end = now - timedelta(minutes=minutes), now
        max_points = max(10, min(int(request.args.get("max_points", 1500)), 20000))
    except (ValueError, TypeError):
        raise BadRequest("use ?minutes=N or ?from=ISO8601&to=ISO8601, optional max_points")
    if end <= start:
        raise BadRequest("'to' must be after 'from'")
    rows = _svc().db.telemetry(start, end, max_points)
    return jsonify(start=start, end=end, count=len(rows), rows=rows)


@bp.get("/events")
@api_auth
def events():
    try:
        limit = max(1, min(int(request.args.get("limit", 100)), 1000))
        start = datetime.fromisoformat(request.args["from"]) if request.args.get("from") else None
        end = datetime.fromisoformat(request.args["to"]) if request.args.get("to") else None
    except ValueError:
        raise BadRequest("use ?limit=N&from=ISO8601&to=ISO8601")
    start = start.replace(tzinfo=timezone.utc) if start and not start.tzinfo else start
    end = end.replace(tzinfo=timezone.utc) if end and not end.tzinfo else end
    return jsonify(events=_svc().db.events(limit, start, end))


@bp.get("/system")
@api_auth
def system():
    svc = _svc()
    cfg = svc.cfg
    return jsonify(
        disk=disk_usage(cfg.DISK_CHECK_PATH), disk_path=cfg.DISK_CHECK_PATH,
        purge_threshold_pct=cfg.PURGE_THRESHOLD_PCT, purge_target_pct=cfg.PURGE_TARGET_PCT,
        retention_days=cfg.RETENTION_DAYS, db=svc.db.stats(),
        last_purge=svc.poller.last_purge, control_enabled=cfg.CONTROL_ENABLED,
        high_limit_write=cfg.ALLOW_HIGH_LIMIT_WRITE, simulator=cfg.simulator,
        log_interval_active=cfg.LOG_INTERVAL_ACTIVE, log_interval_idle=cfg.LOG_INTERVAL_IDLE)


@bp.post("/system/purge")
@api_auth
def purge_now():
    svc = _svc()
    res = run_purge(svc.db, svc.cfg)
    svc.db.log_event(g.actor, "purge_check", {"dropped": len(res["dropped"])})
    return jsonify(res)


# ---------------- control ----------------
@bp.post("/control/setpoint")
@api_auth
def set_setpoint():
    data = _body()
    value = _num(data, "value")
    cfg = _svc().cfg

    def do(ctl):
        info = ctl.info or ctl.read_info()
        c_lo, c_hi = info.get("sp_min"), info.get("sp_max")
        if c_lo is None or c_hi is None or c_hi <= c_lo:   # register missing / unreadable
            c_lo, c_hi = None, None
        los = [x for x in (c_lo, cfg.SETPOINT_MIN) if x is not None]
        his = [x for x in (c_hi, cfg.SETPOINT_MAX) if x is not None]
        if not los or not his:
            raise BadRequest("no setpoint limits known - set SETPOINT_MIN/SETPOINT_MAX in .env")
        lo, hi = max(los), min(his)
        if not lo <= value <= hi:
            raise BadRequest(f"setpoint {value} outside allowed range {lo}..{hi}")
        return {"setpoint": ctl.write(R.READ_MAP["manual_sp"], value)}

    return _command("set_setpoint", {"value": value}, do)


@bp.post("/control/mode")
@api_auth
def set_mode():
    mode = _choice(_body(), "mode", list(R.MODE_SELECT_VALUES))

    def do(ctl):
        snap = ctl.snapshot()
        if _running(snap):
            raise BadRequest("stop the current process before changing mode")
        ctl.write(R.MODE_SELECT, R.MODE_SELECT_VALUES[mode])
        return {"mode": mode}

    return _command("set_mode", {"mode": mode}, do)


@bp.post("/control/manual")
@api_auth
def manual():
    data = _body()
    action = _choice(data, "action", ["start", "stop"])
    label = _label(data)

    def do(ctl):
        if action == "start" and int(ctl.read(R.READ_MAP["current_mode"])) != 2:
            raise BadRequest("controller is not in manual mode (POST /control/mode first)")
        if action == "start":
            _note_start(label)
        ctl.write(R.MANUAL_CONTROL, 1 if action == "start" else 0, verify=False)
        return {"manual": action}

    return _command("manual", {"action": action, "label": label}, do)


@bp.post("/control/timer")
@api_auth
def timer():
    data = _body()
    action = _choice(data, "action", ["start", "stop"])
    seconds = _num(data, "seconds", 1, 65535) if "seconds" in data else None
    label = _label(data)

    def do(ctl):
        if action == "start" and int(ctl.read(R.READ_MAP["current_mode"])) != 3:
            raise BadRequest("controller is not in timer mode (POST /control/mode first)")
        if seconds is not None:
            ctl.write(R.TIMER_TIME, int(seconds))
        if action == "start":
            _note_start(label)
        ctl.write(R.TIMER_CONTROL, 1 if action == "start" else 0, verify=False)
        return {"timer": action, "seconds": seconds}

    return _command("timer", {"action": action, "seconds": seconds, "label": label}, do)


@bp.post("/control/profile")
@api_auth
def profile():
    data = _body()
    action = _choice(data, "action", list(R.PROFILE_COMMANDS))
    number = int(_num(data, "profile", 0, 63)) if "profile" in data else None
    label = _label(data)

    def do(ctl):
        if int(ctl.read(R.READ_MAP["current_mode"])) != 4:
            raise BadRequest("controller is not in profile mode (POST /control/mode first)")
        if number is not None:
            if action != "run":
                raise BadRequest("'profile' can only be given with action 'run'")
            ctl.write(R.READ_MAP["selected_profile"], number)
        if action == "run":
            _note_start(label)
        ctl.write(R.PROFILE_CONTROL, R.PROFILE_COMMANDS[action], verify=False)
        return {"profile": action, "number": number}

    return _command("profile", {"action": action, "profile": number, "label": label}, do)


@bp.post("/control/relay")
@api_auth
def relay():
    data = _body()
    n = int(_num(data, "relay", 1, 4))
    on = data.get("on")
    if not isinstance(on, bool):
        raise BadRequest("'on' must be true or false")

    def do(ctl):
        ctl.write(R.MANUAL_RELAYS[n], 1 if on else 0)
        return {"relay": n, "on": on}

    return _command("relay", {"relay": n, "on": on}, do)


@bp.post("/control/high-limit")
@api_auth
def high_limit():
    cfg = _svc().cfg
    if not cfg.ALLOW_HIGH_LIMIT_WRITE:
        return jsonify(error="high-limit writes disabled (ALLOW_HIGH_LIMIT_WRITE=false)"), 403
    value = _num(_body(), "value", 0)

    def do(ctl):
        info = ctl.info or ctl.read_info()
        if info.get("sp_max") is not None and value > info["sp_max"] + 50:
            raise BadRequest("high limit must not exceed setpoint maximum + 50")
        return {"high_limit": ctl.write(R.READ_MAP["hl_alarm_value"], value)}

    return _command("set_high_limit", {"value": value}, do)


def _running(s):
    return s.get("manual_status") == 1 or s.get("timer_status") == 1 or s.get("profile_status") in (1, 2, 3)

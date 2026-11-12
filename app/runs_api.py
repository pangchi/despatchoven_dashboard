"""Runs API: list, detail, per-run telemetry, CSV export, labels/notes."""
from datetime import datetime, timezone

from flask import Blueprint, Response, current_app, g, jsonify, request

from . import runs as RN
from .auth import api_auth

bp = Blueprint("runs_api", __name__, url_prefix="/api/v1/runs")

CSV_COLUMNS = ["ts", "pv", "sp", "hl_pv", "power", "current_mode", "profile_status", "selected_profile",
               "active_segment", "segment_type", "manual_status", "timer_status", "relay_bits", "alarm_bits",
               "hl_status", "sensor_break", "state"]
MAX_LABEL, MAX_NOTES = 100, 2000


class BadRequest(Exception):
    pass


@bp.errorhandler(BadRequest)
def _bad(e):
    return jsonify(error=str(e)), 400


def _svc():
    return current_app.extensions["p3"]


def clean_text(v, name, max_len):
    if v is None:
        return None
    if not isinstance(v, str):
        raise BadRequest(f"'{name}' must be text")
    v = v.strip()
    if len(v) > max_len:
        raise BadRequest(f"'{name}' is longer than {max_len} characters")
    if any(ord(c) < 32 and c not in "\n\t" for c in v):
        raise BadRequest(f"'{name}' contains control characters")
    return v


def _int_arg(name, default, lo, hi):
    try:
        v = int(request.args.get(name, default))
    except (TypeError, ValueError):
        raise BadRequest(f"'{name}' must be a whole number")
    return max(lo, min(hi, v))


def _date_arg(name):
    v = request.args.get(name)
    if not v:
        return None
    try:
        d = datetime.fromisoformat(v)
    except ValueError:
        raise BadRequest(f"'{name}' must be an ISO date, e.g. 2026-11-12")
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


@bp.get("")
@api_auth
def list_runs():
    mode = request.args.get("mode") or None
    if mode and mode not in ("profile", "manual", "timer"):
        raise BadRequest("mode must be profile, manual or timer")
    profile = request.args.get("profile")
    profile = _int_arg("profile", 0, 0, 63) if profile not in (None, "") else None
    result = request.args.get("result") or None
    if result and result not in ("running", "completed", "aborted", "stopped", "error", "interrupted"):
        raise BadRequest("result must be running, completed, aborted, stopped, error or interrupted")
    total, rows = RN.list_runs(
        _svc().db, limit=_int_arg("limit", 50, 1, 500), offset=_int_arg("offset", 0, 0, 10**9),
        mode=mode, profile=profile, result=result, q=(request.args.get("q") or "").strip()[:100] or None,
        start=_date_arg("from"), end=_date_arg("to"))
    return jsonify(total=total, runs=rows)


@bp.get("/current")
@api_auth
def current():
    svc = _svc()
    cur = svc.runs.current()
    run = RN.get_run(svc.db, cur["id"]) if cur and cur.get("id") else None
    return jsonify(run=run, next_label=svc.runs.pending_label())


@bp.post("/next-label")
@api_auth
def next_label():
    data = request.get_json(silent=True) or {}
    label = clean_text(data.get("label"), "label", MAX_LABEL)
    svc = _svc()
    svc.runs.set_next_label(label or None)
    svc.db.log_event(g.actor, "run_next_label", {"label": label or None})
    return jsonify(next_label=label or None)


@bp.get("/<int:run_id>")
@api_auth
def detail(run_id):
    run = RN.get_run(_svc().db, run_id)
    if not run:
        return jsonify(error="run not found"), 404
    return jsonify(run)


@bp.patch("/<int:run_id>")
@api_auth
def patch(run_id):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise BadRequest("JSON object body required")
    label = clean_text(data.get("label"), "label", MAX_LABEL)
    notes = clean_text(data.get("notes"), "notes", MAX_NOTES)
    svc = _svc()
    run = RN.update_run(svc.db, run_id, label=label, notes=notes)
    if not run:
        return jsonify(error="run not found"), 404
    with svc.runs.lock:                      # keep the live run's label in sync for the dashboard
        if svc.runs.run and svc.runs.run.get("id") == run_id and label is not None:
            svc.runs.run["label"] = label or None
    svc.db.log_event(g.actor, "run_edited", {"run": run_id, "label": label, "notes_changed": notes is not None})
    return jsonify(run)


@bp.get("/<int:run_id>/telemetry")
@api_auth
def telemetry(run_id):
    db = _svc().db
    run = RN.get_run(db, run_id)
    if not run:
        return jsonify(error="run not found"), 404
    rows = RN.run_telemetry(db, run_id, _int_arg("max_points", 2000, 10, 20000))
    return jsonify(run=run, count=len(rows), rows=rows)


@bp.get("/<int:run_id>.csv")
@api_auth
def csv_export(run_id):
    db = _svc().db
    run = RN.get_run(db, run_id)
    if not run:
        return jsonify(error="run not found"), 404
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (run.get("label") or ""))[:40]
    name = f"run{run_id}_{run['started_at']:%Y%m%d-%H%M}{'_' + safe if safe else ''}.csv"
    return Response(RN.iter_run_csv(db, run_id, CSV_COLUMNS), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})

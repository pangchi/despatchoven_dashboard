import hmac
from urllib.parse import urlparse

from flask import Blueprint, current_app, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from .auth import current_user, login_required, password_required, password_user, pw_fingerprint

bp = Blueprint("web", __name__)


@bp.get("/")
@login_required
def index():
    svc = current_app.extensions["p3"]
    return render_template("index.html", cfg=svc.cfg)


@bp.get("/runs")
@login_required
def runs_page():
    return render_template("runs.html")


@bp.get("/runs/<int:run_id>")
@login_required
def run_page(run_id):
    return render_template("run.html", run_id=run_id)


@bp.get("/events")
@login_required
def events_page():
    return render_template("events.html")


@bp.get("/settings")
@password_required
def settings_page():
    return render_template("settings.html", cfg=current_app.extensions["p3"].cfg)


@bp.route("/login", methods=["GET", "POST"])
def login():
    from .auth import _client_ip
    from .lockout import fmt_wait
    svc = current_app.extensions["p3"]
    cfg, db = svc.cfg, svc.db
    error = None
    if request.method == "POST":
        ip = _client_ip()
        user_given = (request.form.get("username") or "").strip()[:64]
        # count the configured account even if the attacker rotates addresses; unknown names count per IP only
        subjects = [("ip", ip)]
        if hmac.compare_digest(user_given.encode(), cfg.WEB_USERNAME.encode()):
            subjects.append(("user", cfg.WEB_USERNAME))
        locked = svc.lockouts.check(subjects)
        if locked:
            error = f"Too many failed attempts. Sign-in is locked for {fmt_wait(locked[2])}."
        elif not cfg.WEB_PASSWORD_HASH:
            error = "Web login is not configured (WEB_PASSWORD_HASH)."
        else:
            # always run the (slow) hash check so timing does not reveal whether the username exists
            pw_ok = check_password_hash(cfg.WEB_PASSWORD_HASH, request.form.get("password", ""))
            if pw_ok and len(subjects) == 2:
                svc.lockouts.success(subjects)
                session.clear()
                session["user"] = cfg.WEB_USERNAME
                session["pwv"] = pw_fingerprint(cfg)
                session.permanent = True
                nxt = request.args.get("next", "")
                if not nxt.startswith("/") or nxt.startswith("//") or urlparse(nxt).netloc:
                    nxt = url_for("web.index")
                db.log_event(f"web:{cfg.WEB_USERNAME}", "login", {"ip": ip})
                return redirect(nxt)
            res = svc.lockouts.failure(subjects)
            db.log_event("web", "login_failed", {"ip": ip, "username": user_given,
                                                 "attempts_left": res["remaining"]}, ok=False)
            for scope, subject, minutes, level in res["locked"]:
                db.log_event("web", "login_locked", {"scope": scope, "subject": subject, "minutes": minutes,
                                                     "level": level, "ip": ip, "username": user_given}, ok=False)
            if res["locked"]:
                longest = max(m for _, _, m, _ in res["locked"])
                error = f"Too many failed attempts. Sign-in is locked for {longest} minute{'s' if longest != 1 else ''}."
            elif res["remaining"] is not None and res["remaining"] <= 2:
                error = (f"Invalid username or password. {res['remaining']} attempt"
                         f"{'s' if res['remaining'] != 1 else ''} left before sign-in is locked.")
            else:
                error = "Invalid username or password."
    note = None
    if request.args.get("why") == "settings":
        note = "Settings need a password sign-in, even on the local network."
    local = current_user() if not password_user() else None
    return render_template("login.html", error=error, note=note, local=local)


@bp.post("/logout")
def logout():
    if session.get("user"):
        from .auth import _client_ip
        current_app.extensions["p3"].db.log_event(f"web:{session['user']}", "logout", {"ip": _client_ip()})
    session.clear()
    return redirect(url_for("web.login"))

from urllib.parse import urlparse

from flask import Blueprint, current_app, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from .auth import login_required, note_failure, throttled

bp = Blueprint("web", __name__)


@bp.get("/")
@login_required
def index():
    svc = current_app.extensions["p3"]
    return render_template("index.html", user=session["user"], cfg=svc.cfg)


@bp.get("/runs")
@login_required
def runs_page():
    return render_template("runs.html", user=session["user"])


@bp.get("/runs/<int:run_id>")
@login_required
def run_page(run_id):
    return render_template("run.html", user=session["user"], run_id=run_id)


@bp.get("/settings")
@login_required
def settings_page():
    return render_template("settings.html", user=session["user"], cfg=current_app.extensions["p3"].cfg)


@bp.route("/login", methods=["GET", "POST"])
def login():
    cfg = current_app.extensions["p3"].cfg
    error = None
    if request.method == "POST":
        if throttled():
            error = "Too many failed attempts. Try again in a few minutes."
        elif not cfg.WEB_PASSWORD_HASH:
            error = "Web login is not configured (WEB_PASSWORD_HASH)."
        elif (request.form.get("username") == cfg.WEB_USERNAME
              and check_password_hash(cfg.WEB_PASSWORD_HASH, request.form.get("password", ""))):
            session.clear()
            session["user"] = cfg.WEB_USERNAME
            session.permanent = True
            nxt = request.args.get("next", "")
            if not nxt.startswith("/") or nxt.startswith("//") or urlparse(nxt).netloc:
                nxt = url_for("web.index")
            current_app.extensions["p3"].db.log_event(f"web:{cfg.WEB_USERNAME}", "login", {"ip": request.remote_addr})
            return redirect(nxt)
        else:
            note_failure()
            error = "Invalid username or password."
    return render_template("login.html", error=error)


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("web.login"))

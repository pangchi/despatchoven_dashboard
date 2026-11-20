"""Authentication: API keys for remote clients, session login for the web UI."""
import functools
import hmac

from flask import current_app, g, jsonify, redirect, request, session, url_for

def _client_ip():
    from .lockout import client_ip
    return client_ip(request, current_app.config["P3"])


def match_api_key(presented):
    if not presented:
        return None
    found = None
    for name, key in current_app.config["P3"].API_KEYS.items():
        # compare against every key in constant time
        if hmac.compare_digest(presented.encode(), key.encode()):
            found = name
    return found


def _presented_key():
    k = request.headers.get("X-API-Key")
    if not k:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            k = auth[7:].strip()
    return k


def api_auth(fn):
    """Accept a valid API key, or a logged-in web session (the dashboard uses the same API)."""

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        key = _presented_key()
        if key:
            from .lockout import fmt_wait
            svc = current_app.extensions["p3"]
            ip = _client_ip()
            locked = svc.lockouts.check([("api", ip)])
            if locked:
                resp = jsonify(error=f"too many failed API key attempts from {ip} - locked for {fmt_wait(locked[2])}")
                resp.headers["Retry-After"] = str(locked[2])
                return resp, 429
            name = match_api_key(key)
            if name:
                svc.lockouts.success([("api", ip)])
                g.actor = f"api:{name}"
                return fn(*a, **kw)
            res = svc.lockouts.failure([("api", ip)])
            try:   # the key itself is never logged
                svc.db.log_event("api", "api_key_rejected",
                                 {"ip": ip, "path": request.path, "attempts_left": res["remaining"]}, ok=False)
                for scope, subject, minutes, level in res["locked"]:
                    svc.db.log_event("api", "api_locked", {"ip": subject, "minutes": minutes, "level": level}, ok=False)
            except Exception:
                pass
            return jsonify(error="unauthorized - invalid API key"), 401
        user = current_user()
        if user:
            # CSRF guard for browser (cookie or local-network) state-changing requests: a custom header
            # cannot be sent cross-site without a CORS preflight, which this app never grants
            if request.method not in ("GET", "HEAD") and request.headers.get("X-Requested-With") != "p3-dashboard":
                return jsonify(error="missing X-Requested-With header"), 403
            if request.method not in ("GET", "HEAD") and user["level"] == "view":
                return jsonify(error="view-only access from this network - sign in with the password to control"), 403
            g.actor, g.user = user["name"], user
            return fn(*a, **kw)
        return jsonify(error="unauthorized - send X-API-Key header"), 401

    return wrapper


_LOCAL_SEEN = {}          # ip -> time of the last "local_access" event (one per hour per address)


def local_identity():
    """Passwordless identity for a request from a trusted local network, or None."""
    import time
    from .lockout import host_allowed, ip_in_networks, parse_networks
    cfg = current_app.config["P3"]
    if not cfg.LOCAL_NETWORKS:
        return None
    if not cfg.TRUST_PROXY and (request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP")):
        return None           # a proxy is forwarding outside traffic: 127.0.0.1 would match everyone
    try:
        nets = parse_networks(cfg.LOCAL_NETWORKS)
    except ValueError:
        return None
    ip = _client_ip()
    if not ip_in_networks(ip, nets) or not host_allowed(request.host, cfg):
        return None
    level = "view" if cfg.LOCAL_ACCESS == "view" else "control"
    now = time.time()
    if now - _LOCAL_SEEN.get(ip, 0) > 3600:
        _LOCAL_SEEN[ip] = now
        try:
            current_app.extensions["p3"].db.log_event(f"local:{ip}", "local_access",
                                                      {"ip": ip, "level": level, "path": request.path})
        except Exception:
            pass
    return {"name": f"local:{ip}", "label": f"Local · {ip}", "kind": "local", "level": level, "ip": ip}


def pw_fingerprint(cfg=None):
    """Short fingerprint of the current password hash, stored in each session at sign-in.
    Changing the password changes it, which signs out every other browser."""
    import hashlib
    cfg = cfg or current_app.config["P3"]
    return hashlib.sha256((cfg.WEB_PASSWORD_HASH or "").encode()).hexdigest()[:16]


def password_user():
    """User name of a valid password session, or None (stale sessions are cleared)."""
    user = session.get("user")
    if not user:
        return None
    if session.get("pwv") != pw_fingerprint():
        session.clear()               # password changed since this browser signed in
        return None
    return user


def current_user():
    """Who is making this request: a password session, a passwordless local user, or None."""
    user = password_user()
    if user:
        return {"name": f"web:{user}", "label": user, "kind": "password", "level": "control"}
    return local_identity()


def login_required(fn):
    """Page for a signed-in user or a passwordless local user."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        g.user = current_user()
        if not g.user:
            return redirect(url_for("web.login", next=request.path))
        return fn(*a, **kw)

    return wrapper


def password_required(fn):
    """Page that always needs a password sign-in (Settings), even on a trusted network."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        g.user = current_user()
        if not g.user or g.user["kind"] != "password":
            return redirect(url_for("web.login", next=request.path, why="settings" if g.user else None))
        return fn(*a, **kw)

    return wrapper


def session_api(fn):
    """JSON endpoint for a logged-in dashboard user only (API keys are refused).
    Used for settings: an API key can operate the oven but not reconfigure the system."""

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        user = password_user()
        if not user:
            return jsonify(error="settings need a dashboard login (API keys are not accepted here)"), 401
        if request.method not in ("GET", "HEAD") and request.headers.get("X-Requested-With") != "p3-dashboard":
            return jsonify(error="missing X-Requested-With header"), 403
        g.actor = f"web:{user}"
        return fn(*a, **kw)

    return wrapper

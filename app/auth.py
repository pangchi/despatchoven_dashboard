"""Authentication: API keys for remote clients, session login for the web UI."""
import functools
import hmac
import time
from collections import defaultdict, deque

from flask import current_app, g, jsonify, redirect, request, session, url_for

# simple in-memory throttle for failed auth attempts (per client IP)
_FAILS = defaultdict(deque)
FAIL_WINDOW = 300
FAIL_LIMIT = 10


def _client_ip():
    return request.headers.get("X-Real-IP") or request.remote_addr or "?"


def throttled():
    q = _FAILS[_client_ip()]
    now = time.time()
    while q and now - q[0] > FAIL_WINDOW:
        q.popleft()
    return len(q) >= FAIL_LIMIT


def note_failure():
    _FAILS[_client_ip()].append(time.time())


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
        if throttled():
            return jsonify(error="too many failed attempts, try later"), 429
        name = match_api_key(_presented_key())
        if name:
            g.actor = f"api:{name}"
            return fn(*a, **kw)
        if session.get("user"):
            # CSRF guard for cookie-authenticated state-changing requests
            if request.method not in ("GET", "HEAD") and request.headers.get("X-Requested-With") != "p3-dashboard":
                return jsonify(error="missing X-Requested-With header"), 403
            g.actor = f"web:{session['user']}"
            return fn(*a, **kw)
        if _presented_key():
            note_failure()
        return jsonify(error="unauthorized - send X-API-Key header"), 401

    return wrapper


def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("user"):
            return redirect(url_for("web.login", next=request.path))
        return fn(*a, **kw)

    return wrapper


def session_api(fn):
    """JSON endpoint for a logged-in dashboard user only (API keys are refused).
    Used for settings: an API key can operate the oven but not reconfigure the system."""

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("user"):
            return jsonify(error="settings need a dashboard login (API keys are not accepted here)"), 401
        if request.method not in ("GET", "HEAD") and request.headers.get("X-Requested-With") != "p3-dashboard":
            return jsonify(error="missing X-Requested-With header"), 403
        g.actor = f"web:{session['user']}"
        return fn(*a, **kw)

    return wrapper

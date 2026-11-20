"""Login lockout policy (in-memory store, no database): pytest -q"""
from datetime import timedelta
from types import SimpleNamespace

import pytest

import app.lockout as L


class NoDB:
    connected = False


def make(**kw):
    cfg = SimpleNamespace(LOCKOUT_ENABLED=True, LOCKOUT_ATTEMPTS=3, LOCKOUT_MINUTES=2,
                          LOCKOUT_MAX_MINUTES=10, LOCKOUT_RESET_HOURS=24.0, TRUST_PROXY=False)
    for k, v in kw.items():
        setattr(cfg, k, v)
    return L.Lockouts(NoDB(), cfg)


@pytest.fixture
def clock(monkeypatch):
    t = {"now": L._now()}
    monkeypatch.setattr(L, "_now", lambda: t["now"])
    return t


U = [("ip", "10.0.0.5"), ("user", "admin")]


def test_locks_after_consecutive_failures(clock):
    lk = make()
    assert lk.failure(U)["remaining"] == 2
    assert lk.failure(U)["remaining"] == 1
    assert lk.check(U) is None
    res = lk.failure(U)
    assert res["remaining"] == 0
    assert {(s, k) for s, k, _, _ in res["locked"]} == set(U)
    scope, subject, secs = lk.check(U)
    assert 110 <= secs <= 121                     # 2 minutes


def test_success_resets_consecutive_count(clock):
    lk = make()
    lk.failure(U); lk.failure(U)
    lk.success(U)
    assert lk.failure(U)["remaining"] == 2        # counting starts again
    assert lk.check(U) is None


def test_lock_expires_and_doubles_up_to_max(clock):
    lk = make()
    durations = []
    for _ in range(4):
        for _ in range(3):
            res = lk.failure([("ip", "1.2.3.4")])
        durations.append(res["locked"][0][2])
        clock["now"] += timedelta(minutes=res["locked"][0][2], seconds=1)
        assert lk.check([("ip", "1.2.3.4")]) is None
    assert durations == [2, 4, 8, 10]             # doubled, capped at LOCKOUT_MAX_MINUTES


def test_level_forgotten_after_quiet_period(clock):
    lk = make(LOCKOUT_RESET_HOURS=1)
    for _ in range(3):
        lk.failure([("ip", "x")])
    clock["now"] += timedelta(hours=2)
    lk.failure([("ip", "x")]); lk.failure([("ip", "x")])
    res = lk.failure([("ip", "x")])
    assert res["locked"][0][3] == 1 and res["locked"][0][2] == 2   # back to level 1, first duration


def test_old_failures_do_not_add_up(clock):
    lk = make(LOCKOUT_RESET_HOURS=1)
    lk.failure([("ip", "y")]); lk.failure([("ip", "y")])
    clock["now"] += timedelta(hours=2)
    assert lk.failure([("ip", "y")])["remaining"] == 2


def test_scopes_are_independent(clock):
    lk = make()
    for _ in range(3):
        lk.failure([("api", "10.0.0.5")])
    assert lk.check([("api", "10.0.0.5")])
    assert lk.check([("ip", "10.0.0.5")]) is None  # a bad script's key does not lock the dashboard


def test_unlock_and_active(clock):
    lk = make()
    for _ in range(3):
        lk.failure(U)
    lk.failure([("api", "9.9.9.9")])
    act = {(r["scope"], r["subject"]): r for r in lk.active()}
    assert act[("user", "admin")]["locked"] and act[("api", "9.9.9.9")]["failures"] == 1
    assert lk.unlock("user", "admin") == 1
    assert lk.check([("user", "admin")]) is None and lk.check([("ip", "10.0.0.5")])
    assert lk.unlock() == 2
    assert lk.active() == []


def test_disabled(clock):
    lk = make(LOCKOUT_ENABLED=False)
    for _ in range(10):
        lk.failure(U)
    assert lk.check(U) is None


class Req:
    def __init__(self, remote, headers):
        self.remote_addr, self.headers = remote, headers


def test_forwarded_headers_ignored_unless_trusted():
    spoof = Req("203.0.113.9", {"X-Real-IP": "1.1.1.1", "X-Forwarded-For": "2.2.2.2"})
    assert L.client_ip(spoof, SimpleNamespace(TRUST_PROXY=False)) == "203.0.113.9"
    assert L.client_ip(spoof, SimpleNamespace(TRUST_PROXY=True)) == "1.1.1.1"
    proxied = Req("127.0.0.1", {"X-Forwarded-For": "6.6.6.6, 198.51.100.7"})
    assert L.client_ip(proxied, SimpleNamespace(TRUST_PROXY=True)) == "198.51.100.7"   # entry our proxy added


def test_fmt_wait():
    assert L.fmt_wait(30) == "30 seconds"
    assert L.fmt_wait(61) == "2 minutes"
    assert L.fmt_wait(60) == "1 minute"

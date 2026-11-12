"""Run start/end classification (no database): pytest -q"""
from app.runs import end_reason, run_mode


def snap(**kw):
    base = {"current_mode": 0, "manual_status": 0, "timer_status": 0, "profile_status": 0,
            "selected_profile": 0, "timer_remaining": 0}
    base.update(kw)
    return base


def test_run_mode_profile_states():
    for st in (1, 2, 3):                       # running, held, delayed all count as one run
        assert run_mode(snap(current_mode=4, profile_status=st)) == "profile"
    for st in (0, 4, 5, 6):
        assert run_mode(snap(current_mode=4, profile_status=st)) is None


def test_run_mode_manual_and_timer():
    assert run_mode(snap(current_mode=2, manual_status=1)) == "manual"
    assert run_mode(snap(current_mode=3, timer_status=1)) == "timer"
    assert run_mode(snap(current_mode=2, manual_status=0)) is None


def test_stale_status_in_other_mode_is_ignored():
    # status registers can keep old values after a mode change
    assert run_mode(snap(current_mode=2, profile_status=1)) is None
    assert run_mode(snap(current_mode=0, manual_status=1)) is None


def test_end_reasons():
    assert end_reason("profile", snap(current_mode=4, profile_status=6)) == "completed"
    assert end_reason("profile", snap(current_mode=4, profile_status=5)) == "aborted"
    assert end_reason("profile", snap(current_mode=4, profile_status=4)) == "error"
    assert end_reason("profile", snap(current_mode=0, profile_status=1)).startswith("stopped")
    assert end_reason("timer", snap(current_mode=3, timer_remaining=0)) == "completed"
    assert end_reason("timer", snap(current_mode=3, timer_remaining=120)) == "stopped"
    assert end_reason("manual", snap(current_mode=2)) == "stopped"

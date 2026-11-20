"""Process-event detection and event classification (no hardware or database): pytest -q"""
from app.events import ProcessEventDetector, classify


def snap(**kw):
    base = {"current_mode": 2, "manual_status": 0, "timer_status": 0, "profile_status": 0, "pv": 25.0,
            "manual_sp": 100.0, "working_sp": 100.0, "hl_pv": 25.0, "hl_alarm_value": 250.0, "hl_status": 0,
            "hl_tripped": False, "sensor_break": False, "alarm_bits": 0, "selected_profile": 0,
            "active_segment": 0, "segment_type": 0}
    base.update(kw)
    return base


def actions(events):
    return [a for a, _, _ in events]


def test_first_snapshot_quiet_when_normal():
    assert ProcessEventDetector().check(snap()) == []


def test_first_snapshot_reports_active_abnormal_state():
    ev = ProcessEventDetector().check(snap(alarm_bits=0b101, hl_tripped=True))
    assert actions(ev) == ["alarm_on", "alarm_on", "high_limit_tripped"]
    assert ev[0][1]["alarm"] == 1 and ev[1][1]["alarm"] == 3
    assert all(d.get("at_startup") for _, d, _ in ev)
    assert ev[2][2] == "alarm"


def test_alarm_edges():
    d = ProcessEventDetector()
    d.check(snap())
    assert actions(d.check(snap(alarm_bits=0b10, pv=130))) == ["alarm_on"]
    assert d.check(snap(alarm_bits=0b10, pv=131)) == []            # still on: no repeat
    ev = d.check(snap(alarm_bits=0, pv=110))
    assert actions(ev) == ["alarm_off"] and ev[0][1]["alarm"] == 2 and ev[0][2] == "info"


def test_high_limit_and_sensors():
    d = ProcessEventDetector()
    d.check(snap())
    assert actions(d.check(snap(hl_tripped=True, hl_status=4))) == ["high_limit_tripped"]
    assert actions(d.check(snap(hl_tripped=False))) == ["high_limit_reset"]
    ev = d.check(snap(sensor_break=True, hl_status=1))
    assert actions(ev) == ["sensor_break", "hl_sensor_open"] and {s for _, _, s in ev} == {"alarm"}
    assert actions(d.check(snap())) == ["sensor_ok", "hl_sensor_ok"]


def test_front_panel_mode_and_setpoint_changes():
    d = ProcessEventDetector()
    d.check(snap())
    ev = d.check(snap(current_mode=4))
    assert actions(ev) == ["mode_changed"] and ev[0][1] == {"from": "manual", "to": "profile"}
    ev = d.check(snap(current_mode=4, manual_sp=120.0))
    assert actions(ev) == ["setpoint_changed"] and ev[0][1] == {"from": 100.0, "to": 120.0}


def test_remote_changes_not_logged_twice():
    d = ProcessEventDetector()
    d.check(snap())
    d.note_remote("mode"); d.note_remote("setpoint")
    assert d.check(snap(current_mode=3, manual_sp=150.0)) == []


def test_profile_hold_resume_and_segments():
    d = ProcessEventDetector()
    d.check(snap(current_mode=4, profile_status=1, selected_profile=3, active_segment=1, segment_type=1))
    ev = d.check(snap(current_mode=4, profile_status=1, selected_profile=3, active_segment=2, segment_type=3))
    assert actions(ev) == ["segment_changed"] and ev[0][1]["segment"] == 2 and ev[0][1]["type"] == "dwell"
    assert actions(d.check(snap(current_mode=4, profile_status=2, selected_profile=3, active_segment=2))) == ["profile_held"]
    assert actions(d.check(snap(current_mode=4, profile_status=1, selected_profile=3, active_segment=2))) == ["profile_resumed"]
    d.note_remote("profile")
    assert d.check(snap(current_mode=4, profile_status=2, selected_profile=3, active_segment=2)) == []


def test_reset_after_controller_swap():
    d = ProcessEventDetector()
    d.check(snap(alarm_bits=1))
    d.reset()
    assert actions(d.check(snap(alarm_bits=1))) == ["alarm_on"]      # re-reported for the new connection


def test_classify():
    assert classify("high_limit_tripped") == ("process", "alarm")
    assert classify("set_setpoint", ok=False) == ("control", "warning")
    assert classify("run_started") == ("run", "info")
    assert classify("settings_saved") == ("settings", "info")
    assert classify("login_failed", ok=False) == ("access", "warning")
    assert classify("comm_lost", ok=False) == ("system", "warning")
    assert classify("something_new") == ("system", "info")

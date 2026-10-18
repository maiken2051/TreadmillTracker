"""State machine: table-driven scenarios. Times are seconds; the config uses the defaults."""

import pytest

from gymtracker.ble.treadmill import TreadmillState as TS
from gymtracker.config import SessionConfig
from gymtracker.state import Device, Effect, EndReason, SessionState, Transitions

S = SessionState
HRM, TM = Device.HRM, Device.TREADMILL
CFG = SessionConfig()
WALK = 1.0  # m/s, 3.6 km/h


def run(steps):
    """steps: (t, method, *args). Returns the machine and all effects with their times."""
    m = Transitions(SessionConfig())
    effects = []
    for t, method, *args in steps:
        for e in getattr(m, method)(t, *args):
            effects.append((t, e))
    return m, effects


def connected_ready(t=0):
    return [(t, "device_seen", TM), (t, "device_connected", TM)]


def speeds(t_from, t_to, v):
    return [(t, "speed", v) for t in range(t_from, t_to)]


def test_seen_then_connected_goes_ready():
    m, _ = run([(0, "device_seen", HRM)])
    assert m.state is S.WAKING
    m, _ = run([(0, "device_seen", HRM), (1, "device_connected", HRM)])
    assert m.state is S.READY


def test_waking_times_out():
    m, _ = run([(0, "device_seen", TM), (59, "tick"), ])
    assert m.state is S.WAKING
    m, _ = run([(0, "device_seen", TM), (60, "tick")])
    assert m.state is S.IDLE


def test_ready_times_out_without_start():
    m, _ = run([(0, "device_seen", HRM), (0, "device_connected", HRM), (599, "tick")])
    assert m.state is S.READY
    m, _ = run([(0, "device_seen", HRM), (0, "device_connected", HRM), (600, "tick")])
    assert m.state is S.IDLE


def test_treadmill_connect_restarts_ready_timeout():
    m, _ = run([(0, "device_seen", HRM), (0, "device_connected", HRM),
                (500, "device_connected", TM), (700, "tick")])
    assert m.state is S.READY


def test_ready_loses_all_devices_goes_waking():
    m, _ = run(connected_ready() + [(5, "device_disconnected", TM)])
    assert m.state is S.WAKING


def test_start_by_state_event():
    m, eff = run(connected_ready() + [(5, "treadmill_state", TS.RUNNING)])
    assert m.state is S.RECORDING
    assert eff == [(5, Effect.START)]


def test_start_by_speed_fallback():
    m, eff = run(connected_ready() + speeds(10, 14, WALK))
    assert eff == [(13, Effect.START)]  # held for start_hold_s = 3
    assert m.state is S.RECORDING


def test_slow_belt_does_not_start():
    m, eff = run(connected_ready() + speeds(10, 30, 0.1))  # 0.36 km/h
    assert eff == []
    assert m.state is S.READY


def test_pause_resume_and_end_by_state_events():
    m, eff = run(connected_ready() + [
        (5, "treadmill_state", TS.RUNNING),
        (100, "treadmill_state", TS.PAUSED_OR_STOPPED),
        (110, "treadmill_state", TS.RUNNING),
        (200, "treadmill_state", TS.PAUSED_OR_STOPPED),
        (207, "treadmill_state", TS.ENDED),
    ])
    assert [e for _, e in eff] == [Effect.START, Effect.PAUSE, Effect.RESUME, Effect.PAUSE,
                                   Effect.SAVE]
    assert m.state is S.FINISHED
    assert m.end_reason is EndReason.CONSOLE_ENDED
    assert m.active_s == pytest.approx(95 + 90)


def test_coast_down_after_stop_does_not_resume():
    # After Stop (0x02) the belt slows over ~10 s; speed must not count as a resume.
    m, eff = run(connected_ready() + [(5, "treadmill_state", TS.RUNNING)] + speeds(6, 100, WALK)
                 + [(100, "treadmill_state", TS.PAUSED_OR_STOPPED)] + speeds(100, 110, WALK))
    assert m.state is S.PAUSED
    assert [e for _, e in eff] == [Effect.START, Effect.PAUSE]


def test_autopause_and_resume_by_speed_fallback():
    m, eff = run(connected_ready() + speeds(0, 100, WALK) + speeds(100, 115, 0.0)
                 + speeds(115, 120, WALK))
    assert [(t, e) for t, e in eff] == [(3, Effect.START), (110, Effect.PAUSE),
                                        (118, Effect.RESUME)]


def test_autopause_by_tick_when_speed_stays_zero():
    m, eff = run(connected_ready() + speeds(0, 100, WALK) + [(100, "speed", 0.0), (111, "tick")])
    assert m.state is S.PAUSED


def test_end_by_pause_timeout():
    m, eff = run(connected_ready() + [(0, "treadmill_state", TS.RUNNING),
                                      (100, "treadmill_state", TS.PAUSED_OR_STOPPED),
                                      (279, "tick")])
    assert m.state is S.PAUSED
    m, eff = run(connected_ready() + [(0, "treadmill_state", TS.RUNNING),
                                      (100, "treadmill_state", TS.PAUSED_OR_STOPPED),
                                      (280, "tick")])
    assert m.state is S.FINISHED
    assert m.end_reason is EndReason.PAUSE_TIMEOUT
    assert eff[-1] == (280, Effect.SAVE)


def test_end_by_treadmill_disconnect():
    m, eff = run(connected_ready() + [(0, "treadmill_state", TS.RUNNING),
                                      (300, "device_disconnected", TM)])
    assert m.end_reason is EndReason.DISCONNECTED
    assert eff[-1] == (300, Effect.SAVE)


def test_hrm_disconnect_does_not_end():
    m, _ = run(connected_ready() + [(0, "device_connected", HRM),
                                    (0, "treadmill_state", TS.RUNNING),
                                    (300, "device_disconnected", HRM)])
    assert m.state is S.RECORDING


def test_end_by_user():
    m, eff = run(connected_ready() + [(0, "treadmill_state", TS.RUNNING), (300, "user_end")])
    assert m.end_reason is EndReason.USER
    assert eff[-1] == (300, Effect.SAVE)


def test_short_session_is_discarded():
    m, eff = run(connected_ready() + [(0, "treadmill_state", TS.RUNNING),
                                      (59, "treadmill_state", TS.ENDED)])
    assert m.state is S.FINISHED
    assert m.too_short and not m.saved
    assert eff[-1] == (59, Effect.DISCARD)


def test_pauses_do_not_count_toward_minimum():
    m, eff = run(connected_ready() + [(0, "treadmill_state", TS.RUNNING),
                                      (30, "treadmill_state", TS.PAUSED_OR_STOPPED),
                                      (200, "treadmill_state", TS.RUNNING),
                                      (229, "treadmill_state", TS.ENDED)])
    assert eff[-1] == (229, Effect.DISCARD)


def test_finished_returns_to_idle():
    base = connected_ready() + [(0, "treadmill_state", TS.RUNNING),
                                (100, "treadmill_state", TS.ENDED)]
    m, _ = run(base + [(399, "tick")])
    assert m.state is S.FINISHED
    m, _ = run(base + [(400, "tick")])
    assert m.state is S.IDLE
    m, eff = run(base + [(150, "user_done")])
    assert m.state is S.IDLE and eff[-1] == (100, Effect.SAVE)


def test_user_discard_after_save():
    m, eff = run(connected_ready() + [(0, "treadmill_state", TS.RUNNING),
                                      (100, "treadmill_state", TS.ENDED),
                                      (110, "user_discard")])
    assert m.state is S.IDLE
    assert eff[-1] == (110, Effect.DISCARD)


def test_next_workout_from_finished():
    m, eff = run(connected_ready() + [(0, "treadmill_state", TS.RUNNING),
                                      (100, "treadmill_state", TS.ENDED),
                                      (101, "device_disconnected", TM),
                                      (150, "device_connected", TM),
                                      (160, "treadmill_state", TS.RUNNING)])
    assert m.state is S.RECORDING
    assert [e for _, e in eff] == [Effect.START, Effect.SAVE, Effect.START]


def test_unknown_state_is_ignored():
    m, eff = run(connected_ready() + [(5, "treadmill_state", TS.UNKNOWN)] + speeds(6, 10, WALK))
    assert eff == [(9, Effect.START)]  # speed fallback still active


def test_on_change_callback():
    changes = []
    m = Transitions(SessionConfig(), on_change=lambda t, a, b: changes.append((t, a, b)))
    m.device_seen(1, TM)
    m.device_connected(2, TM)
    assert changes == [(1, S.IDLE, S.WAKING), (2, S.WAKING, S.READY)]

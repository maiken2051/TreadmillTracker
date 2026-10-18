"""Recorder driven by the real captures, end to end without Bluetooth."""

from pathlib import Path

from gymtracker import units
from gymtracker.config import SessionConfig
from gymtracker.recorder import Recorder
from gymtracker.sim.replay import read_fixture, run_recorder, summarize
from gymtracker.state import EndReason, SessionState

FIXTURES = Path(__file__).parent / "fixtures"


def replay(name, **cfg):
    saved, discarded, checkpoints = [], [], []
    rec = Recorder(SessionConfig(**cfg), on_save=saved.append, on_discard=discarded.append,
                   on_checkpoint=checkpoints.append)
    run_recorder(read_fixture(FIXTURES / name), rec)
    return rec, saved, discarded, checkpoints


def test_walk_gives_one_session_with_one_pause():
    rec, saved, discarded, checkpoints = replay("treadmill_walk_5min.jsonl")
    assert len(saved) == 1 and not discarded
    s = saved[0].summary()
    assert s.pause_count == 1
    assert s.moving_s == 156  # console elapsed 2:36
    assert s.distance_m == 128
    assert s.kcal == 9
    assert rec.machine.end_reason is EndReason.CONSOLE_ENDED
    assert rec.state is SessionState.FINISHED
    assert len(checkpoints) == 5  # every 30 s over ~173 s


def test_calibration_walk_matches_console():
    _, saved, _, _ = replay("treadmill_calib.jsonl")
    s = saved[0].summary()
    assert (s.moving_s, s.distance_m, s.kcal) == (308, 418, 29)  # console 5:08 / 0.26 mi / 29
    assert round(units.m_to_miles(s.distance_m), 2) == 0.26
    assert s.pause_count == 0
    assert s.max_incline_pct == 5.0
    assert round(units.mps_to_mph(s.max_speed_mps), 1) == 4.0


def test_combined_capture_merges_heart_rate():
    rec, saved, _, _ = replay("combined_3min_b.jsonl")
    sm = saved[0].summary()
    assert 84 <= sm.min_hr <= sm.avg_hr <= sm.max_hr <= 94
    hr_seconds = sum(1 for r in saved[0].records if r.hr is not None)
    assert hr_seconds >= len(saved[0].records) - 10
    assert rec.machine.end_reason is EndReason.DISCONNECTED


def test_strap_alone_never_starts_a_session():
    rec, saved, discarded, _ = replay("hrm_worn_3min.jsonl")
    assert not saved and not discarded and rec.session is None


def test_short_minimum_discards():
    _, saved, discarded, _ = replay("treadmill_walk_5min.jsonl", min_session_s=200)
    assert not saved and len(discarded) == 1


def test_summarize_output():
    lines = list(summarize(read_fixture(FIXTURES / "treadmill_calib.jsonl")))
    assert any("ready -> recording" in line for line in lines)
    assert any(line.strip().startswith("337.1s  saved: moving 5:08, 0.26 mi, 29 kcal")
               for line in lines)
    assert lines[-1] == "end reason: console ended"

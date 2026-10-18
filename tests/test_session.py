"""Session: 1 Hz records, staleness, pauses, summary, checkpoints."""

import pytest

from gymtracker.ble.hrm import HrSample
from gymtracker.ble.treadmill import InclineSample, TreadmillSample
from gymtracker.session import Session


def tm(elapsed, speed, dist, kcal=0):
    return TreadmillSample(elapsed_s=elapsed, speed_mps=speed, distance_m=dist, kcal=kcal,
                           mets=3.0)


def test_one_record_per_second_with_latest_values():
    s = Session(start_t=1000.0)
    s.add_hr(1000.2, HrSample(90))
    s.add_treadmill(1000.5, tm(0, 1.0, 0))
    s.add_hr(1001.3, HrSample(91))
    s.add_treadmill(1002.5, InclineSample(2.0))
    s.tick(1003.0)
    assert [r.t_s for r in s.records] == [0, 1, 2]
    assert [r.hr for r in s.records] == [90, 91, 91]
    assert [r.incline_pct for r in s.records] == [None, None, 2.0]
    assert s.records[0].speed_mps == 1.0


def test_stale_hr_and_speed_become_gaps():
    s = Session(start_t=0.0)
    s.add_hr(0.5, HrSample(100))
    s.add_treadmill(0.5, tm(1, 1.0, 10))
    s.tick(10.0)
    assert [r.hr for r in s.records][:6] == [100, 100, 100, 100, 100, None]
    assert s.records[-1].speed_mps is None
    assert s.records[-1].distance_m == 10  # cumulative counters carry forward


def test_off_skin_is_a_gap():
    s = Session(start_t=0.0)
    s.add_hr(0.5, HrSample(None))
    s.tick(1.0)
    assert s.records[0].hr is None


def test_pauses_flag_records_and_count_only_resumed_pauses():
    s = Session(start_t=0.0)
    s.tick(2.0)
    s.pause(2.0)
    s.tick(4.0)
    s.resume(4.0)
    s.tick(6.0)
    s.pause(6.0)
    s.finish(7.5)
    assert [r.paused for r in s.records] == [False, False, True, True, False, False, True, True]
    assert s.summary().pause_count == 1
    assert s.summary().elapsed_s == 7.5


def test_finish_is_idempotent_and_freezes_records():
    s = Session(start_t=0.0)
    s.finish(3.0)
    s.tick(10.0)
    s.finish(12.0)
    assert len(s.records) == 3 and s.end_t == 3.0


def test_summary_uses_console_counters():
    s = Session(start_t=0.0)
    for i in range(120):
        s.add_hr(i + 0.1, HrSample(100 + i % 20))
        s.add_treadmill(i + 0.2, tm(i, 1.5, int(i * 1.5), kcal=i // 10))
        s.add_treadmill(i + 0.3, InclineSample(5.0 if i >= 60 else 0.0))
    s.finish(120.0)
    sm = s.summary()
    assert sm.start_utc == "1970-01-01T00:00:00+00:00"
    assert sm.moving_s == 119  # the console's elapsed time
    assert sm.distance_m == 178
    assert sm.kcal == 11
    assert sm.avg_speed_mps == pytest.approx(178 / 119)
    assert sm.max_speed_mps == 1.5
    assert (sm.min_hr, sm.max_hr) == (100, 119)
    assert sm.max_incline_pct == 5.0
    assert sm.ascent_m == pytest.approx(60 * 1.5 * 0.05, abs=0.1)


def test_summary_without_treadmill():
    s = Session(start_t=0.0)
    s.add_hr(0.5, HrSample(80))
    s.finish(5.0)
    sm = s.summary()
    assert sm.distance_m == 0 and sm.avg_speed_mps is None
    assert sm.moving_s == 5 and sm.avg_hr == 80


def test_checkpoint_round_trip(tmp_path):
    s = Session(start_t=50.0, devices={"treadmill": "Treadmill 0951"})
    s.add_hr(50.5, HrSample(88))
    s.add_treadmill(50.6, tm(1, 1.0, 1, 0))
    s.tick(55.0)
    s.pause(55.0)
    path = tmp_path / "checkpoint.json"
    s.save_checkpoint(path)
    assert not path.with_name("checkpoint.json.tmp").exists()
    r = Session.load_checkpoint(path)
    assert r.records == s.records
    assert r.pauses == s.pauses and r.paused
    assert r.devices == s.devices
    assert r.summary() == s.summary()


def test_checkpoint_version_checked():
    with pytest.raises(ValueError):
        Session.from_dict({"version": 99})

"""Replay tests: fake clients deliver parsed samples; the 1 Hz table is sane."""

import asyncio
from pathlib import Path

from gymtracker.ble.treadmill import InclineSample, TreadmillSample, TreadmillState
from gymtracker.sim.replay import (
    FakeHrmClient,
    FakeTreadmillClient,
    dispatch,
    format_table,
    read_fixture,
    replay,
    table_rows,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _collect(name):
    got = {"hr": [], "tm": [], "incline": [], "state": []}

    def on_tm(t, s):
        got["incline" if isinstance(s, InclineSample) else "tm"].append((t, s))

    hrm = FakeHrmClient(lambda t, s: got["hr"].append((t, s)))
    treadmill = FakeTreadmillClient(on_tm, lambda t, s: got["state"].append((t, s)))
    return got, hrm, treadmill


def test_combined_fixture_routes_both_devices():
    got, hrm, treadmill = _collect("combined_3min_b.jsonl")
    dispatch(read_fixture(FIXTURES / "combined_3min_b.jsonl"), hrm, treadmill)
    assert hrm.connected and treadmill.connected
    assert len(got["hr"]) == 180
    assert len(got["tm"]) == 184 and len(got["incline"]) == 184
    assert [s for _, s in got["state"]] == [TreadmillState.RUNNING]
    assert all(isinstance(s, TreadmillSample) for _, s in got["tm"])


def test_fixture_is_time_ordered():
    notes = read_fixture(FIXTURES / "combined_3min_b.jsonl")
    assert all(a.t <= b.t for a, b in zip(notes, notes[1:], strict=False))


def test_async_replay_without_waiting():
    got, hrm, treadmill = _collect("treadmill_walk_5min.jsonl")
    notes = read_fixture(FIXTURES / "treadmill_walk_5min.jsonl")
    asyncio.run(replay(notes, hrm, treadmill, speed=0))
    assert not hrm.connected
    assert [s for _, s in got["state"]][-1] is TreadmillState.ENDED


def test_table_one_row_per_second():
    notes = read_fixture(FIXTURES / "treadmill_walk_5min.jsonl")
    rows = list(table_rows(notes))
    assert [r.t_s for r in rows] == list(range(len(rows)))
    assert len(rows) == int(notes[-1].t - notes[0].t) + 1
    assert rows[-1].state is TreadmillState.ENDED
    assert rows[-1].distance_m == 128
    lines = list(format_table(rows))
    assert lines[0].split() == ["t", "state", "HR", "mph", "incl%", "miles", "kcal", "time"]
    assert len(lines) == len(rows) + 1

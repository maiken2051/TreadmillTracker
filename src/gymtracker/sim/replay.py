"""Replay recorded BLE notifications (JSONL fixtures) as fake devices.

A fixture line is {"t": epoch ms, "dev": address, "char": uuid, "data": hex}, as written by
tools/ble_spike.py and, later, by the real clients' raw logs. The fake clients parse each
notification with the same parsers as the real clients and call the same callbacks.

    python -m gymtracker.sim.replay tests/fixtures/treadmill_walk_5min.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from gymtracker import units
from gymtracker.ble.hrm import HR_MEASUREMENT_UUID, HrSample, parse_hr_measurement
from gymtracker.ble.treadmill import (
    DATA_UUID,
    STATE_UUID,
    InclineSample,
    TreadmillSample,
    TreadmillState,
    parse_data_frame,
    parse_state,
)


@dataclass(frozen=True)
class Notification:
    t: float  # epoch seconds
    dev: str
    char: str
    data: bytes


def read_fixture(path: str | Path) -> list[Notification]:
    """Load a JSONL fixture, sorted by time (combined captures interleave two devices)."""
    notes = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            notes.append(Notification(rec["t"] / 1000, rec["dev"], rec["char"].lower(),
                                      bytes.fromhex(rec["data"])))
    notes.sort(key=lambda n: n.t)
    return notes


HrCallback = Callable[[float, HrSample], None]
TreadmillCallback = Callable[[float, TreadmillSample | InclineSample], None]
StateCallback = Callable[[float, TreadmillState], None]


class FakeHrmClient:
    """Stands in for the HRM client: feeds parsed samples to on_sample(t, sample)."""

    def __init__(self, on_sample: HrCallback) -> None:
        self.on_sample = on_sample
        self.connected = False

    def feed(self, n: Notification) -> None:
        self.connected = True
        if n.char == HR_MEASUREMENT_UUID:
            sample = parse_hr_measurement(n.data)
            if sample is not None:
                self.on_sample(n.t, sample)


class FakeTreadmillClient:
    """Stands in for the treadmill client: on_sample(t, sample) and on_state(t, state)."""

    def __init__(self, on_sample: TreadmillCallback, on_state: StateCallback) -> None:
        self.on_sample = on_sample
        self.on_state = on_state
        self.connected = False

    def feed(self, n: Notification) -> None:
        self.connected = True
        if n.char == DATA_UUID:
            sample = parse_data_frame(n.data)
            if sample is not None:
                self.on_sample(n.t, sample)
        elif n.char == STATE_UUID:
            self.on_state(n.t, parse_state(n.data))


def dispatch(notes: Iterable[Notification], hrm: FakeHrmClient | None,
             treadmill: FakeTreadmillClient | None) -> None:
    """Feed every notification to the matching fake client, as fast as possible."""
    for n in notes:
        _route(n, hrm, treadmill)


async def replay(notes: list[Notification], hrm: FakeHrmClient | None,
                 treadmill: FakeTreadmillClient | None, speed: float = 1.0) -> None:
    """Feed notifications with their recorded spacing, divided by speed (0 = no waiting)."""
    prev = notes[0].t if notes else 0.0
    for n in notes:
        if speed > 0 and n.t > prev:
            await asyncio.sleep((n.t - prev) / speed)
        prev = n.t
        _route(n, hrm, treadmill)


def _route(n: Notification, hrm: FakeHrmClient | None,
           treadmill: FakeTreadmillClient | None) -> None:
    if n.char == HR_MEASUREMENT_UUID:
        if hrm is not None:
            hrm.feed(n)
    elif n.char in (DATA_UUID, STATE_UUID):
        if treadmill is not None:
            treadmill.feed(n)


# --------------------------------------------------------------------------- 1 Hz table


@dataclass
class Row:
    t_s: int  # seconds since the first notification
    hr: int | None = None
    speed_mps: float | None = None
    incline_pct: float | None = None
    distance_m: int | None = None
    kcal: int | None = None
    elapsed_s: int | None = None
    state: TreadmillState | None = None


def table_rows(notes: list[Notification]) -> Iterator[Row]:
    """One row per second: the latest value of each field as of the end of that second."""
    if not notes:
        return
    t0 = notes[0].t
    cur = Row(0)
    seen_second = 0

    def on_hr(_t: float, s: HrSample) -> None:
        cur.hr = s.bpm

    def on_tm(_t: float, s: TreadmillSample | InclineSample) -> None:
        if isinstance(s, InclineSample):
            cur.incline_pct = s.incline_pct
        else:
            cur.speed_mps, cur.distance_m = s.speed_mps, s.distance_m
            cur.kcal, cur.elapsed_s = s.kcal, s.elapsed_s

    def on_state(_t: float, s: TreadmillState) -> None:
        cur.state = s

    hrm = FakeHrmClient(on_hr)
    treadmill = FakeTreadmillClient(on_tm, on_state)
    for n in notes:
        second = int(n.t - t0)
        while seen_second < second:
            yield Row(**{**cur.__dict__, "t_s": seen_second})
            seen_second += 1
        _route(n, hrm, treadmill)
    yield Row(**{**cur.__dict__, "t_s": seen_second})


def format_table(rows: Iterable[Row]) -> Iterator[str]:
    yield "    t  state      HR   mph incl%  miles kcal   time"
    for r in rows:
        state = r.state.name.split("_")[0].lower() if r.state else ""
        hr = "" if r.hr is None else str(r.hr)
        mph = "" if r.speed_mps is None else f"{units.mps_to_mph(r.speed_mps):.1f}"
        incl = "" if r.incline_pct is None else f"{r.incline_pct:.1f}"
        miles = "" if r.distance_m is None else f"{units.m_to_miles(r.distance_m):.2f}"
        kcal = "" if r.kcal is None else str(r.kcal)
        elapsed = "" if r.elapsed_s is None else f"{r.elapsed_s // 60}:{r.elapsed_s % 60:02d}"
        yield f"{r.t_s:>5}  {state:<8} {hr:>4} {mph:>5} {incl:>5} {miles:>6} {kcal:>4} {elapsed:>6}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Print a 1 Hz table from a JSONL capture.")
    ap.add_argument("fixture", type=Path)
    args = ap.parse_args(argv)
    for line in format_table(table_rows(read_fixture(args.fixture))):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())

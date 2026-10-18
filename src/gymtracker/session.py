"""One workout: 1 Hz records merged from the strap and the treadmill, pauses, and totals.

Record k covers second k of the session and holds the latest value of each field as of the
end of that second. Heart rate and speed older than STALE_S are left blank (a dropout is a
gap, not a zero). Distance, calories and elapsed time are the console's own cumulative
counters (DESIGN.md §4.4), so they are carried forward.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from gymtracker.ble.hrm import HrSample
from gymtracker.ble.treadmill import InclineSample, TreadmillSample

STALE_S = 5.0
CHECKPOINT_VERSION = 1
# "Min speed" is the lowest speed held for MIN_SPEED_HOLD_S, ignoring anything under
# MIN_SPEED_FLOOR_MPS; a plain minimum is just the belt ramping up (user, 2026-10-18).
MIN_SPEED_HOLD_S = 60
# 2 mph less half a 0.1 mph step: the console sends 2.0 mph as 3.21 km/h, a hair under.
MIN_SPEED_FLOOR_MPS = 1.95 * 1609.344 / 3600
_SPEED_BIN_MPS = 0.02  # finer than one treadmill step (0.1 mph = 0.045 m/s)


@dataclass
class Record:
    t_s: int  # seconds since the session start
    hr: int | None = None
    speed_mps: float | None = None
    distance_m: float | None = None
    incline_pct: float | None = None
    kcal: float | None = None
    paused: bool = False


@dataclass
class Summary:
    start_utc: str
    elapsed_s: float  # wall time, start to end
    moving_s: float  # not paused; the console's own elapsed time when available
    distance_m: float
    ascent_m: float
    kcal: float
    avg_speed_mps: float | None
    max_speed_mps: float | None
    avg_incline_pct: float | None
    max_incline_pct: float | None
    avg_hr: int | None
    max_hr: int | None
    min_hr: int | None
    pause_count: int
    min_speed_mps: float | None = None  # added in schema 2; see held_min_speed()


def held_min_speed(records: list[Record]) -> float | None:
    """The lowest speed of at least MIN_SPEED_FLOOR_MPS kept for MIN_SPEED_HOLD_S
    consecutive moving seconds, or None."""
    best = None
    run: list[float] = []
    prev_t = None
    for r in records:
        v = r.speed_mps
        if (run and v is not None and prev_t == r.t_s - 1 and not r.paused
                and round(v / _SPEED_BIN_MPS) == round(run[0] / _SPEED_BIN_MPS)):
            run.append(v)
        elif v is not None and v >= MIN_SPEED_FLOOR_MPS - 1e-9 and not r.paused:
            run = [v]
        else:
            run = []
        prev_t = r.t_s
        if len(run) >= MIN_SPEED_HOLD_S:
            speed = sum(run) / len(run)
            best = speed if best is None else min(best, speed)
    return best


def summarize(start_t: float, end_t: float, records: list[Record], *,
              moving_s: float | None = None, distance_m: float | None = None,
              kcal: float | None = None, pause_count: int = 0) -> Summary:
    """Totals for a workout. moving_s, distance_m and kcal are the device's own totals when
    known; otherwise they come from the records (non-paused seconds, last values)."""
    moving = [r for r in records if not r.paused]
    hrs = [r.hr for r in moving if r.hr is not None]
    speeds = [r.speed_mps for r in moving if r.speed_mps is not None]
    inclines = [r.incline_pct for r in moving if r.incline_pct is not None]
    if moving_s is None:
        moving_s = float(len(moving))
    has_distance = distance_m is not None or any(r.distance_m is not None for r in records)
    if distance_m is None:
        distance_m = next((r.distance_m for r in reversed(records)
                           if r.distance_m is not None), 0.0)
    if kcal is None:
        kcal = next((r.kcal for r in reversed(records) if r.kcal is not None), 0.0)
    return Summary(
        start_utc=datetime.fromtimestamp(start_t, UTC).isoformat(timespec="seconds"),
        elapsed_s=round(end_t - start_t, 1),
        moving_s=moving_s,
        distance_m=distance_m,
        ascent_m=round(sum((r.speed_mps or 0) * (r.incline_pct or 0) / 100 for r in moving), 1),
        kcal=kcal,
        avg_speed_mps=distance_m / moving_s if moving_s and has_distance else None,
        max_speed_mps=max(speeds) if speeds else None,
        avg_incline_pct=round(sum(inclines) / len(inclines), 1) if inclines else None,
        max_incline_pct=max(inclines) if inclines else None,
        avg_hr=round(sum(hrs) / len(hrs)) if hrs else None,
        max_hr=max(hrs) if hrs else None,
        min_hr=min(hrs) if hrs else None,
        pause_count=pause_count,
        min_speed_mps=held_min_speed(records),
    )


@dataclass
class Session:
    start_t: float  # epoch seconds
    end_t: float | None = None
    records: list[Record] = field(default_factory=list)
    pauses: list[list[float | None]] = field(default_factory=list)  # [start, end or None]
    devices: dict[str, str] = field(default_factory=dict)  # "hrm"/"treadmill" -> name
    _hr: tuple[float, int | None] | None = field(default=None, repr=False)
    _speed: tuple[float, float] | None = field(default=None, repr=False)
    _treadmill: TreadmillSample | None = field(default=None, repr=False)
    _incline: float | None = field(default=None, repr=False)

    # ------------------------------------------------------------------ input

    def add_hr(self, t: float, sample: HrSample) -> None:
        self._advance(t)
        self._hr = (t, sample.bpm)

    def add_treadmill(self, t: float, sample: TreadmillSample | InclineSample) -> None:
        self._advance(t)
        if isinstance(sample, InclineSample):
            self._incline = sample.incline_pct
        else:
            self._treadmill = sample
            self._speed = (t, sample.speed_mps)

    def pause(self, t: float) -> None:
        self._advance(t)
        if not self.paused:
            self.pauses.append([t, None])

    def resume(self, t: float) -> None:
        self._advance(t)
        if self.paused:
            self.pauses[-1][1] = t

    def tick(self, t: float) -> None:
        self._advance(t)

    def finish(self, t: float) -> None:
        if self.end_t is not None:
            return
        self._advance(t)
        if t > self.start_t + len(self.records):
            self.records.append(self._record(len(self.records), t))
        self.end_t = t

    @property
    def paused(self) -> bool:
        return bool(self.pauses) and self.pauses[-1][1] is None

    # ------------------------------------------------------------------ output

    def summary(self) -> Summary:
        last = self._treadmill
        end = self.end_t if self.end_t is not None else self.start_t + len(self.records)
        return summarize(
            self.start_t, end, self.records,
            moving_s=float(last.elapsed_s) if last and last.elapsed_s else None,
            distance_m=float(last.distance_m) if last else None,
            kcal=float(last.kcal) if last else None,
            pause_count=sum(1 for _, end_t in self.pauses if end_t is not None),
        )

    # ------------------------------------------------------------------ checkpoint

    def to_dict(self) -> dict:
        return {
            "version": CHECKPOINT_VERSION,
            "start_t": self.start_t,
            "end_t": self.end_t,
            "records": [asdict(r) for r in self.records],
            "pauses": self.pauses,
            "devices": self.devices,
            "treadmill": asdict(self._treadmill) if self._treadmill else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Session:
        if d.get("version") != CHECKPOINT_VERSION:
            raise ValueError(f"unsupported checkpoint version {d.get('version')}")
        s = cls(start_t=d["start_t"], end_t=d["end_t"],
                records=[Record(**r) for r in d["records"]],
                pauses=[list(p) for p in d["pauses"]], devices=dict(d["devices"]))
        if d["treadmill"]:
            s._treadmill = TreadmillSample(**d["treadmill"])
        return s

    def save_checkpoint(self, path: str | Path) -> None:
        """Write atomically: a power cut leaves the old checkpoint or the new one."""
        path = Path(path)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(self.to_dict()), encoding="utf-8")
        os.replace(tmp, path)

    @classmethod
    def load_checkpoint(cls, path: str | Path) -> Session:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    # ------------------------------------------------------------------ internals

    def _advance(self, t: float) -> None:
        """Emit the records for every second that ended before t."""
        if self.end_t is not None:
            return
        while self.start_t + len(self.records) + 1 <= t:
            k = len(self.records)
            self.records.append(self._record(k, self.start_t + k + 1))

    def _record(self, k: int, at: float) -> Record:
        tm = self._treadmill
        return Record(
            t_s=k,
            hr=self._hr[1] if self._hr and at - self._hr[0] <= STALE_S else None,
            speed_mps=self._speed[1] if self._speed and at - self._speed[0] <= STALE_S else None,
            distance_m=float(tm.distance_m) if tm else None,
            incline_pct=self._incline,
            kcal=float(tm.kcal) if tm else None,
            paused=self.paused,
        )

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
        moving = [r for r in self.records if not r.paused]
        hrs = [r.hr for r in moving if r.hr is not None]
        speeds = [r.speed_mps for r in moving if r.speed_mps is not None]
        inclines = [r.incline_pct for r in moving if r.incline_pct is not None]
        last = self._treadmill
        moving_s = float(last.elapsed_s) if last and last.elapsed_s else float(len(moving))
        distance = float(last.distance_m) if last else 0.0
        end = self.end_t if self.end_t is not None else self.start_t + len(self.records)
        return Summary(
            start_utc=datetime.fromtimestamp(self.start_t, UTC).isoformat(timespec="seconds"),
            elapsed_s=round(end - self.start_t, 1),
            moving_s=moving_s,
            distance_m=distance,
            ascent_m=round(sum((r.speed_mps or 0) * (r.incline_pct or 0) / 100
                               for r in moving), 1),
            kcal=float(last.kcal) if last else 0.0,
            avg_speed_mps=distance / moving_s if moving_s and last else None,
            max_speed_mps=max(speeds) if speeds else None,
            avg_incline_pct=round(sum(inclines) / len(inclines), 1) if inclines else None,
            max_incline_pct=max(inclines) if inclines else None,
            avg_hr=round(sum(hrs) / len(hrs)) if hrs else None,
            max_hr=max(hrs) if hrs else None,
            min_hr=min(hrs) if hrs else None,
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

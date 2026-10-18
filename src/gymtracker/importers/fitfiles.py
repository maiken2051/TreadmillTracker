"""Import .fit activity files (e.g. old GymTrakr or Garmin workouts) as workouts.

Needs the [fit] extra (garmin-fit-sdk). Records are collapsed to 1 Hz (GymTrakr wrote about
four per second; the last one in each second wins); heart rate 0 means no reading. The
file's own session totals are used for moving time, distance and calories, because
GymTrakr's records have gaps during pauses and the session time excludes them.
"""

from __future__ import annotations

from pathlib import Path

from gymtracker.session import Record, summarize
from gymtracker.store import Workout, local_offset_min


class FitImportError(Exception):
    pass


def read_fit(path: str | Path, source: str) -> tuple[Workout, list[Record]]:
    """Decode one .fit activity into a Workout (not yet saved) and its 1 Hz records."""
    try:
        from garmin_fit_sdk import Decoder, Stream
    except ImportError as e:
        raise FitImportError("importing .fit files needs: pip install gymtracker[fit]") from e

    messages, errors = Decoder(Stream.from_file(str(path))).read()
    if errors:
        raise FitImportError(f"{path}: {errors[0]}")
    sessions = messages.get("session_mesgs", [])
    if len(sessions) != 1:
        raise FitImportError(f"{path}: expected one session, found {len(sessions)}")
    session = sessions[0]
    start_t = session["start_time"].timestamp()

    by_second: dict[int, dict] = {}
    for m in messages.get("record_mesgs", []):
        if "timestamp" in m:
            by_second[int(m["timestamp"].timestamp() - start_t)] = m
    records = [_record(t_s, m) for t_s, m in sorted(by_second.items()) if t_s >= 0]
    if not records:
        raise FitImportError(f"{path}: no records")

    end_t = max(start_t + records[-1].t_s + 1, session["timestamp"].timestamp())
    moving_s = session.get("total_timer_time") or session.get("total_elapsed_time")
    summary = summarize(
        start_t, end_t, records,
        moving_s=float(moving_s) if moving_s else None,
        distance_m=_float(session.get("total_distance")),
        kcal=_float(session.get("total_calories")),
    )
    workout = Workout(summary=summary, utc_offset_min=local_offset_min(start_t),
                      source=source, devices={"file": Path(path).name})
    return workout, records


def _record(t_s: int, m: dict) -> Record:
    speed = m.get("enhanced_speed", m.get("speed"))
    return Record(
        t_s=t_s,
        hr=m.get("heart_rate") or None,
        speed_mps=_float(speed),
        distance_m=_float(m.get("distance")),
        incline_pct=_float(m.get("grade")),
        kcal=_float(m.get("calories")),
        paused=False,
    )


def _float(v) -> float | None:
    return None if v is None else float(v)


def find_fit_files(folder: str | Path) -> list[Path]:
    return sorted(p for p in Path(folder).iterdir() if p.suffix.lower() == ".fit")

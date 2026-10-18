"""Per-workout archive files (DESIGN.md §4.5): archive/YYYY/YYYY-MM-DD_HH-MM_treadmill.json.gz.

Written once, never changed, and complete enough to rebuild the database: the workout row,
the 1 Hz samples, and (for workouts recorded by the Pi) the raw BLE notifications, so a
parser fix can be applied to old workouts.
"""

from __future__ import annotations

import dataclasses
import gzip
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from gymtracker.session import Record, Summary
from gymtracker.store import Workout

FORMAT_VERSION = 1
_RECORD_FIELDS = [f.name for f in dataclasses.fields(Record)]


@dataclass
class Archived:
    workout: Workout
    records: list[Record]
    raw: list[dict] = field(default_factory=list)  # fixture-format lines: t, dev, char, data


def archive_name(workout: Workout) -> Path:
    """Relative path, from the workout's local start time."""
    start = workout.start_local
    return Path(f"{start:%Y}") / f"{start:%Y-%m-%d_%H-%M}_treadmill.json.gz"


def write_archive(root: str | Path, workout: Workout, records: list[Record],
                  raw: list[dict] | None = None) -> Path:
    """Write a new archive file and return its path. Never overwrites: if the name is
    taken (two workouts starting in the same minute), a suffix is added."""
    first = Path(root) / archive_name(workout)
    path, n = first, 1
    while path.exists():
        n += 1
        path = first.with_name(first.name.replace("_treadmill", f"_treadmill-{n}"))
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "format": "gymtracker-workout",
        "version": FORMAT_VERSION,
        "workout": {
            "summary": dataclasses.asdict(workout.summary),
            "utc_offset_min": workout.utc_offset_min,
            "source": workout.source,
            "devices": workout.devices,
            "notes": workout.notes,
            "created_utc": workout.created_utc,
        },
        "record_fields": _RECORD_FIELDS,
        "records": [[getattr(r, f) for f in _RECORD_FIELDS] for r in records],
        "raw": raw or [],
    }
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"))
    os.replace(tmp, path)
    return path


def read_archive(path: str | Path) -> Archived:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        doc = json.load(f)
    if doc.get("format") != "gymtracker-workout" or doc.get("version") != FORMAT_VERSION:
        raise ValueError(f"{path}: not a version {FORMAT_VERSION} workout archive")
    w = doc["workout"]
    workout = Workout(summary=Summary(**w["summary"]), utc_offset_min=w["utc_offset_min"],
                      source=w["source"], devices=w["devices"], notes=w["notes"],
                      created_utc=w["created_utc"])
    fields = doc["record_fields"]
    records = [Record(**dict(zip(fields, row, strict=True))) for row in doc["records"]]
    return Archived(workout, records, doc["raw"])


def iter_archives(root: str | Path) -> list[Path]:
    return sorted(Path(root).rglob("*.json.gz"))

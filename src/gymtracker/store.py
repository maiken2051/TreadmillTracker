"""SQLite storage (DESIGN.md §4.5): the source of truth, on the Pi's local disk.

All values are SI. Times are UTC ISO 8601 plus the local UTC offset at the start of the
workout, so local times display correctly without a timezone database.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from gymtracker.session import Record, Summary

SCHEMA_VERSION = 1

_MIGRATIONS = {
    1: """
    CREATE TABLE workouts (
        id INTEGER PRIMARY KEY,
        start_utc TEXT NOT NULL UNIQUE,
        utc_offset_min INTEGER NOT NULL,
        elapsed_s REAL NOT NULL,
        moving_s REAL NOT NULL,
        distance_m REAL NOT NULL,
        ascent_m REAL NOT NULL,
        kcal REAL NOT NULL,
        avg_speed_mps REAL,
        max_speed_mps REAL,
        avg_incline_pct REAL,
        max_incline_pct REAL,
        avg_hr INTEGER,
        max_hr INTEGER,
        min_hr INTEGER,
        pause_count INTEGER NOT NULL,
        source TEXT NOT NULL,
        devices TEXT NOT NULL,
        notes TEXT NOT NULL DEFAULT '',
        archive TEXT NOT NULL DEFAULT '',  -- archive file, relative to the archive root
        created_utc TEXT NOT NULL
    );
    CREATE TABLE samples (
        workout_id INTEGER NOT NULL REFERENCES workouts(id) ON DELETE CASCADE,
        t_s INTEGER NOT NULL,
        hr INTEGER,
        speed_mps REAL,
        distance_m REAL,
        incline_pct REAL,
        kcal REAL,
        paused INTEGER NOT NULL,
        PRIMARY KEY (workout_id, t_s)
    ) WITHOUT ROWID;
    """,
}

_SUMMARY_FIELDS = [f.name for f in dataclasses.fields(Summary)]
_META_FIELDS = ["utc_offset_min", "source", "devices", "notes", "archive", "created_utc"]


class DuplicateWorkout(Exception):
    """A workout with the same start time is already stored."""


@dataclass
class Workout:
    summary: Summary
    utc_offset_min: int
    source: str  # "pi" or "import:<label>"
    devices: dict[str, str] = field(default_factory=dict)
    notes: str = ""
    id: int | None = None
    created_utc: str = ""
    archive: str = ""  # archive file, relative to the archive root

    @property
    def start_local(self) -> datetime:
        """Start time in the local time of the workout (fixed offset)."""
        start = datetime.fromisoformat(self.summary.start_utc)
        return start.astimezone(timezone(timedelta(minutes=self.utc_offset_min)))


def local_offset_min(t: float) -> int:
    """This machine's UTC offset, in minutes, at epoch time t (handles DST)."""
    offset = datetime.fromtimestamp(t).astimezone().utcoffset()
    return int(offset.total_seconds() // 60)


def now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA foreign_keys = ON")
        if str(path) != ":memory:":
            self.db.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _migrate(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"{self.path} has schema {version}, newer than this code "
                               f"({SCHEMA_VERSION})")
        for v in range(version + 1, SCHEMA_VERSION + 1):
            with self.db:
                self.db.executescript(_MIGRATIONS[v])
                self.db.execute(f"PRAGMA user_version = {v}")

    # ------------------------------------------------------------------ writes

    def save(self, workout: Workout, records: list[Record]) -> int:
        """Insert a workout and its samples; returns its id. Raises DuplicateWorkout."""
        s = workout.summary
        if self.find(s.start_utc) is not None:
            raise DuplicateWorkout(s.start_utc)
        created = workout.created_utc or now_utc()
        cols = _SUMMARY_FIELDS + _META_FIELDS
        values = [getattr(s, f) for f in _SUMMARY_FIELDS] + [
            workout.utc_offset_min, workout.source, json.dumps(workout.devices),
            workout.notes, workout.archive, created]
        with self.db:
            cur = self.db.execute(
                f"INSERT INTO workouts ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' * len(cols))})", values)
            workout_id = cur.lastrowid
            self.db.executemany(
                "INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(workout_id, r.t_s, r.hr, r.speed_mps, r.distance_m, r.incline_pct, r.kcal,
                  int(r.paused)) for r in records])
        workout.id, workout.created_utc = workout_id, created
        return workout_id

    def delete(self, workout_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM workouts WHERE id = ?", (workout_id,))

    def set_notes(self, workout_id: int, notes: str) -> None:
        with self.db:
            self.db.execute("UPDATE workouts SET notes = ? WHERE id = ?", (notes, workout_id))

    # ------------------------------------------------------------------ reads

    def find(self, start_utc: str) -> int | None:
        row = self.db.execute("SELECT id FROM workouts WHERE start_utc = ?",
                              (start_utc,)).fetchone()
        return row[0] if row else None

    def get(self, workout_id: int) -> Workout | None:
        rows = self._select("WHERE id = ?", (workout_id,))
        return rows[0] if rows else None

    def list(self) -> list[Workout]:
        """All workouts, oldest first."""
        return self._select("ORDER BY start_utc", ())

    def samples(self, workout_id: int) -> list[Record]:
        rows = self.db.execute(
            "SELECT t_s, hr, speed_mps, distance_m, incline_pct, kcal, paused FROM samples "
            "WHERE workout_id = ? ORDER BY t_s", (workout_id,))
        return [Record(t, hr, sp, d, inc, kc, bool(p)) for t, hr, sp, d, inc, kc, p in rows]

    def snapshot_to(self, path: str | Path) -> None:
        """Consistent copy of the whole database (SQLite backup API), safe while in use."""
        dest = sqlite3.connect(path)
        try:
            self.db.backup(dest)
        finally:
            dest.close()

    def _select(self, where: str, params: tuple) -> list[Workout]:
        cols = ["id"] + _SUMMARY_FIELDS + _META_FIELDS
        rows = self.db.execute(f"SELECT {', '.join(cols)} FROM workouts {where}", params)
        out = []
        for row in rows:
            d = dict(zip(cols, row, strict=True))
            summary = Summary(**{f: d[f] for f in _SUMMARY_FIELDS})
            out.append(Workout(summary=summary, utc_offset_min=d["utc_offset_min"],
                               source=d["source"], devices=json.loads(d["devices"]),
                               notes=d["notes"], id=d["id"], created_utc=d["created_utc"],
                               archive=d["archive"]))
        return out

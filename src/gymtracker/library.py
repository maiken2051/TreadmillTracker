"""The workout library: database + archive + spreadsheet, in one data directory.

    <data_dir>/gymtracker.db
    <data_dir>/archive/YYYY/*.json.gz   (archive/deleted/ holds discarded workouts)
    <data_dir>/Workouts.xlsx

Adding a workout writes its archive file first, then the database row, so the archive is
never missing anything the database has. Everything here is local; copying to the NAS is
export.py (Phase 5).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from gymtracker.archive import iter_archives, read_archive, write_archive
from gymtracker.config import SessionConfig
from gymtracker.session import Record, Session
from gymtracker.sheet import write_workbook
from gymtracker.store import DuplicateWorkout, Store, Workout, local_offset_min, now_utc

log = logging.getLogger(__name__)

DB_NAME = "gymtracker.db"
ARCHIVE_DIR = "archive"
DELETED_DIR = "deleted"


@dataclass
class ImportResult:
    added: list[Workout] = field(default_factory=list)
    duplicates: list[Path] = field(default_factory=list)
    failed: list[tuple[Path, str]] = field(default_factory=list)


class Library:
    def __init__(self, data_dir: str | Path, unit_system: str = "imperial",
                 spreadsheet: str = "Workouts.xlsx") -> None:
        self.data_dir = Path(data_dir).expanduser()
        self.archive_dir = self.data_dir / ARCHIVE_DIR
        self.sheet_path = self.data_dir / spreadsheet
        self.unit_system = unit_system
        self.store = Store(self.data_dir / DB_NAME)

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> Library:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ adding

    def add(self, workout: Workout, records: list[Record], raw: list[dict] | None = None) -> int:
        """Archive and store a workout; returns its id. Raises DuplicateWorkout."""
        if self.store.find(workout.summary.start_utc) is not None:
            raise DuplicateWorkout(workout.summary.start_utc)
        workout.created_utc = workout.created_utc or now_utc()
        path = write_archive(self.archive_dir, workout, records, raw)
        workout.archive = path.relative_to(self.archive_dir).as_posix()
        return self.store.save(workout, records)

    def add_session(self, session: Session, raw: list[dict] | None = None,
                    source: str = "pi") -> int:
        workout = Workout(summary=session.summary(),
                          utc_offset_min=local_offset_min(session.start_t),
                          source=source, devices=dict(session.devices))
        return self.add(workout, session.records, raw)

    def import_fit(self, folder: str | Path, label: str) -> ImportResult:
        from gymtracker.importers.fitfiles import FitImportError, find_fit_files, read_fit
        result = ImportResult()
        for path in find_fit_files(folder):
            try:
                workout, records = read_fit(path, source=f"import:{label}")
                self.add(workout, records)
                result.added.append(workout)
            except DuplicateWorkout:
                result.duplicates.append(path)
            except FitImportError as e:
                result.failed.append((path, str(e)))
        return result

    # ------------------------------------------------------------------ changing

    def delete(self, workout_id: int) -> None:
        """Remove from the database and move the archive file aside, so a rebuild does not
        bring it back. Nothing is destroyed."""
        workout = self.store.get(workout_id)
        if workout is None:
            return
        if workout.archive:
            src = self.archive_dir / workout.archive
            if src.exists():
                dest = self.archive_dir / DELETED_DIR / workout.archive
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(src, dest)
        self.store.delete(workout_id)

    def write_sheet(self, path: str | Path | None = None) -> Path:
        return write_workbook(self.store.list(), path or self.sheet_path, self.unit_system)


def rebuild_db(archive_dir: str | Path, db_path: str | Path, reparse: bool = False,
               cfg: SessionConfig | None = None) -> tuple[int, list[tuple[Path, str]]]:
    """Create a new database from archive files. With reparse, workouts that have raw BLE
    notifications are re-derived from them (so a parser fix reaches old workouts); the rest
    use their stored samples. Returns (count, problems)."""
    archive_dir, db_path = Path(archive_dir), Path(db_path)
    if db_path.exists():
        raise FileExistsError(f"{db_path} exists; rebuild into a new file")
    problems: list[tuple[Path, str]] = []
    count = 0
    with Store(db_path) as store:
        for path in iter_archives(archive_dir):
            rel = path.relative_to(archive_dir)
            if rel.parts[0] == DELETED_DIR:
                continue
            try:
                a = read_archive(path)
            except (ValueError, OSError, KeyError, TypeError) as e:
                problems.append((path, f"unreadable: {e}"))
                continue
            workout, records = a.workout, a.records
            if reparse and a.raw:
                session = _reparse(a.raw, cfg or SessionConfig())
                if session is None:
                    problems.append((path, "reparse did not give one workout; kept samples"))
                else:
                    workout.summary, records = session.summary(), session.records
            workout.archive = rel.as_posix()
            try:
                store.save(workout, records)
                count += 1
            except DuplicateWorkout:
                problems.append((path, "duplicate start time; skipped"))
    return count, problems


def _reparse(raw: list[dict], cfg: SessionConfig) -> Session | None:
    from gymtracker.recorder import Recorder
    from gymtracker.sim.replay import Notification, run_recorder

    notes = sorted((Notification(r["t"] / 1000, r["dev"], r["char"].lower(),
                                 bytes.fromhex(r["data"])) for r in raw), key=lambda n: n.t)
    saved: list[Session] = []
    recorder = Recorder(cfg, on_save=saved.append)
    run_recorder(notes, recorder)
    return saved[0] if len(saved) == 1 else None

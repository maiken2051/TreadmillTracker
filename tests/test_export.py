"""NAS export: full sync, temp-then-rename, the .pending retry queue, a locked spreadsheet."""
import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

from gymtracker.app import App
from gymtracker.config import Config, ExportConfig
from gymtracker.display import NullBacklight
from gymtracker.export import PENDING, Exporter
from gymtracker.library import Library
from gymtracker.session import Record, summarize
from gymtracker.store import Workout

FIXTURES = Path(__file__).parent / "fixtures"


def add_workouts(data_dir, n=2):
    with Library(data_dir) as lib:
        for i in range(n):
            start = 1_760_000_000.0 + i * 86400
            records = [Record(t, 100, 1.2, 1.2 * t, 0.0, t / 10, False) for t in range(120)]
            summary = summarize(start, start + 120, records, distance_m=142.8, kcal=11.9)
            lib.add(Workout(summary=summary, utc_offset_min=-240, source="pi"), records)
        lib.write_sheet()


class Nas:
    """A temp folder standing in for the mount, which can be taken down."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.up = True

    def is_mounted(self, path: Path) -> bool:
        if not self.up:
            raise OSError(112, "Host is down")
        return True


def make_exporter(tmp_path, subdir=""):
    data, mount = tmp_path / "data", tmp_path / "nas"
    data.mkdir()
    mount.mkdir()
    nas = Nas(mount)
    cfg = ExportConfig(smb_mount=str(mount), smb_subdir=subdir)
    return Exporter(data, cfg, is_mounted=nas.is_mounted), data, nas


def nas_files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_full_sync_copies_archive_sheet_and_snapshot(tmp_path):
    exporter, data, nas = make_exporter(tmp_path, subdir="Gym Tracker")
    add_workouts(data)
    result = exporter.export()
    assert result.ok and exporter.pending is None
    target = nas.root / "Gym Tracker"
    files = nas_files(target)
    assert files == {"Workouts.xlsx", "gymtracker.db",
                     *(f"archive/{p.relative_to(data / 'archive').as_posix()}"
                       for p in (data / "archive").rglob("*.json.gz"))}
    assert len([f for f in files if f.startswith("archive/")]) == 2
    db = sqlite3.connect(target / "gymtracker.db")
    assert db.execute("SELECT count(*) FROM workouts").fetchone()[0] == 2
    db.close()


def test_second_sync_skips_archive_files_already_there(tmp_path):
    exporter, data, _ = make_exporter(tmp_path)
    add_workouts(data)
    exporter.export()
    assert exporter.export().copied == ["Workouts.xlsx", "gymtracker.db"]


def test_nas_down_queues_and_recovery_catches_up(tmp_path):
    exporter, data, nas = make_exporter(tmp_path)
    add_workouts(data)
    nas.up = False
    result = exporter.export()
    assert not result.ok and "Host is down" in result.error
    assert (data / PENDING).exists() and exporter.pending["since"]
    since = exporter.pending["since"]
    assert not exporter.export().ok
    assert exporter.pending["since"] == since  # behind since the first failure
    assert nas_files(nas.root) == set()

    nas.up = True
    assert exporter.export().ok
    assert exporter.pending is None
    assert len(nas_files(nas.root)) == 4


def test_unmounted_share_is_not_written_to(tmp_path):
    exporter, data, nas = make_exporter(tmp_path)
    exporter.is_mounted = lambda path: False  # the bare local mount point
    add_workouts(data)
    assert not exporter.export().ok
    assert nas_files(nas.root) == set()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file locking")
def test_spreadsheet_open_in_excel_is_retried(tmp_path):
    exporter, data, nas = make_exporter(tmp_path)
    add_workouts(data)
    exporter.export()
    with open(nas.root / "Workouts.xlsx", "rb"):  # Excel holds it open: no rename over it
        result = exporter.export()
        assert not result.ok and exporter.pending is not None
    assert not any(".tmp" in f for f in nas_files(nas.root))
    assert exporter.export().ok and exporter.pending is None


def test_deleted_workout_leaves_the_nas_archive_too(tmp_path):
    exporter, data, nas = make_exporter(tmp_path)
    add_workouts(data)
    exporter.export()
    with Library(data) as lib:
        rel = lib.store.list()[0].archive
        lib.delete(1)
        lib.write_sheet()
    assert exporter.export().ok
    files = nas_files(nas.root)
    assert f"archive/{rel}" not in files and f"archive/deleted/{rel}" in files


def make_app(tmp_path, nas_up=True):
    exporter, data, nas = make_exporter(tmp_path)
    nas.up = nas_up
    cfg = Config()
    cfg.storage.data_dir = str(data)
    lines = []
    app = App(cfg, backlight=NullBacklight(), out=lines.append, exporter=exporter)
    return app, lines, nas


def test_app_exports_after_saving(tmp_path):
    app, lines, nas = make_app(tmp_path)
    asyncio.run(app.run_replay(FIXTURES / "treadmill_calib.jsonl", speed=0))
    assert any("nas: up to date (3 files copied)" in line for line in lines)
    assert "Workouts.xlsx" in nas_files(nas.root)


def test_app_retries_a_pending_export(tmp_path):
    app, lines, nas = make_app(tmp_path, nas_up=False)
    asyncio.run(app.run_replay(FIXTURES / "treadmill_calib.jsonl", speed=0))
    assert any("nas: NOT copied" in line for line in lines)
    assert app.nas_pending and "nas pending" in app.status_line(0)

    nas.up = True
    app.open()  # a restart picks the pending export up
    assert app.nas_pending
    app.tick(app._next_export_retry - 1)  # not due yet
    assert app.nas_pending
    app.tick(app._next_export_retry)
    assert not app.nas_pending and any("nas: up to date" in line for line in lines)
    app.close(0)

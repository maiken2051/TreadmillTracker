"""Copy the library to the NAS: archive files, Workouts.xlsx and a database snapshot.

    <target>/archive/YYYY/*.json.gz   mirrored from <data_dir>/archive (deleted/ too)
    <target>/Workouts.xlsx
    <target>/gymtracker.db            a consistent snapshot (SQLite backup API)

<target> is [export] smb_mount / smb_subdir, a CIFS systemd automount on the Pi. Every
attempt is a full sync: archive files missing on the NAS are copied (they are write-once),
and the spreadsheet and snapshot are whole-file rebuilds, so one good attempt catches up
everything a failed one missed. Each file is written under a temporary name and renamed.

If an attempt fails (NAS down, Workouts.xlsx open in Excel), <data_dir>/.pending records it
and the app retries every 10 minutes and after the next workout. Nothing local depends on
the NAS. Attempts block on the network, so the app runs them in a thread.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from gymtracker.config import ExportConfig
from gymtracker.library import ARCHIVE_DIR, DB_NAME, DELETED_DIR
from gymtracker.store import now_utc

log = logging.getLogger(__name__)

PENDING = ".pending"
RETRY_EVERY_S = 600.0
_NETWORK_FS = {"cifs", "smb3", "smbfs"}


class NasUnavailable(OSError):
    pass


@dataclass
class ExportResult:
    ok: bool
    copied: list[str]
    error: str = ""


def is_network_mount(path: Path) -> bool:
    """True if path is (inside) a mounted SMB share. Stat first so a systemd automount
    mounts; the check then stops us writing into the empty local mount point."""
    os.stat(path)
    if not sys.platform.startswith("linux"):
        return True  # Windows/macOS: a UNC path or a test folder
    best, fstype = "", ""
    with open("/proc/self/mounts", encoding="utf-8") as f:
        for line in f:
            _, mnt, fs, *_ = line.split()
            mnt = mnt.replace("\\040", " ")
            if (str(path) == mnt or str(path).startswith(mnt.rstrip("/") + "/")) \
                    and len(mnt) > len(best):
                best, fstype = mnt, fs
    return fstype in _NETWORK_FS


class Exporter:
    def __init__(self, data_dir: str | Path, cfg: ExportConfig,
                 is_mounted: Callable[[Path], bool] = is_network_mount) -> None:
        self.data_dir = Path(data_dir).expanduser()
        self.target = Path(cfg.smb_mount) / cfg.smb_subdir if cfg.smb_subdir \
            else Path(cfg.smb_mount)
        self.mount = Path(cfg.smb_mount)
        self.spreadsheet = cfg.spreadsheet
        self.is_mounted = is_mounted
        self.pending_path = self.data_dir / PENDING
        self._lock = threading.Lock()

    @property
    def pending(self) -> dict | None:
        """{"since": utc, "error": text} while the NAS is behind, else None."""
        try:
            return json.loads(self.pending_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None

    def export(self) -> ExportResult:
        """One full sync attempt. Never raises; a failure is recorded in .pending."""
        with self._lock:
            copied: list[str] = []
            try:
                try:
                    mounted = self.is_mounted(self.mount)
                except OSError as e:
                    raise NasUnavailable(f"{self.mount}: {e.strerror or e}") from e
                if not mounted:
                    raise NasUnavailable(f"{self.mount} is not mounted")
                self.target.mkdir(exist_ok=True)
                self._sync_archive(copied)
                sheet = self.data_dir / self.spreadsheet
                if sheet.exists():
                    _copy(sheet, self.target / self.spreadsheet)
                    copied.append(self.spreadsheet)
                self._copy_snapshot()
                copied.append(DB_NAME)
            except OSError as e:
                error = f"{type(e).__name__}: {e}"
                log.warning("NAS export failed, will retry: %s", error)
                since = (self.pending or {}).get("since") or now_utc()
                self._write_pending({"since": since, "error": error})
                return ExportResult(False, copied, error)
            self.pending_path.unlink(missing_ok=True)
            return ExportResult(True, copied)

    def _sync_archive(self, copied: list[str]) -> None:
        src_root, dst_root = self.data_dir / ARCHIVE_DIR, self.target / ARCHIVE_DIR
        if not src_root.is_dir():
            return
        for src in sorted(src_root.rglob("*.json.gz")):
            rel = src.relative_to(src_root)
            dst = dst_root / rel
            if not dst.exists() or dst.stat().st_size != src.stat().st_size:
                dst.parent.mkdir(parents=True, exist_ok=True)
                _copy(src, dst)
                copied.append(f"{ARCHIVE_DIR}/{rel.as_posix()}")
            if rel.parts[0] == DELETED_DIR:
                # A discarded workout: take the NAS copy out too, so a rebuild from the
                # NAS does not bring it back.
                (dst_root / Path(*rel.parts[1:])).unlink(missing_ok=True)

    def _copy_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / DB_NAME
            src = sqlite3.connect(self.data_dir / DB_NAME)
            dest = sqlite3.connect(snap)
            try:
                src.backup(dest)
            finally:
                dest.close()
                src.close()
            _copy(snap, self.target / DB_NAME)

    def _write_pending(self, data: dict) -> None:
        tmp = self.pending_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, self.pending_path)


def _copy(src: Path, dst: Path) -> None:
    """Write dst.tmp then rename, so a half-written file never appears on the NAS."""
    tmp = dst.with_name(f".{dst.name}.tmp")
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise

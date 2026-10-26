"""The app: scanner -> clients -> Recorder -> library -> NAS, plus the backlight and a
status line.

The App has the Recorder's event methods (device_seen, hr, tick, ...) and adds the side
effects around them: the raw notification log, checkpoints, saving, the NAS export, the
backlight and the headless status output. run_ble() feeds it from the real devices;
run_replay() feeds it from a capture through the fake clients, so the whole app runs on
Windows.

Raw notifications go to <data_dir>/logs/<wake time>.jsonl (one file per wake-up, in the
fixture format, so `gymtracker --replay` can read it), and the ones since the last saved
workout go into that workout's archive file.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from gymtracker import units
from gymtracker.ble.hrm import HrSample
from gymtracker.ble.treadmill import InclineSample, TreadmillSample, TreadmillState
from gymtracker.config import Config
from gymtracker.display import Backlight, NullBacklight
from gymtracker.export import RETRY_EVERY_S, Exporter, ExportResult
from gymtracker.library import Library
from gymtracker.recorder import Recorder
from gymtracker.session import Session
from gymtracker.state import Device, SessionState

log = logging.getLogger(__name__)

LOG_DIR = "logs"
CHECKPOINT = "current-workout.json"


class App:
    def __init__(self, cfg: Config, *, backlight: Backlight | NullBacklight | None = None,
                 out: Callable[[str], None] = print, status_every_s: float = 1.0,
                 exporter: Exporter | None = None) -> None:
        self.cfg = cfg
        self.data_dir = Path(cfg.storage.data_dir).expanduser()
        self.log_dir = self.data_dir / LOG_DIR
        self.checkpoint_path = self.data_dir / CHECKPOINT
        self.backlight = backlight if backlight is not None else Backlight(cfg.display.backlight)
        self.out = out
        self.status_every_s = status_every_s
        self.exporter = exporter  # None: no NAS copy (replays, tests)
        self._export_task: asyncio.Task | None = None
        self._export_again = False
        self._next_export_retry = 0.0  # a pending export from a previous run retries at once
        self.nas_pending = False
        self.imperial = cfg.user.units == "imperial"
        self.recorder = Recorder(cfg.session, on_save=self._save, on_discard=self._discard,
                                 on_checkpoint=self._checkpoint, on_change=self._changed)
        self.library: Library | None = None
        self.raw: list[dict] = []  # notifications since waking or since the last workout
        self.saved: dict[int, int] = {}  # id(session) -> workout id
        self.hrm = None  # the BLE clients, in run_ble()
        self.treadmill_client = None
        self._log_file = None
        self._last_status = 0.0
        self._hr: int | None = None
        self._tm: TreadmillSample | None = None
        self._incline: float | None = None

    @property
    def state(self) -> SessionState:
        return self.recorder.state

    # ------------------------------------------------------------------ lifecycle

    def open(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(exist_ok=True)
        self.library = Library(self.data_dir, self.cfg.user.units, self.cfg.export.spreadsheet)
        if self.checkpoint_path.exists():
            # Recovering partial workouts is Phase 8; keep the file for it.
            kept = self.checkpoint_path.with_name(
                f"checkpoint-{datetime.now():%Y%m%d-%H%M%S}.json")
            self.checkpoint_path.rename(kept)
            log.warning("unfinished workout from a previous run kept as %s", kept)
        self.nas_pending = self.exporter is not None and self.exporter.pending is not None
        self.backlight.set(False)

    def close(self, t: float) -> None:
        """End and save a workout in progress (Ctrl-C or SIGTERM while recording)."""
        if self.recorder.recording:
            self.out("stopping: saving the workout in progress")
            self.recorder.user_end(t)
        self._close_log()
        if self.library is not None:
            self.library.close()
            self.library = None

    # ------------------------------------------------------------------ device events
    # (the same methods as Recorder, so sim.replay can drive the App)

    def device_seen(self, t: float, device: Device, name: str = "") -> None:
        self.recorder.device_seen(t, device, name)

    def device_connected(self, t: float, device: Device, name: str = "") -> None:
        self.out(f"{_clock(t)} {device.value} connected {name}".rstrip())
        self.recorder.device_connected(t, device)

    def device_disconnected(self, t: float, device: Device, name: str = "") -> None:
        self.out(f"{_clock(t)} {device.value} disconnected")
        if device is Device.HRM:
            self._hr = None
        self.recorder.device_disconnected(t, device)

    def hr(self, t: float, sample: HrSample) -> None:
        self._hr = sample.bpm
        self.recorder.hr(t, sample)

    def treadmill(self, t: float, sample: TreadmillSample | InclineSample) -> None:
        if isinstance(sample, InclineSample):
            self._incline = sample.incline_pct
        else:
            self._tm = sample
        self.recorder.treadmill(t, sample)

    def treadmill_state(self, t: float, state: TreadmillState) -> None:
        self.recorder.treadmill_state(t, state)

    def tick(self, t: float) -> None:
        self.recorder.tick(t)
        if self.nas_pending and t >= self._next_export_retry:
            self._next_export_retry = t + RETRY_EVERY_S
            self.start_export()
        if self.state is not SessionState.IDLE and t - self._last_status >= self.status_every_s:
            self._last_status = t
            self.out(self.status_line(t))

    def note(self, t: float, address: str, char: str, data: bytes) -> None:
        """A raw notification (or value read) from either device."""
        if self.state is SessionState.IDLE:
            return  # e.g. the strap left connected after a timeout
        rec = {"t": round(t * 1000), "dev": address, "char": char, "data": data.hex()}
        self.raw.append(rec)
        if self._log_file is not None:
            self._log_file.write(json.dumps(rec) + "\n")
            self._log_file.flush()

    # ------------------------------------------------------------------ status

    def status_line(self, t: float) -> str:
        parts = [_clock(t), f"{self.state.value:<9}"]
        parts.append(f"HR {self._hr:>3}" if self._hr else "HR   -")
        tm = self._tm
        if tm is not None:
            if self.imperial:
                parts += [f"{units.mps_to_mph(tm.speed_mps):4.1f} mph",
                          f"{units.m_to_miles(tm.distance_m):5.2f} mi"]
            else:
                parts += [f"{tm.speed_mps * 3.6:4.1f} km/h", f"{tm.distance_m / 1000:5.2f} km"]
            parts += [f"{tm.elapsed_s // 60:3}:{tm.elapsed_s % 60:02d}", f"{tm.kcal:3} kcal"]
            if self._incline:
                parts.append(f"{self._incline:.1f}%")
        links = []
        for name, client in (("hrm", self.hrm), ("treadmill", self.treadmill_client)):
            if client is not None and client.connected:
                battery = getattr(client, "battery_pct", None)
                links.append(f"{name} {battery}%" if battery is not None else name)
        if self.nas_pending:
            links.append("nas pending")
        if links:
            parts.append("| " + " ".join(links))
        return "  ".join(parts)

    # ------------------------------------------------------------------ recorder callbacks

    def _changed(self, t: float, old: SessionState, new: SessionState) -> None:
        reason = ""
        if new is SessionState.FINISHED and self.recorder.machine.end_reason is not None:
            reason = f" ({self.recorder.machine.end_reason.value})"
        self.out(f"{_clock(t)} {old.value} -> {new.value}{reason}")
        self.backlight.set(new is not SessionState.IDLE)
        if old is SessionState.IDLE:
            self.raw = []
            self._open_log(t)
        elif new is SessionState.IDLE:
            self.raw = []
            self._close_log()
            self._tm = self._incline = None

    def _save(self, session: Session) -> None:
        s = session.summary()
        moving = int(s.moving_s)
        dist = (f"{units.m_to_miles(s.distance_m):.2f} mi" if self.imperial
                else f"{s.distance_m / 1000:.2f} km")
        try:
            t0 = time.monotonic()
            workout_id = self.library.add_session(session, list(self.raw))
            self.saved[id(session)] = workout_id
            self.library.write_sheet()
            self.out(f"saved workout {workout_id}: {moving // 60}:{moving % 60:02d}, {dist}, "
                     f"{s.kcal:.0f} kcal, HR avg {s.avg_hr or '-'} max {s.max_hr or '-'} "
                     f"({time.monotonic() - t0:.1f} s)")
        except Exception:
            log.exception("saving the workout failed; the checkpoint is kept")
            self.out("SAVE FAILED; see the log. The checkpoint file is kept.")
            return
        self.raw = []
        self._remove_checkpoint()
        self.start_export()

    def _discard(self, session: Session) -> None:
        workout_id = self.saved.pop(id(session), None)
        if workout_id is not None:
            self.library.delete(workout_id)
            self.library.write_sheet()
            self.out(f"deleted workout {workout_id}")
            self.start_export()
        else:
            self.out(f"discarded: shorter than {self.cfg.session.min_session_s:.0f} s")
        self.raw = []
        self._remove_checkpoint()

    # ------------------------------------------------------------------ NAS export

    def start_export(self) -> None:
        """Copy to the NAS in a worker thread (it blocks on the network). Without a running
        event loop (tests), copy now."""
        if self.exporter is None:
            return
        if self._export_task is not None and not self._export_task.done():
            self._export_again = True  # a newer workout arrived mid-copy
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._exported(self.exporter.export())
            return
        self._export_task = loop.create_task(self._export())

    async def _export(self) -> None:
        while True:
            self._export_again = False
            self._exported(await asyncio.to_thread(self.exporter.export))
            if not self._export_again:
                return

    def _exported(self, result: ExportResult) -> None:
        self.nas_pending = not result.ok
        self._next_export_retry = time.time() + RETRY_EVERY_S
        now = _clock(time.time())
        if result.ok:
            self.out(f"{now} nas: up to date ({len(result.copied)} files copied)")
        else:
            self.out(f"{now} nas: NOT copied, will retry every "
                     f"{RETRY_EVERY_S / 60:.0f} min ({result.error})")

    async def wait_export(self, timeout: float = 60.0) -> None:
        if self._export_task is not None and not self._export_task.done():
            await asyncio.wait({self._export_task}, timeout=timeout)

    def _checkpoint(self, session: Session) -> None:
        try:
            session.save_checkpoint(self.checkpoint_path)
        except OSError as e:
            log.warning("checkpoint failed: %s", e)

    def _remove_checkpoint(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.checkpoint_path.unlink()

    def _open_log(self, t: float) -> None:
        self._close_log()
        path = self.log_dir / f"{datetime.fromtimestamp(t):%Y%m%d-%H%M%S}.jsonl"
        try:
            self._log_file = open(path, "a", encoding="utf-8")
        except OSError as e:
            log.warning("cannot open raw log %s: %s", path, e)

    def _close_log(self) -> None:
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None

    # ------------------------------------------------------------------ runners

    async def run_ble(self, stop: asyncio.Event | None = None) -> None:
        """Run on the real devices until stop is set (or the task is cancelled)."""
        from gymtracker.ble.hrm import HrmClient
        from gymtracker.ble.scanner import Scanner, Sighting
        from gymtracker.ble.treadmill import TreadmillClient

        stop = stop or asyncio.Event()
        clients = {}

        def seen(s: Sighting) -> None:
            client = clients[s.device]
            if client.busy:
                return
            self.device_seen(time.time(), s.device, s.name)
            client.offer(s)

        scanner = Scanner(self.cfg.hrm, self.cfg.treadmill, seen)
        common = dict(on_raw=self.note, on_connected=self.device_connected,
                      on_disconnected=self.device_disconnected, gate=scanner.pause)
        self.hrm = clients[Device.HRM] = HrmClient(self.hr, **common)
        self.treadmill_client = clients[Device.TREADMILL] = TreadmillClient(
            self.treadmill, self.treadmill_state, **common)
        self.open()
        self.out(f"{_clock(time.time())} waiting for the strap or the treadmill "
                 f"(data in {self.data_dir})")
        try:
            while not stop.is_set():
                await scanner.set_wanted(not all(c.connected for c in clients.values()))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=1.0 - time.time() % 1.0)
                self.tick(time.time())
        finally:
            self.close(time.time())
            await self.wait_export()
            for client in clients.values():
                await client.close()
            await scanner.stop()

    async def run_replay(self, fixture: Path, speed: float = 1.0) -> None:
        from gymtracker.sim.replay import read_fixture, run_recorder_paced

        notes = read_fixture(fixture)
        self.open()
        try:
            await run_recorder_paced(notes, self, speed=speed,
                                     on_note=lambda n: self.note(n.t, n.dev, n.char, n.data))
        finally:
            self.close(notes[-1].t if notes else time.time())
            await self.wait_export()


def run(cfg: Config, replay: Path | None = None, speed: float = 1.0) -> int:
    """`gymtracker run`: the app in the foreground, printing state changes and status."""
    if replay is not None:
        app = App(cfg, backlight=NullBacklight(),
                  status_every_s=1.0 if speed <= 1 else speed)
        asyncio.run(app.run_replay(replay, speed))
        return 0

    async def main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):  # Windows
                loop.add_signal_handler(sig, stop.set)
        exporter = Exporter(cfg.storage.data_dir, cfg.export) if cfg.export.smb_mount else None
        await App(cfg, exporter=exporter).run_ble(stop)

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
    return 0


def _clock(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%H:%M:%S")

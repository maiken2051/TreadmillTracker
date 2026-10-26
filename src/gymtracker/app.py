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
        self.nas_error = ""
        self.listeners: list[Callable[[], None]] = []  # the web server: "state changed"
        self.connected: dict[Device, bool] = {d: False for d in Device}
        self.started = time.time()  # the page reloads when this changes (a new version)
        self.last_tick: float | None = None  # replay time, so a stopped replay ends there
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
        self.connected[device] = True
        if name:
            self.recorder.device_names[device.value] = name
        self.recorder.device_connected(t, device)

    def device_disconnected(self, t: float, device: Device, name: str = "") -> None:
        self.out(f"{_clock(t)} {device.value} disconnected")
        self.connected[device] = False
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
        self.last_tick = t
        self.recorder.tick(t)
        if self.nas_pending and t >= self._next_export_retry:
            self._next_export_retry = t + RETRY_EVERY_S
            self.start_export()
        if self.state is not SessionState.IDLE and t - self._last_status >= self.status_every_s:
            self._last_status = t
            self.out(self.status_line(t))
        self._notify()

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

    # ------------------------------------------------------------------ the screen

    def snapshot(self, t: float | None = None) -> dict:
        """Everything the touchscreen shows, display-ready in the user's units (DESIGN §4.7).
        Sent over the WebSocket at 1 Hz and on every change."""
        t = time.time() if t is None else t
        imperial = self.imperial
        hrm = self.hrm
        tm = self._tm
        snap: dict = {
            "state": self.state.value,
            "started": self.started,
            "clock": datetime.fromtimestamp(t).strftime("%H:%M"),
            "units": {"speed": "mph" if imperial else "km/h", "distance": "mi" if imperial
                      else "km", "pace": "/mi" if imperial else "/km"},
            "hr": self._hr,
            "hr_zone": hr_zone(self._hr, self.cfg.user.max_hr, self.cfg.user.hr_zones),
            "hrm": {"connected": self.connected[Device.HRM],
                    "name": self.recorder.device_names.get(Device.HRM.value, ""),
                    "battery_pct": getattr(hrm, "battery_pct", None) if hrm else None},
            "treadmill": {"connected": self.connected[Device.TREADMILL],
                          "name": self.recorder.device_names.get(Device.TREADMILL.value, "")},
            "nas": {"enabled": self.exporter is not None, "pending": self.nas_pending,
                    "error": self.nas_error},
            "end_reason": None,
            "live": None,
            "summary": None,
        }
        if tm is not None:
            pace = (units.mps_to_min_per_mile(tm.speed_mps) if imperial
                    else (1000 / tm.speed_mps / 60 if tm.speed_mps > 0 else None))
            snap["live"] = {
                "elapsed_s": tm.elapsed_s,
                "speed": round(units.mps_to_mph(tm.speed_mps) if imperial
                               else tm.speed_mps * 3.6, 1),
                "distance": round(units.m_to_miles(tm.distance_m) if imperial
                                  else tm.distance_m / 1000, 2),
                "pace_s": round(pace * 60) if pace and pace < 60 else None,
                "kcal": tm.kcal,
                "incline_pct": self._incline,
            }
        if self.state is SessionState.FINISHED:
            reason = self.recorder.machine.end_reason
            snap["end_reason"] = reason.value if reason else None
            session = self.recorder.session
            if session is not None:
                snap["summary"] = self._summary_view(session)
        return snap

    def _summary_view(self, session: Session) -> dict:
        s = session.summary()
        imperial = self.imperial

        def speed(v):
            if v is None:
                return None
            return round(units.mps_to_mph(v) if imperial else v * 3.6, 1)

        return {
            "workout_id": self.saved.get(id(session)),
            "moving_s": int(s.moving_s),
            "distance": round(units.m_to_miles(s.distance_m) if imperial
                              else s.distance_m / 1000, 2),
            "kcal": round(s.kcal),
            "avg_hr": s.avg_hr, "max_hr": s.max_hr, "min_hr": s.min_hr,
            "min_speed": speed(s.min_speed_mps), "avg_speed": speed(s.avg_speed_mps),
            "max_speed": speed(s.max_speed_mps),
            "pauses": s.pause_count,
        }

    def action(self, name: str, t: float | None = None) -> bool:
        """A button on the screen. False for an unknown action."""
        t = time.time() if t is None else t
        if name == "end":
            self.recorder.user_end(t)
        elif name == "done":
            self.recorder.user_done(t)
        elif name == "discard":
            self.recorder.user_discard(t)
        elif name == "retry_export":
            self.start_export()
        else:
            return False
        self._notify()
        return True

    def _notify(self) -> None:
        for listener in self.listeners:
            listener()

    # ------------------------------------------------------------------ recorder callbacks

    def _changed(self, t: float, old: SessionState, new: SessionState) -> None:
        reason = ""
        if new is SessionState.FINISHED and self.recorder.machine.end_reason is not None:
            reason = f" ({self.recorder.machine.end_reason.value})"
        self.out(f"{_clock(t)} {old.value} -> {new.value}{reason}")
        self._notify()
        self.backlight.set(new is not SessionState.IDLE)
        if old is SessionState.IDLE:
            self.raw = []
            self._open_log(t)
        elif new is SessionState.IDLE:
            self.raw = []
            self._close_log()
            self._tm = self._incline = None
            self._release_strap()

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
        self.nas_error = result.error
        self._next_export_retry = time.time() + RETRY_EVERY_S
        now = _clock(time.time())
        if result.ok:
            self.out(f"{now} nas: up to date ({len(result.copied)} files copied)")
        else:
            self.out(f"{now} nas: NOT copied, will retry every "
                     f"{RETRY_EVERY_S / 60:.0f} min ({result.error})")
        self._notify()

    async def wait_export(self, timeout: float = 60.0) -> None:
        if self._export_task is not None and not self._export_task.done():
            await asyncio.wait({self._export_task}, timeout=timeout)

    def wants(self, device: Device) -> bool:
        """Connect to this device now? Asleep, only the treadmill: its Bluetooth button wakes
        the app, then the strap connects (it is seen again within ~2 s)."""
        return device is Device.TREADMILL or self.state is not SessionState.IDLE

    def _release_strap(self) -> None:
        """Asleep: let go of the strap so it can sleep too (and its LED stays off)."""
        if self.hrm is None or not self.hrm.busy:
            return
        with contextlib.suppress(RuntimeError):  # no event loop: replays and tests
            asyncio.get_running_loop().create_task(self.hrm.disconnect())

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
            if client.busy or not self.wants(s.device):
                return
            self.device_seen(time.time(), s.device, s.name)
            client.offer(s)

        scanner = Scanner(self.cfg.hrm, self.cfg.treadmill, seen)
        common = dict(on_raw=self.note, on_connected=self.device_connected,
                      on_disconnected=self.device_disconnected, gate=scanner.pause)
        self.hrm = clients[Device.HRM] = HrmClient(
            self.hr, check_silence=lambda: self.recorder.recording, **common)
        self.treadmill_client = clients[Device.TREADMILL] = TreadmillClient(
            self.treadmill, self.treadmill_state, **common)
        self.open()
        self.out(f"{_clock(time.time())} waiting for the treadmill "
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
            end = self.last_tick if self.last_tick is not None else (
                notes[-1].t if notes else time.time())
            self.close(end)
            await self.wait_export()


def run(cfg: Config, replay: Path | None = None, speed: float = 1.0,
        quiet: bool = False) -> int:
    """`gymtracker run`: the app in the foreground, printing state changes and status, with
    the touchscreen page on [web] port. A replay keeps serving the page after the capture
    ends, until Ctrl-C, so the FINISHED screen can be looked at."""
    from gymtracker.web.server import WebServer

    async def main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):  # Windows
                loop.add_signal_handler(sig, stop.set)
        if replay is not None:
            app = App(cfg, backlight=NullBacklight(),
                      status_every_s=1.0 if speed <= 1 else speed)
        else:
            exporter = (Exporter(cfg.storage.data_dir, cfg.export) if cfg.export.smb_mount
                        else None)
            app = App(cfg, exporter=exporter,
                      status_every_s=float("inf") if quiet else 1.0)
        # A replay is for testing on this machine: localhost only (no firewall prompt).
        server = WebServer(app, cfg.web.port,
                           host="127.0.0.1" if replay is not None else "0.0.0.0")
        await server.start()
        app.out(f"page: http://localhost:{cfg.web.port}/")
        try:
            if replay is not None:
                # Ctrl-C/SIGTERM stops a replay mid-capture too (it saves what it has).
                task = asyncio.create_task(app.run_replay(replay, speed))
                await stop.wait()
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            else:
                await app.run_ble(stop)
        finally:
            await server.stop()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
    return 0


def hr_zone(hr: int | None, max_hr: int, zones: list[int]) -> int | None:
    """1-5, or 0 below zone 1. With max_hr set, zones start at 50/60/70/80/90 % of it;
    otherwise at the configured lower bounds."""
    if not hr:
        return None
    bounds = [max_hr * p / 100 for p in (50, 60, 70, 80, 90)] if max_hr else zones
    return sum(1 for b in bounds if hr >= b)


def _clock(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%H:%M:%S")

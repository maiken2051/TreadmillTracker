"""Session state machine (DESIGN.md §4.4). Pure: no I/O, time is passed in with every event.

Each event method returns the effects the caller must carry out (start a session, pause it,
save it, ...). The treadmill's state events are the primary start/pause/stop signal; the
speed rules are a fallback used only while no state event has arrived on the current
treadmill connection. (After a Stop the belt coasts down for ~10 s, which the speed rules
would otherwise read as a resume.)
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Callable

from gymtracker.ble.treadmill import TreadmillState
from gymtracker.config import SessionConfig

log = logging.getLogger(__name__)


class SessionState(enum.Enum):
    IDLE = "idle"
    WAKING = "waking"
    READY = "ready"
    RECORDING = "recording"
    PAUSED = "paused"
    FINISHED = "finished"


class Device(enum.Enum):
    HRM = "hrm"
    TREADMILL = "treadmill"


class Effect(enum.Enum):
    START = "start"  # begin a new session
    PAUSE = "pause"
    RESUME = "resume"
    SAVE = "save"  # the session ended and is long enough: finish and save it
    DISCARD = "discard"  # drop the session (too short, or the user discarded a saved one)


class EndReason(enum.Enum):
    CONSOLE_ENDED = "console ended"
    DISCONNECTED = "treadmill disconnected"
    PAUSE_TIMEOUT = "paused too long"
    USER = "ended on screen"


_STILL_KMH = 0.1  # below this the belt counts as stopped

StateCallback = Callable[[float, SessionState, SessionState], None]


class Transitions:
    def __init__(self, cfg: SessionConfig, on_change: StateCallback | None = None) -> None:
        self.cfg = cfg
        self.on_change = on_change
        self.state = SessionState.IDLE
        self.since = 0.0  # time the current state was entered
        self.connected: set[Device] = set()
        self.state_events_seen = False  # on the current treadmill connection
        self.end_reason: EndReason | None = None
        self.too_short = False
        self.saved = False
        self._fast_since: float | None = None
        self._still_since: float | None = None
        self._active_s = 0.0  # time spent RECORDING in the current session
        self._run_since: float | None = None

    # ------------------------------------------------------------------ device events

    def device_seen(self, t: float, device: Device) -> list[Effect]:
        # Only the treadmill wakes the app: its Bluetooth button is a deliberate start. The
        # COROS keeps advertising and accepting connections for ~10 min after it comes off
        # the arm, so the strap is not even connected until the app is awake (UI review).
        if self.state is SessionState.IDLE and device is Device.TREADMILL:
            self._enter(t, SessionState.WAKING)
        return []

    def device_connected(self, t: float, device: Device) -> list[Effect]:
        self.connected.add(device)
        if device is Device.TREADMILL:
            self.state_events_seen = False
            self._fast_since = self._still_since = None
        if self.state is SessionState.IDLE and device is Device.HRM:
            return []  # left over from the last workout; the treadmill wakes the app
        if self.state in (SessionState.IDLE, SessionState.WAKING, SessionState.READY):
            self._enter(t, SessionState.READY)
        elif self.state is SessionState.FINISHED and device is Device.TREADMILL:
            self._enter(t, SessionState.READY)  # next workout; the last one is already settled
        return []

    def device_disconnected(self, t: float, device: Device) -> list[Effect]:
        self.connected.discard(device)
        if device is Device.TREADMILL and self.state in (SessionState.RECORDING,
                                                         SessionState.PAUSED):
            return self._finish(t, EndReason.DISCONNECTED)
        if self.state is SessionState.READY and not self.connected:
            self._enter(t, SessionState.WAKING)
        return []

    def speed(self, t: float, speed_mps: float) -> list[Effect]:
        kmh = speed_mps * 3.6
        if kmh > self.cfg.start_speed_kmh:
            self._fast_since = t if self._fast_since is None else self._fast_since
        else:
            self._fast_since = None
        if kmh < _STILL_KMH:
            self._still_since = t if self._still_since is None else self._still_since
        else:
            self._still_since = None
        return self._speed_rules(t)

    def treadmill_state(self, t: float, state: TreadmillState) -> list[Effect]:
        if state is TreadmillState.UNKNOWN:
            return []
        self.state_events_seen = True
        if state is TreadmillState.RUNNING:
            if self.state in (SessionState.READY, SessionState.FINISHED):
                return self._start(t)
            if self.state is SessionState.PAUSED:
                return self._resume(t)
        elif state is TreadmillState.PAUSED_OR_STOPPED:
            if self.state is SessionState.RECORDING:
                return self._pause(t)
        elif state is TreadmillState.ENDED:
            if self.state in (SessionState.RECORDING, SessionState.PAUSED):
                return self._finish(t, EndReason.CONSOLE_ENDED)
        return []

    def tick(self, t: float) -> list[Effect]:
        elapsed = t - self.since
        if self.state is SessionState.WAKING and elapsed >= self.cfg.wake_timeout_s:
            self._enter(t, SessionState.IDLE)
        elif self.state is SessionState.READY and elapsed >= self.cfg.ready_timeout_s:
            self._enter(t, SessionState.IDLE)
        elif self.state is SessionState.FINISHED and elapsed >= self.cfg.idle_return_s:
            self._enter(t, SessionState.IDLE)
        elif self.state is SessionState.PAUSED and elapsed >= self.cfg.end_after_paused_s:
            return self._finish(t, EndReason.PAUSE_TIMEOUT)
        elif self.state is SessionState.RECORDING:
            return self._speed_rules(t)
        return []

    # ------------------------------------------------------------------ user actions

    def user_end(self, t: float) -> list[Effect]:
        if self.state in (SessionState.RECORDING, SessionState.PAUSED):
            return self._finish(t, EndReason.USER)
        return []

    def user_done(self, t: float) -> list[Effect]:
        if self.state is SessionState.FINISHED:
            self._enter(t, SessionState.IDLE)
        return []

    def user_discard(self, t: float) -> list[Effect]:
        if self.state is SessionState.FINISHED:
            self._enter(t, SessionState.IDLE)
            if self.saved:
                self.saved = False
                return [Effect.DISCARD]
        return []

    @property
    def active_s(self) -> float:
        """Time spent RECORDING in the current session, up to the last state change."""
        return self._active_s

    # ------------------------------------------------------------------ internals

    def _speed_rules(self, t: float) -> list[Effect]:
        if self.state_events_seen:
            return []
        held = self._fast_since is not None and t - self._fast_since >= self.cfg.start_hold_s
        if self.state is SessionState.READY and held:
            return self._start(t)
        if self.state is SessionState.PAUSED and held:
            return self._resume(t)
        if (self.state is SessionState.RECORDING and self._still_since is not None
                and t - self._still_since >= self.cfg.autopause_after_s):
            return self._pause(t)
        return []

    def _start(self, t: float) -> list[Effect]:
        self._active_s = 0.0
        self._run_since = t
        self.end_reason = None
        self.too_short = self.saved = False
        self._enter(t, SessionState.RECORDING)
        return [Effect.START]

    def _pause(self, t: float) -> list[Effect]:
        self._stop_clock(t)
        self._enter(t, SessionState.PAUSED)
        return [Effect.PAUSE]

    def _resume(self, t: float) -> list[Effect]:
        self._run_since = t
        self._enter(t, SessionState.RECORDING)
        return [Effect.RESUME]

    def _finish(self, t: float, reason: EndReason) -> list[Effect]:
        self._stop_clock(t)
        self.end_reason = reason
        self.too_short = self._active_s < self.cfg.min_session_s
        self.saved = not self.too_short
        self._enter(t, SessionState.FINISHED)
        return [Effect.DISCARD] if self.too_short else [Effect.SAVE]

    def _stop_clock(self, t: float) -> None:
        if self._run_since is not None:
            self._active_s += t - self._run_since
            self._run_since = None

    def _enter(self, t: float, state: SessionState) -> None:
        old, self.state, self.since = self.state, state, t
        if old is not state:
            log.info("state %s -> %s", old.value, state.value)
            if self.on_change is not None:
                self.on_change(t, old, state)

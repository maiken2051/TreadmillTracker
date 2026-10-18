"""Recorder: wires device events through the state machine into Sessions.

Synchronous and I/O-free apart from the callbacks, so the same code runs under the real
BLE clients (Phase 4) and under fixture replay. The caller supplies time with every event
and calls tick() about once a second.
"""

from __future__ import annotations

from collections.abc import Callable

from gymtracker.ble.hrm import HrSample
from gymtracker.ble.treadmill import InclineSample, TreadmillSample, TreadmillState
from gymtracker.config import SessionConfig
from gymtracker.session import Session
from gymtracker.state import Device, Effect, StateCallback, Transitions

SessionCallback = Callable[[Session], None]


class Recorder:
    def __init__(self, cfg: SessionConfig, *,
                 on_save: SessionCallback | None = None,
                 on_discard: SessionCallback | None = None,
                 on_checkpoint: SessionCallback | None = None,
                 on_change: StateCallback | None = None) -> None:
        self.cfg = cfg
        self.machine = Transitions(cfg, on_change=on_change)
        self.session: Session | None = None  # current, or the last finished one
        self.on_save = on_save
        self.on_discard = on_discard
        self.on_checkpoint = on_checkpoint
        self._last_checkpoint = 0.0
        self.device_names: dict[str, str] = {}

    @property
    def state(self):
        return self.machine.state

    @property
    def recording(self) -> bool:
        return self.session is not None and self.session.end_t is None

    # ------------------------------------------------------------------ events

    def device_seen(self, t: float, device: Device, name: str = "") -> None:
        if name:
            self.device_names[device.value] = name
        self._apply(t, self.machine.device_seen(t, device))

    def device_connected(self, t: float, device: Device) -> None:
        self._apply(t, self.machine.device_connected(t, device))

    def device_disconnected(self, t: float, device: Device) -> None:
        self._apply(t, self.machine.device_disconnected(t, device))

    def hr(self, t: float, sample: HrSample) -> None:
        if self.recording:
            self.session.add_hr(t, sample)

    def treadmill(self, t: float, sample: TreadmillSample | InclineSample) -> None:
        if self.recording:
            self.session.add_treadmill(t, sample)
        if isinstance(sample, TreadmillSample):
            self._apply(t, self.machine.speed(t, sample.speed_mps))

    def treadmill_state(self, t: float, state: TreadmillState) -> None:
        self._apply(t, self.machine.treadmill_state(t, state))

    def tick(self, t: float) -> None:
        if self.recording:
            self.session.tick(t)
            if t - self._last_checkpoint >= self.cfg.checkpoint_s:
                self._last_checkpoint = t
                if self.on_checkpoint is not None:
                    self.on_checkpoint(self.session)
        self._apply(t, self.machine.tick(t))

    def user_end(self, t: float) -> None:
        self._apply(t, self.machine.user_end(t))

    def user_done(self, t: float) -> None:
        self._apply(t, self.machine.user_done(t))

    def user_discard(self, t: float) -> None:
        self._apply(t, self.machine.user_discard(t))

    # ------------------------------------------------------------------ effects

    def _apply(self, t: float, effects: list[Effect]) -> None:
        for effect in effects:
            if effect is Effect.START:
                self.session = Session(start_t=t, devices=dict(self.device_names))
                self._last_checkpoint = t
            elif effect is Effect.PAUSE:
                self.session.pause(t)
            elif effect is Effect.RESUME:
                self.session.resume(t)
            elif effect is Effect.SAVE:
                self.session.finish(t)
                if self.on_save is not None:
                    self.on_save(self.session)
            elif effect is Effect.DISCARD:
                self.session.finish(t)
                if self.on_discard is not None:
                    self.on_discard(self.session)

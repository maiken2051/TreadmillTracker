"""One continuous BLE scanner for both devices (DESIGN.md §4.3).

BlueZ allows one discovery at a time, so all matching goes through this scanner; the clients
never scan. The treadmill advertises for only ~1 minute after its Bluetooth button is pressed
and with a new address every time, so the app connects as soon as it is seen.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from gymtracker.ble.hrm import HRS_UUID
from gymtracker.ble.treadmill import TREADMILL_SERVICE_UUID
from gymtracker.config import HrmConfig, TreadmillConfig
from gymtracker.state import Device

log = logging.getLogger(__name__)

REPEAT_S = 2.0  # report the same address at most this often (the console sends ~30 adv/s)


@dataclass(frozen=True)
class Sighting:
    device: Device
    ble_device: BLEDevice
    name: str
    rssi: int


SeenCallback = Callable[[Sighting], None]


def match(address: str, name: str, service_uuids: list[str], hrm: HrmConfig,
          treadmill: TreadmillConfig) -> Device | None:
    """Which of our devices an advertisement is, if any."""
    uuids = {u.lower() for u in service_uuids}
    if HRS_UUID in uuids:
        if hrm.address:
            if address.lower() == hrm.address.lower():
                return Device.HRM
        elif name.startswith(hrm.name_prefix):
            return Device.HRM
    if TREADMILL_SERVICE_UUID in uuids and name.startswith(treadmill.name_prefix):
        return Device.TREADMILL
    return None


class Debouncer:
    """Passes the first sighting of an address, then at most one per REPEAT_S."""

    def __init__(self, repeat_s: float = REPEAT_S) -> None:
        self.repeat_s = repeat_s
        self._last: dict[str, float] = {}

    def __call__(self, address: str, now: float) -> bool:
        last = self._last.get(address)
        if last is not None and now - last < self.repeat_s:
            return False
        self._last[address] = now
        return True


class Scanner:
    """Scans while wanted and not paused. pause() is held around every connect, which also
    serialises connections (one at a time, as in Phase 0)."""

    def __init__(self, hrm: HrmConfig, treadmill: TreadmillConfig,
                 on_seen: SeenCallback) -> None:
        self.hrm_cfg = hrm
        self.treadmill_cfg = treadmill
        self.on_seen = on_seen
        self._debounce = Debouncer()
        self._scanner = BleakScanner(detection_callback=self._on_adv,
                                     service_uuids=[HRS_UUID, TREADMILL_SERVICE_UUID])
        self._lock = asyncio.Lock()
        self._wanted = False
        self._paused = 0
        self._running = False

    async def set_wanted(self, wanted: bool) -> None:
        """Scan (True) or not (False); the app stops scanning once both devices are connected."""
        self._wanted = wanted
        await self._update()

    @contextlib.asynccontextmanager
    async def pause(self) -> AsyncIterator[None]:
        async with self._lock:
            self._paused += 1
            try:
                await self._update()
                yield
            finally:
                self._paused -= 1
                await self._update()

    async def stop(self) -> None:
        self._wanted = False
        await self._update()

    async def _update(self) -> None:
        want = self._wanted and not self._paused
        if want == self._running:
            return
        try:
            if want:
                await self._scanner.start()
                log.info("scanning")
            else:
                await self._scanner.stop()
                log.info("scan stopped")
            self._running = want
        except Exception as e:  # BlueZ hiccups must never kill the app
            log.warning("scanner %s failed: %s", "start" if want else "stop", e)

    def _on_adv(self, ble_device: BLEDevice, adv: AdvertisementData) -> None:
        name = adv.local_name or ble_device.name or ""
        device = match(ble_device.address, name, adv.service_uuids, self.hrm_cfg,
                       self.treadmill_cfg)
        if device is None or not self._debounce(ble_device.address, time.monotonic()):
            return
        self.on_seen(Sighting(device, ble_device, name, adv.rssi))

"""Heart Rate Service (0x180D): parser for Heart Rate Measurement (0x2A37), and the client.

The parser handles the full flags byte from the Bluetooth SIG spec, even though the COROS
strap only ever sends flags 0x00 + uint8 bpm. That strap has no sensor-contact bits, so bpm 0
means "not on skin" and is reported as no heart rate (DESIGN.md §4.3).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from gymtracker.ble.link import Link

log = logging.getLogger(__name__)

SIG_BASE = "-0000-1000-8000-00805f9b34fb"
HRS_UUID = "0000180d" + SIG_BASE
HR_MEASUREMENT_UUID = "00002a37" + SIG_BASE
BATTERY_SERVICE_UUID = "0000180f" + SIG_BASE
BATTERY_LEVEL_UUID = "00002a19" + SIG_BASE

_FLAG_HR_UINT16 = 0x01
_FLAG_CONTACT_DETECTED = 0x02
_FLAG_CONTACT_SUPPORTED = 0x04
_FLAG_ENERGY = 0x08
_FLAG_RR = 0x10


@dataclass(frozen=True)
class HrSample:
    bpm: int | None  # None when the strap reports 0 (off skin)
    contact: bool | None = None  # None when the strap does not report contact
    energy_kj: int | None = None
    rr_ms: list[float] = field(default_factory=list)


def parse_hr_measurement(data: bytes) -> HrSample | None:
    """Parse one Heart Rate Measurement notification; None (with a warning) if malformed."""
    if len(data) < 2:
        log.warning("HR measurement too short: %s", data.hex())
        return None
    flags = data[0]
    pos = 1
    if flags & _FLAG_HR_UINT16:
        if len(data) < pos + 2:
            log.warning("HR measurement too short for uint16 HR: %s", data.hex())
            return None
        bpm = int.from_bytes(data[pos:pos + 2], "little")
        pos += 2
    else:
        bpm = data[pos]
        pos += 1

    contact = None
    if flags & _FLAG_CONTACT_SUPPORTED:
        contact = bool(flags & _FLAG_CONTACT_DETECTED)

    energy = None
    if flags & _FLAG_ENERGY:
        if len(data) < pos + 2:
            log.warning("HR measurement too short for energy: %s", data.hex())
            return None
        energy = int.from_bytes(data[pos:pos + 2], "little")
        pos += 2

    rr_ms = []
    if flags & _FLAG_RR:
        while pos + 2 <= len(data):
            rr_ms.append(int.from_bytes(data[pos:pos + 2], "little") * 1000 / 1024)
            pos += 2

    return HrSample(bpm=bpm or None, contact=contact, energy_kj=energy, rr_ms=rr_ms)


def parse_battery_level(data: bytes) -> int | None:
    """Battery Level (0x2A19): one byte, percent."""
    if len(data) != 1 or data[0] > 100:
        log.warning("unexpected battery level: %s", data.hex())
        return None
    return data[0]


SILENCE_S = 20.0  # connected but quiet this long -> reconnect once (seen at 20 % battery)
BATTERY_EVERY_S = 300.0


class HrmClient(Link):
    """Subscribes to Heart Rate Measurement and polls Battery Level (read-only on the COROS
    strap). Silences under ~15 s are normal (strap adjusted, brief loss of contact).

    A longer silence gets one reconnect, for a link that BlueZ thinks is up but is dead. If
    the new link is silent too, the strap is off the arm: it keeps advertising and accepting
    connections for ~10 min but sends nothing (NAS test, 2026-10-25), and every reconnect
    lights its LED and wakes the app. So stay connected and wait; the next notification
    re-arms the reconnect, and the strap drops the link itself when it goes to sleep."""

    def __init__(self, on_sample: Callable[[float, HrSample], None], **kwargs) -> None:
        super().__init__(**kwargs)
        from gymtracker.state import Device  # state.py imports the ble package
        self.device = Device.HRM
        self.on_sample = on_sample
        self.battery_pct: int | None = None
        self._last_note = 0.0  # monotonic
        self._silence_retried = False  # reconnected for silence, no notification since

    async def setup(self, client) -> None:
        self._last_note = time.monotonic()
        await client.start_notify(HR_MEASUREMENT_UUID, self.notify_handler(HR_MEASUREMENT_UUID))

    def handle(self, t: float, char: str, data: bytes) -> None:
        if char == HR_MEASUREMENT_UUID:
            self._last_note = time.monotonic()
            self._silence_retried = False
            sample = parse_hr_measurement(data)
            if sample is not None:
                self.on_sample(t, sample)
        elif char == BATTERY_LEVEL_UUID:
            pct = parse_battery_level(data)
            if pct is not None:
                if pct != self.battery_pct:
                    log.info("hrm: battery %d%%", pct)
                self.battery_pct = pct

    async def maintain(self, client) -> None:
        next_battery = time.monotonic()
        while self.connected:
            now = time.monotonic()
            if now >= next_battery:
                next_battery = now + BATTERY_EVERY_S
                try:
                    self.received(BATTERY_LEVEL_UUID,
                                  bytes(await client.read_gatt_char(BATTERY_LEVEL_UUID)))
                except Exception as e:
                    log.warning("hrm: battery read failed: %r", e)
            if now - self._last_note >= SILENCE_S:
                if self._silence_retried:
                    log.info("hrm: still silent after reconnecting; strap not worn? "
                             "staying connected")
                    self._last_note = float("inf")  # no more checks until it speaks
                else:
                    log.warning("hrm: connected but silent for %.0f s; reconnecting",
                                now - self._last_note)
                    self._silence_retried = True
                    await self.disconnect()
                    return
            await asyncio.sleep(1.0)

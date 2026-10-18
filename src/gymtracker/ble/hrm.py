"""Heart Rate Service (0x180D): parser for Heart Rate Measurement (0x2A37).

The parser handles the full flags byte from the Bluetooth SIG spec, even though the COROS
strap only ever sends flags 0x00 + uint8 bpm. That strap has no sensor-contact bits, so bpm 0
means "not on skin" and is reported as no heart rate (DESIGN.md §4.3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

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

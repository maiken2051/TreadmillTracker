"""TRUE treadmill vendor protocol (service a026ee07): parsers for data and state frames, and
the client.

The console does not speak FTMS. The layout below was decoded from the Phase 0 captures
and confirmed by a calibration walk; DESIGN.md §4.3 has the full table.

NEVER WRITE to any characteristic of this device. Several are writable (presumably for
control); this project only observes.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Callable
from dataclasses import dataclass

from gymtracker.ble.link import Link

log = logging.getLogger(__name__)

VENDOR_BASE = "-0a7d-4ab3-97fa-f1500f9feb8b"
TREADMILL_SERVICE_UUID = "a026ee07" + VENDOR_BASE
DATA_UUID = "a026e01d" + VENDOR_BASE  # notify: 0x53 and 0x22 frames, ~1 Hz each
STATE_UUID = "a026e01e" + VENDOR_BASE  # read + notify: one state byte

_WORKOUT_FRAME = 0x53
_WORKOUT_FRAME_LEN = 19
_INCLINE_FRAME = 0x22
_INCLINE_FRAME_LEN = 4


@dataclass(frozen=True)
class TreadmillSample:
    """A 0x53 frame. Counters are the console's own totals for the current workout."""
    elapsed_s: int  # frozen while paused, 0 before start
    speed_mps: float
    distance_m: int
    kcal: int
    mets: float


@dataclass(frozen=True)
class InclineSample:
    """A 0x22 frame."""
    incline_pct: float


class TreadmillState(enum.Enum):
    IDLE = 0x01  # connected, no workout started yet (seen in field test 1)
    RUNNING = 0x0A  # Quick Start or resume
    PAUSED_OR_STOPPED = 0x02  # Stop sends this at once, ENDED follows 6-12 s later
    ENDED = 0x80
    UNKNOWN = -1


def parse_data_frame(data: bytes) -> TreadmillSample | InclineSample | None:
    """Parse one a026e01d notification; None (with a warning) for unknown frames."""
    if not data:
        log.warning("empty treadmill data frame")
        return None
    kind = data[0]
    if kind == _WORKOUT_FRAME and len(data) == _WORKOUT_FRAME_LEN:
        speed_raw = int.from_bytes(data[7:9], "little")  # 0.01 km/h
        return TreadmillSample(
            elapsed_s=int.from_bytes(data[3:7], "little"),
            speed_mps=speed_raw / 360.0,
            distance_m=int.from_bytes(data[9:11], "little"),
            kcal=int.from_bytes(data[14:16], "little"),
            mets=data[16] / 10,
        )
    if kind == _INCLINE_FRAME and len(data) == _INCLINE_FRAME_LEN:
        return InclineSample(incline_pct=int.from_bytes(data[2:4], "little") / 10)
    log.warning("unknown treadmill frame (type 0x%02x, %d bytes): %s",
                kind, len(data), data.hex())
    return None


def parse_state(data: bytes) -> TreadmillState:
    """Parse one a026e01e value."""
    if len(data) == 1:
        try:
            return TreadmillState(data[0])
        except ValueError:
            pass
    log.warning("unknown treadmill state: %s", data.hex())
    return TreadmillState.UNKNOWN


class TreadmillClient(Link):
    """Subscribes to the data and state characteristics and reads the state once on connect.
    Read-only: this class has no code path that writes to the console."""

    def __init__(self, on_sample: Callable[[float, TreadmillSample | InclineSample], None],
                 on_state: Callable[[float, TreadmillState], None], **kwargs) -> None:
        super().__init__(**kwargs)
        from gymtracker.state import Device  # state.py imports this module
        self.device = Device.TREADMILL
        self.on_sample = on_sample
        self.on_state = on_state

    async def setup(self, client) -> None:
        await client.start_notify(DATA_UUID, self.notify_handler(DATA_UUID))
        await client.start_notify(STATE_UUID, self.notify_handler(STATE_UUID))
        self.received(STATE_UUID, bytes(await client.read_gatt_char(STATE_UUID)))

    def handle(self, t: float, char: str, data: bytes) -> None:
        if char == DATA_UUID:
            sample = parse_data_frame(data)
            if sample is not None:
                self.on_sample(t, sample)
        elif char == STATE_UUID:
            self.on_state(t, parse_state(data))

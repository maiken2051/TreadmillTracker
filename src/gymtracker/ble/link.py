"""Connection handling shared by the HRM and treadmill clients.

The app offers each sighting from the scanner to the matching client; a client that is not
connected connects to it, unless it is backing off after a failure (1 s doubling to 30 s).
Reconnecting is therefore driven by sightings, which suits both devices: the treadmill has a
new address every time, and the strap stops advertising when it is not worn.

Two BlueZ quirks from Phase 0 are handled here: the disconnect callback can fire while
connect() is still in progress (BlueZ retries), and is ignored until connect() returns.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic

if TYPE_CHECKING:  # hrm.py and treadmill.py import this module, and state.py imports them
    from gymtracker.ble.scanner import Sighting
    from gymtracker.state import Device

log = logging.getLogger(__name__)

BACKOFF_FIRST_S = 1.0
BACKOFF_MAX_S = 30.0
CONNECT_TIMEOUT_S = 15.0

RawCallback = Callable[[float, str, str, bytes], None]  # t, address, char uuid, data
LinkCallback = Callable[[float, "Device", str], None]  # t, device, name
Gate = Callable[[], AbstractAsyncContextManager[None]]


@contextlib.asynccontextmanager
async def no_gate():
    yield


class Link:
    """One device's connection. Subclasses set `self.device` and implement setup() and handle(),
    and may implement maintain(), which runs once set up and should return when
    `connected` goes False."""

    device: Device

    def __init__(self, *, on_raw: RawCallback | None = None,
                 on_connected: LinkCallback | None = None,
                 on_disconnected: LinkCallback | None = None,
                 gate: Gate = no_gate, client_factory=BleakClient,
                 clock: Callable[[], float] = time.time) -> None:
        self.on_raw = on_raw
        self.on_connected = on_connected
        self.on_disconnected = on_disconnected
        self.gate = gate
        self.client_factory = client_factory
        self.clock = clock
        self.connected = False
        self.name = ""
        self.address = ""
        self._client = None
        self._up = False  # connect() returned for the current client
        self._task: asyncio.Task | None = None
        self._maintain_task: asyncio.Task | None = None
        self._delay = BACKOFF_FIRST_S
        self._not_before = 0.0  # monotonic

    @property
    def busy(self) -> bool:
        """Connected or connecting."""
        return self.connected or (self._task is not None and not self._task.done())

    def offer(self, sighting: Sighting) -> bool:
        """Connect to a sighted device unless busy or backing off. True if started."""
        if self.busy or time.monotonic() < self._not_before:
            return False
        self._task = asyncio.create_task(self._connect(sighting))
        return True

    async def close(self) -> None:
        for task in (self._task, self._maintain_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.disconnect()

    async def disconnect(self) -> None:
        """Drop the link (the disconnect callback then reports it)."""
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as e:
                log.warning("%s: disconnect failed: %s", self.device.value, e)

    # ------------------------------------------------------------------ subclass hooks

    async def setup(self, client) -> None:
        raise NotImplementedError

    def handle(self, t: float, char: str, data: bytes) -> None:
        raise NotImplementedError

    async def maintain(self, client) -> None:
        return

    # ------------------------------------------------------------------ helpers

    def notify_handler(self, char: str):
        def cb(_sender: BleakGATTCharacteristic, data: bytearray) -> None:
            self.received(char, bytes(data))
        return cb

    def received(self, char: str, data: bytes) -> None:
        """A notification or a value read: log it raw, then parse it."""
        t = self.clock()
        if self.on_raw is not None:
            self.on_raw(t, self.address, char, data)
        self.handle(t, char, data)

    # ------------------------------------------------------------------ internals

    async def _connect(self, s: Sighting) -> None:
        self.name, self.address = s.name, s.ble_device.address
        client = None
        try:
            async with self.gate():
                client = self.client_factory(s.ble_device,
                                             disconnected_callback=self._on_bleak_disconnect,
                                             timeout=CONNECT_TIMEOUT_S)
                self._client, self._up = client, False
                log.info("%s: connecting to %s (%s, rssi %d)", self.device.value, s.name,
                         self.address, s.rssi)
                t0 = time.monotonic()
                await client.connect()
                log.info("%s: connected in %.1f s", self.device.value, time.monotonic() - t0)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._failed(f"connect failed: {e!r}")
            return
        if not client.is_connected:
            await self._failed("link dropped as connect() returned")
            return
        self._up = True
        self.connected = True
        if self.on_connected is not None:
            self.on_connected(self.clock(), self.device, self.name)
        try:
            await self.setup(client)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("%s: setup failed: %r; disconnecting", self.device.value, e)
            self._backoff()
            await self.disconnect()
            return
        self._delay = BACKOFF_FIRST_S
        self._maintain_task = asyncio.create_task(self._maintain(client))

    async def _maintain(self, client) -> None:
        try:
            await self.maintain(client)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("%s: %r; disconnecting", self.device.value, e)
            await self.disconnect()

    def _on_bleak_disconnect(self, client) -> None:
        if client is not self._client or not self._up:
            log.info("%s: link dropped during connect (BlueZ retries)", self.device.value)
            return
        self._up = False
        if not self.connected:
            return
        self.connected = False
        log.info("%s: disconnected", self.device.value)
        if self.on_disconnected is not None:
            self.on_disconnected(self.clock(), self.device, self.name)

    async def _failed(self, why: str) -> None:
        log.warning("%s: %s; next try in %.0f s", self.device.value, why, self._delay)
        self._backoff()
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.disconnect()

    def _backoff(self) -> None:
        self._not_before = time.monotonic() + self._delay
        self._delay = min(self._delay * 2, BACKOFF_MAX_S)

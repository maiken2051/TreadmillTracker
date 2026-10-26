"""Scanner matching and the client connection logic, with a fake BleakClient."""

import asyncio
from types import SimpleNamespace

import pytest

from gymtracker.ble import hrm as hrm_mod
from gymtracker.ble import link
from gymtracker.ble.hrm import BATTERY_LEVEL_UUID, HR_MEASUREMENT_UUID, HRS_UUID, HrmClient
from gymtracker.ble.scanner import Debouncer, Sighting, match
from gymtracker.ble.treadmill import (
    DATA_UUID,
    STATE_UUID,
    TREADMILL_SERVICE_UUID,
    TreadmillClient,
    TreadmillState,
)
from gymtracker.config import HrmConfig, TreadmillConfig
from gymtracker.state import Device

# --------------------------------------------------------------------------- scanner


def test_match_hrm_by_name_and_service():
    assert match("C5:BE:F4:C1:8C:38", "COROS HEART RATE FCCB4C", [HRS_UUID], HrmConfig(),
                 TreadmillConfig()) is Device.HRM
    assert match("C5:BE:F4:C1:8C:38", "Polar H10", [HRS_UUID], HrmConfig(),
                 TreadmillConfig()) is None
    assert match("C5:BE:F4:C1:8C:38", "COROS HEART RATE", [], HrmConfig(),
                 TreadmillConfig()) is None


def test_match_hrm_pinned_address_wins_over_name():
    pinned = HrmConfig(address="c5:be:f4:c1:8c:38")
    assert match("C5:BE:F4:C1:8C:38", "anything", [HRS_UUID], pinned,
                 TreadmillConfig()) is Device.HRM
    assert match("AA:BB:CC:DD:EE:FF", "COROS HEART RATE", [HRS_UUID], pinned,
                 TreadmillConfig()) is None


def test_match_treadmill_needs_vendor_service_and_prefix():
    uuids = [TREADMILL_SERVICE_UUID.upper()]
    assert match("F4:01:0A:1D:94:B1", "Treadmill 9566", uuids, HrmConfig(),
                 TreadmillConfig()) is Device.TREADMILL
    assert match("F4:01:0A:1D:94:B1", "Treadmill 9566", [], HrmConfig(),
                 TreadmillConfig()) is None
    assert match("F4:01:0A:1D:94:B1", "Bike 1", uuids, HrmConfig(), TreadmillConfig()) is None


def test_debouncer():
    d = Debouncer(2.0)
    assert d("A", 0.0)
    assert not d("A", 1.0)
    assert d("B", 1.0)
    assert d("A", 2.0)


# --------------------------------------------------------------------------- fake bleak


class FakeBleakClient:
    """Records calls; notify() and drop() simulate the device."""

    instances: list["FakeBleakClient"] = []
    fail_connect = 0  # number of connect() calls that raise
    drop_during_connect = False
    values = {BATTERY_LEVEL_UUID: b"\x50", STATE_UUID: b"\x0a"}

    def __init__(self, device, disconnected_callback=None, timeout=None):
        self.device = device
        self.cb = disconnected_callback
        self.is_connected = False
        self.handlers = {}
        self.reads = []
        self.writes = []
        FakeBleakClient.instances.append(self)

    async def connect(self):
        if FakeBleakClient.fail_connect:
            FakeBleakClient.fail_connect -= 1
            raise OSError("le-connection-abort-by-local")
        if FakeBleakClient.drop_during_connect:
            self.cb(self)  # BlueZ drops and retries while connect() is running
        self.is_connected = True

    async def start_notify(self, uuid, handler):
        self.handlers[uuid] = handler

    async def read_gatt_char(self, uuid):
        self.reads.append(uuid)
        return bytearray(self.values[uuid])

    async def write_gatt_char(self, *args, **kwargs):
        self.writes.append(args)
        raise AssertionError("must never write")

    async def disconnect(self):
        if self.is_connected:
            self.is_connected = False
            self.cb(self)

    def notify(self, uuid, data: bytes):
        self.handlers[uuid](None, bytearray(data))


@pytest.fixture(autouse=True)
def reset_fake():
    FakeBleakClient.instances = []
    FakeBleakClient.fail_connect = 0
    FakeBleakClient.drop_during_connect = False


def sighting(device=Device.HRM, address="C5:BE:F4:C1:8C:38", name="COROS HEART RATE"):
    return Sighting(device, SimpleNamespace(address=address, name=name), name, -60)


class Events:
    def __init__(self):
        self.log = []

    def __getattr__(self, name):
        return lambda *args: self.log.append((name, *args[1:]))


def make_hrm(ev, **kw):
    return HrmClient(lambda t, s: ev.log.append(("hr", s.bpm)), on_raw=ev.raw,
                     on_connected=ev.connected, on_disconnected=ev.disconnected,
                     client_factory=FakeBleakClient, clock=lambda: 1000.0, **kw)


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


# --------------------------------------------------------------------------- clients


def test_hrm_connects_subscribes_and_reads_battery():
    async def go():
        ev = Events()
        c = make_hrm(ev)
        assert c.offer(sighting())
        assert not c.offer(sighting())  # busy
        await settle()
        fake = FakeBleakClient.instances[0]
        assert c.connected and HR_MEASUREMENT_UUID in fake.handlers
        assert c.battery_pct == 80 and fake.reads == [BATTERY_LEVEL_UUID]
        fake.notify(HR_MEASUREMENT_UUID, b"\x00\x5b")
        fake.notify(HR_MEASUREMENT_UUID, b"\x00\x00")  # off skin
        await c.close()
        return ev.log

    log = asyncio.run(go())
    assert log[0] == ("connected", Device.HRM, "COROS HEART RATE")
    assert ("raw", "C5:BE:F4:C1:8C:38", BATTERY_LEVEL_UUID, b"\x50") in log
    assert [e for e in log if e[0] == "hr"] == [("hr", 91), ("hr", None)]
    assert ("raw", "C5:BE:F4:C1:8C:38", HR_MEASUREMENT_UUID, b"\x00\x5b") in log


def test_disconnect_during_connect_is_ignored():
    async def go():
        FakeBleakClient.drop_during_connect = True
        ev = Events()
        c = make_hrm(ev)
        c.offer(sighting())
        await settle()
        assert c.connected
        await FakeBleakClient.instances[0].disconnect()  # a real drop afterwards counts
        assert not c.connected
        await c.close()
        return ev.log

    log = asyncio.run(go())
    assert [e[0] for e in log if e[0] != "raw"] == ["connected", "disconnected"]


def test_failed_connect_backs_off(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(link.time, "monotonic", lambda: now[0])

    async def go():
        FakeBleakClient.fail_connect = 2
        c = make_hrm(Events())
        assert c.offer(sighting())
        await settle()
        assert not c.connected
        assert not c.offer(sighting())  # within 1 s
        now[0] += 1.0
        assert c.offer(sighting())  # fails again; now 2 s
        await settle()
        now[0] += 1.5
        assert not c.offer(sighting())
        now[0] += 0.5
        assert c.offer(sighting())
        await settle()
        assert c.connected
        await c.close()

    asyncio.run(go())


def test_hrm_silence_watchdog_reconnects(monkeypatch):
    monkeypatch.setattr(hrm_mod, "SILENCE_S", 0.0)

    async def go():
        ev = Events()
        c = make_hrm(ev)
        c.offer(sighting())
        await settle()
        assert not c.connected  # silent at once -> dropped
        await c.close()
        return ev.log

    log = asyncio.run(go())
    assert [e[0] for e in log if e[0] != "raw"] == ["connected", "disconnected"]


def test_hrm_strap_off_the_arm_is_not_reconnected_again(monkeypatch):
    # NAS test: the strap kept accepting connections but sent nothing, and the watchdog
    # reconnected every 20 s, lighting its LED each time.
    monkeypatch.setattr(hrm_mod, "SILENCE_S", 0.0)

    async def go():
        ev = Events()
        c = make_hrm(ev)
        c.offer(sighting())
        await settle()
        assert not c.connected  # first silence: one reconnect
        c.offer(sighting())
        await settle()
        assert c.connected  # still silent: stays connected
        for _ in range(3):
            await asyncio.sleep(0)
        assert c.connected
        FakeBleakClient.instances[-1].notify(HR_MEASUREMENT_UUID, b"\x00\x5b")
        assert not c._silence_retried  # worn again: the watchdog is armed again
        await c.close()
        return ev.log

    log = asyncio.run(go())
    assert [e[0] for e in log if e[0] not in ("raw", "hr")] == [
        "connected", "disconnected", "connected", "disconnected"]  # the last is close()


def test_treadmill_reads_state_after_connect_and_never_writes():
    async def go():
        ev = Events()
        c = TreadmillClient(lambda t, s: ev.log.append(("sample", s)),
                            lambda t, s: ev.log.append(("state", s)),
                            on_raw=ev.raw, on_connected=ev.connected,
                            on_disconnected=ev.disconnected, client_factory=FakeBleakClient,
                            clock=lambda: 1000.0)
        c.offer(sighting(Device.TREADMILL, "F4:01:0A:1D:94:B1", "Treadmill 9566"))
        await settle()
        fake = FakeBleakClient.instances[0]
        assert set(fake.handlers) == {DATA_UUID, STATE_UUID}
        # elapsed 52 s, 2.0 mph, 91 m, 21 kcal, 2.5 METs
        fake.notify(DATA_UUID, bytes.fromhex("53cb1a" "34000000" "4101" "5b00" "000000"
                                             "1500" "19" "0000"))
        fake.notify(STATE_UUID, b"\x80")
        await fake.disconnect()
        await c.close()
        assert fake.writes == []
        return ev.log

    log = asyncio.run(go())
    kinds = [e[0] for e in log if e[0] != "raw"]
    assert kinds == ["connected", "state", "sample", "state", "disconnected"]
    states = [e[1] for e in log if e[0] == "state"]
    assert states == [TreadmillState.RUNNING, TreadmillState.ENDED]
    sample = next(e[1] for e in log if e[0] == "sample")
    assert (sample.elapsed_s, sample.distance_m) == (52, 91)

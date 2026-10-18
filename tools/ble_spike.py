#!/usr/bin/env python3
"""Phase 0 BLE discovery spike: scan, dump and pair-test BLE devices.

Standalone (bleak only). Run on the Pi from the venv:

    ~/venv/bin/python ble_spike.py scan --seconds 20
    ~/venv/bin/python ble_spike.py dump COROS --seconds 120 --out hrm_rest_2min.jsonl
    ~/venv/bin/python ble_spike.py dump COROS TRUE --seconds 180 --out combined_3min.jsonl
    ~/venv/bin/python ble_spike.py pairtest AA:BB:CC:DD:EE:FF

`dump` never writes to any characteristic. It does not even subscribe to the FTMS Control
Point (0x2AD9), because the project rule is to leave the treadmill's control path alone.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError

SIG_BASE = "-0000-1000-8000-00805f9b34fb"
HRS_UUID = "0000180d" + SIG_BASE
FTMS_UUID = "00001826" + SIG_BASE
FTMS_CONTROL_POINT_UUID = "00002ad9" + SIG_BASE
SERVICE_CHANGED_UUID = "00002a05" + SIG_BASE
INTERESTING = {HRS_UUID: "HRS 0x180D", FTMS_UUID: "FTMS 0x1826"}


def short_uuid(uuid: str) -> str:
    """Return '0x180D' for SIG-base UUIDs, else the full UUID."""
    uuid = uuid.lower()
    if uuid.endswith(SIG_BASE) and uuid.startswith("0000"):
        return "0x" + uuid[4:8].upper()
    return uuid


def now_ms() -> int:
    return int(time.time() * 1000)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------- scan


async def cmd_scan(args: argparse.Namespace) -> int:
    seen: dict[str, tuple[BLEDevice, AdvertisementData, int]] = {}

    def on_adv(device: BLEDevice, adv: AdvertisementData) -> None:
        first = device.address not in seen
        prev_count = seen[device.address][2] if not first else 0
        seen[device.address] = (device, adv, prev_count + 1)
        tags = sorted({INTERESTING[u] for u in adv.service_uuids if u in INTERESTING}
                      | {INTERESTING[u] for u in adv.service_data if u in INTERESTING})
        if first or (tags and args.verbose):
            mark = f"  <== {', '.join(tags)}" if tags else ""
            log(f"{device.address} rssi={adv.rssi} name={adv.local_name!r}{mark}")

    log(f"scanning {args.seconds}s (active)...")
    async with BleakScanner(detection_callback=on_adv):
        await asyncio.sleep(args.seconds)

    print()
    print(f"{len(seen)} devices, strongest first:")
    rows = sorted(seen.values(), key=lambda r: -r[1].rssi)
    for device, adv, count in rows:
        tags = [INTERESTING[u] for u in adv.service_uuids if u in INTERESTING]
        tags += [INTERESTING[u] for u in adv.service_data if u in INTERESTING]
        star = "*** " if tags else "    "
        print(f"{star}{device.address}  rssi={adv.rssi:4d}  advs={count:3d}  "
              f"name={adv.local_name!r}  tx={adv.tx_power}")
        if adv.service_uuids:
            print("        services:", ", ".join(short_uuid(u) for u in adv.service_uuids))
        for company, data in adv.manufacturer_data.items():
            print(f"        mfr 0x{company:04X}: {data.hex()}")
        for uuid, data in adv.service_data.items():
            print(f"        svcdata {short_uuid(uuid)}: {data.hex()}")
    return 0


# --------------------------------------------------------------------------- find


async def find_devices(targets: list[str], timeout: float) -> dict[str, BLEDevice | None]:
    """Find each target by exact address (case-insensitive) or by name prefix.

    Uses a single scanner for all targets: BlueZ rejects a second concurrent discovery
    with org.bluez.Error.InProgress.
    """
    found: dict[str, BLEDevice | None] = {t: None for t in targets}
    all_found = asyncio.Event()

    def on_adv(device: BLEDevice, adv: AdvertisementData) -> None:
        name = (adv.local_name or device.name or "").lower()
        for t in targets:
            if found[t] is None and (device.address.lower() == t.lower()
                                     or name.startswith(t.lower())):
                found[t] = device
        if all(found.values()):
            all_found.set()

    async with BleakScanner(detection_callback=on_adv):
        try:
            await asyncio.wait_for(all_found.wait(), timeout=timeout)
        except TimeoutError:
            pass
    return found


async def find_device(target: str, timeout: float) -> BLEDevice | None:
    return (await find_devices([target], timeout))[target]


# --------------------------------------------------------------------------- dump


async def dump_one(target: str, device: BLEDevice, args: argparse.Namespace, out,
                   out_path: Path | None, stop: asyncio.Event,
                   connect_lock: asyncio.Lock) -> None:
    closing = False
    connected = False

    def on_disconnect(_client: BleakClient) -> None:
        if not connected:
            # BlueZ can drop and retry a link while connect() is still in progress.
            log(f"[{target}] link dropped during connect (BlueZ retries)")
            return
        if closing:
            log(f"[{target}] disconnected (end of recording)")
        else:
            log(f"[{target}] DISCONNECTED BY DEVICE OR LINK LOSS")
        stop.set()

    counts: dict[str, int] = {}
    last_t = [time.monotonic()]
    client = BleakClient(device, disconnected_callback=on_disconnect,
                         timeout=args.connect_timeout)
    # Connect one device at a time; the rest of the capture runs concurrently.
    async with connect_lock:
        log(f"[{target}] connecting to {device.address} name={device.name!r}...")
        t_connect = time.monotonic()
        await client.connect()
    connected = True
    try:
        log(f"[{target}] connected in {time.monotonic() - t_connect:.1f}s, "
            f"mtu={client.mtu_size}")
        gatt: dict = {"address": device.address, "name": device.name,
                      "captured_at_ms": now_ms(), "services": []}
        notifiable: list[BleakGATTCharacteristic] = []

        for service in client.services:
            print(f"  service {short_uuid(service.uuid)}  {service.description}")
            svc = {"uuid": service.uuid, "description": service.description,
                   "characteristics": []}
            for char in service.characteristics:
                entry = {"uuid": char.uuid, "handle": char.handle,
                         "description": char.description,
                         "properties": list(char.properties),
                         "descriptors": [d.uuid for d in char.descriptors]}
                line = (f"    char {short_uuid(char.uuid)} h={char.handle}  "
                        f"{char.description}  [{', '.join(char.properties)}]")
                if "read" in char.properties:
                    try:
                        value = await client.read_gatt_char(char)
                        entry["value"] = value.hex()
                        line += f"  = {value.hex()}"
                        if value and all(32 <= b < 127 for b in value):
                            line += f" ({value.decode()!r})"
                    except (BleakError, OSError) as e:
                        entry["read_error"] = str(e)
                        line += f"  read error: {e}"
                print(line)
                svc["characteristics"].append(entry)
                if char.uuid == FTMS_CONTROL_POINT_UUID:
                    print("      (control point: not subscribed, by design)")
                elif char.uuid == SERVICE_CHANGED_UUID:
                    pass  # BlueZ handles Service Changed itself; subscribing fails
                elif {"notify", "indicate"} & set(char.properties):
                    notifiable.append(char)
            gatt["services"].append(svc)

        if out_path is not None:
            gatt_path = out_path.with_name(out_path.name + f".{safe(device.address)}.gatt.json")
            gatt_path.write_text(json.dumps(gatt, indent=2))
            log(f"[{target}] GATT table -> {gatt_path}")

        def make_cb(uuid: str):
            def cb(_sender: BleakGATTCharacteristic, data: bytearray) -> None:
                rec = {"t": now_ms(), "dev": device.address, "char": uuid,
                       "data": data.hex()}
                counts[uuid] = counts.get(uuid, 0) + 1
                gap = time.monotonic() - last_t[0]
                if gap >= args.silence:
                    log(f"[{target}] notifications resumed after {gap:.1f}s")
                last_t[0] = time.monotonic()
                if out is not None:
                    out.write(json.dumps(rec) + "\n")
                    out.flush()
                if not args.quiet:
                    print(f"  {rec['t']} {device.address} {short_uuid(uuid)} {data.hex()}",
                          flush=True)
            return cb

        for char in notifiable:
            try:
                await client.start_notify(char, make_cb(char.uuid))
                log(f"[{target}] subscribed {short_uuid(char.uuid)}")
            except (BleakError, OSError) as e:
                log(f"[{target}] subscribe {short_uuid(char.uuid)} failed: {e}")

        log(f"[{target}] recording for {args.seconds}s (Ctrl-C to stop early)")
        t_start = time.monotonic()
        last_t[0] = t_start
        warned_at = 0.0
        while not stop.is_set():
            remaining = args.seconds - (time.monotonic() - t_start)
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=min(1.0, remaining))
            except TimeoutError:
                pass
            quiet = time.monotonic() - last_t[0]
            if quiet >= args.silence and quiet - warned_at >= args.silence:
                log(f"[{target}] no notifications for {quiet:.0f}s "
                    f"(connected={client.is_connected}), {remaining:.0f}s left")
                warned_at = quiet
            elif quiet < args.silence:
                warned_at = 0.0
    finally:
        closing = True
        await client.disconnect()

    summary = ", ".join(f"{short_uuid(u)}={n}" for u, n in counts.items()) or "none"
    log(f"[{target}] done; notifications: {summary}")


def safe(address: str) -> str:
    return address.replace(":", "")


async def cmd_dump(args: argparse.Namespace) -> int:
    out_path = Path(args.out) if args.out else None
    out = None
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out = out_path.open("a", encoding="utf-8")
    try:
        log(f"looking for {', '.join(args.targets)} (up to {args.find_timeout}s)...")
        devices = await find_devices(args.targets, args.find_timeout)
        for target, device in devices.items():
            if device is None:
                log(f"[{target}] NOT FOUND")
            else:
                log(f"[{target}] found {device.address} name={device.name!r}")
        present = [t for t, d in devices.items() if d is not None]
        # One stop event per device: a disconnect ends only that device's capture.
        connect_lock = asyncio.Lock()
        tasks = [dump_one(t, devices[t], args, out, out_path, asyncio.Event(), connect_lock)
                 for t in present]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for target, result in zip(present, results):
            if isinstance(result, BaseException):
                log(f"[{target}] FAILED: {type(result).__name__}: {result}")
    finally:
        if out is not None:
            out.close()
            log(f"notifications -> {out_path}")
    return 0


# --------------------------------------------------------------------------- pairtest


async def cmd_pairtest(args: argparse.Namespace) -> int:
    device = await find_device(args.target, args.find_timeout)
    if device is None:
        log("NOT FOUND")
        return 1
    log(f"found {device.address} name={device.name!r}; connecting...")
    async with BleakClient(device, timeout=args.connect_timeout) as client:
        log("connected without pairing")
        try:
            await client.pair()
            log("pair(): OK (no passkey prompt, or Just Works)")
        except (BleakError, OSError) as e:
            log(f"pair(): FAILED: {type(e).__name__}: {e}")
            log("a failure mentioning Authentication or an agent usually means "
                "the device wants a passkey")
    log(f"check bond state with: bluetoothctl info {device.address}")
    log(f"remove the bond with:  bluetoothctl remove {device.address}")
    return 0


# --------------------------------------------------------------------------- main


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="print every advertisement")
    s.add_argument("--seconds", type=float, default=15)
    s.add_argument("-v", "--verbose", action="store_true",
                   help="print every HRS/FTMS advertisement, not just the first")

    d = sub.add_parser("dump", help="connect, dump GATT, record notifications")
    d.add_argument("targets", nargs="+", help="address or name prefix (one or more)")
    d.add_argument("--seconds", type=float, default=60)
    d.add_argument("--out", help="JSONL file to append notifications to")
    d.add_argument("--quiet", action="store_true", help="don't print each notification")
    d.add_argument("--silence", type=float, default=5,
                   help="warn when no notifications arrive for this many seconds")

    t = sub.add_parser("pairtest", help="try BleakClient.pair()")
    t.add_argument("target", help="address or name prefix")

    for sp in (d, t):
        sp.add_argument("--find-timeout", type=float, default=30)
        sp.add_argument("--connect-timeout", type=float, default=20)

    args = p.parse_args()
    handler = {"scan": cmd_scan, "dump": cmd_dump, "pairtest": cmd_pairtest}[args.cmd]
    try:
        return asyncio.run(handler(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

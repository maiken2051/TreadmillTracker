# TreadmillTracker

Records treadmill workouts on a Raspberry Pi with a touchscreen, sitting on the treadmill:

- **Heart rate** from a BLE heart-rate strap (standard Heart Rate Service, tested with a
  COROS HR armband).
- **Speed, incline, distance, time and calories** from a **TRUE Performance 8000**
  treadmill. Its console does *not* speak the standard FTMS profile; it uses an
  undocumented vendor BLE protocol (service `a026ee07-0a7d-4ab3-97fa-f1500f9feb8b`) that
  this project decodes. The same service shows up on other Zwift-compatible treadmills.

Workouts are kept private: a SQLite database on the Pi, plus a spreadsheet and backups
copied to a network share. No cloud services, no accounts. It replaces the abandoned
GymTrakr iPhone app.

**Status:** under construction. Bluetooth discovery is done and the protocol is decoded;
the parsers are written and tested. Recording, storage, the touchscreen UI and history
pages come next. A full write-up of the treadmill protocol will be in `docs/PROTOCOL.md`.

## Hardware

- Raspberry Pi 5 (4 GB) with the onboard Bluetooth radio, running Raspberry Pi OS
- Raspberry Pi Touch Display 2 (7")
- TRUE Performance 8000 treadmill (press the console's Bluetooth button to start advertising
  as `Treadmill NNNN` for about a minute)
- Any BLE heart-rate strap; a COROS HR31 armband was used here

## Repository layout

| Path | What it is |
|---|---|
| `src/gymtracker/ble/` | Parsers (and, later, clients) for the strap and the treadmill |
| `src/gymtracker/sim/` | Replays recorded captures as fake devices, so everything runs without hardware |
| `tests/fixtures/` | Real captures from the devices: one JSON line per BLE notification, plus each device's GATT table |
| `tools/ble_spike.py` | Discovery and capture tool, run on the Pi |

## Development

Everything except the Bluetooth and display code runs on Windows, macOS or Linux, using
recorded captures from the real devices (`tests/fixtures/*.jsonl`).

```sh
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"   # .venv/bin/pip on Linux/macOS
.venv/Scripts/pytest
.venv/Scripts/ruff check .
```

Replay a capture as a 1 Hz table:

```sh
python -m gymtracker --replay tests/fixtures/treadmill_calib.jsonl
```

`tools/ble_spike.py` is the discovery tool used to capture the fixtures on the Pi
(scan, dump the GATT table, record notifications). It only reads and subscribes; it never
writes to the treadmill.

## License

MIT

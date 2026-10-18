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

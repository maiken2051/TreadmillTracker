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
the parsers and the workout state machine (start, pause, resume, end, from the console's own
events) are written and tested against real captures, and so is storage (SQLite, a
write-once archive file per workout, and a rebuilt-every-time `Workouts.xlsx`). The live
Bluetooth clients, NAS export, touchscreen UI and history pages come next. A full write-up of the treadmill protocol
will be in `docs/PROTOCOL.md`.

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
| `src/gymtracker/state.py`, `session.py`, `recorder.py` | Workout state machine, 1 Hz records and totals, and the glue between them |
| `src/gymtracker/store.py`, `archive.py`, `sheet.py`, `library.py` | SQLite database, per-workout archive files, spreadsheet, and the facade over all three |
| `src/gymtracker/importers/` | `.fit` history import |
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

Replay a capture through the recorder (state changes and the resulting workout), or as a
raw 1 Hz table:

```sh
python -m gymtracker --replay tests/fixtures/treadmill_calib.jsonl
python -m gymtracker --replay tests/fixtures/treadmill_calib.jsonl --table
```

Storage commands (all take `--data-dir DIR`; the default is `~/workouts`):

```sh
gymtracker import-fit OLD_FIT_FOLDER --label gymtrakr   # bring in old .fit workouts
gymtracker list
gymtracker sheet                                        # rebuild Workouts.xlsx
gymtracker rebuild-db --out new.db [--reparse]          # recreate the DB from the archive
```

Importing `.fit` files needs the extra: `pip install -e ".[fit]"`.

Settings live in `config.toml` (not committed); `config.example.toml` lists every key with
its default.

`tools/ble_spike.py` is the discovery tool used to capture the fixtures on the Pi
(scan, dump the GATT table, record notifications). It only reads and subscribes; it never
writes to the treadmill.

## License

MIT

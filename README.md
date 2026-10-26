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

**Status:** in daily use. The Pi records workouts by itself: press the treadmill console's
Bluetooth button, walk, press Stop. It shows heart rate (coloured by zone), time, speed,
distance, pace and calories on a 7" touchscreen, saves the workout to SQLite plus a
write-once archive file, rebuilds `Workouts.xlsx`, and copies all of it to a network share.
Still to come: history pages and printable reports (to replace Strava), and a full
write-up of the treadmill protocol in `docs/PROTOCOL.md`.

## How a workout goes

1. Put the strap on, then press **Bluetooth** on the treadmill console. The screen wakes
   up and shows the setup steps; the treadmill connects, then the strap.
2. Press **Quick Start**. Recording starts with the belt; the console's Pause and Stop
   are followed. (The app only listens: it never sends anything to the treadmill.)
3. After Stop, the screen shows a summary (heart rate and speed min/avg/max, NAS status,
   strap battery). Tap **Done**, or leave it; the screen goes dark after 5 minutes.

Only the treadmill wakes the app. A COROS strap keeps advertising and accepting
connections for about 10 minutes after it comes off the arm, so it is not a reliable
"someone is here" signal; it is connected only once the app is awake.

## Hardware

- Raspberry Pi 5 (4 GB) with the onboard Bluetooth radio, running Raspberry Pi OS
  (Debian 13) with the desktop and auto-login
- A 7" DSI touchscreen: a Waveshare 7inch DSI LCD (H), 1280×720 (the official Touch Display 2
  should work too; skip the display step of the installer and set the rotation yourself)
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
| `src/gymtracker/app.py`, `export.py` | The running app (BLE, recorder, saving, status) and the copy to the network share |
| `src/gymtracker/web/` | The touchscreen page (one HTML file) and its aiohttp server |
| `deploy/` | Everything the Pi needs to run by itself; `deploy/install.sh` sets it up |
| `tools/deploy.sh` | Updates the Pi from a checkout without interrupting a workout |
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

## Running it on the Pi

First time, on the Pi (as the desktop user, with sudo):

```sh
mkdir -p ~/gymtracker/app            # then copy the repo there, e.g. from a PC:
# git archive HEAD | ssh PI 'tar -x -C ~/gymtracker/app'
sh ~/gymtracker/app/deploy/install.sh
```

`install.sh` is safe to run again. It installs the app into `~/venv`, runs it as a systemd
**user** service that starts at boot, opens the page full-screen at login (Chromium kiosk),
lets the app switch the backlight (udev rule), mounts the network share on demand
(`deploy/fstab.snippet`: edit the share path first), turns on automatic updates, and replaces
their 04:00 reboot with one that waits until no workout is running. `GT_DISPLAY=no` skips
the Waveshare-specific display step.

Updating from a PC checkout: `sh tools/deploy.sh [ssh-host]`. It installs `HEAD` and
restarts the app as soon as no workout is in progress; the page reloads itself.

Where things are on the Pi:

| What | Where |
|---|---|
| Workouts | `~/workouts/gymtracker.db`, `~/workouts/archive/YYYY/*.json.gz`, `~/workouts/Workouts.xlsx` |
| Raw Bluetooth logs (replayable) | `~/workouts/logs/*.jsonl` |
| App log | `journalctl --user-unit gymtracker -f` |
| Reboots for updates | `journalctl -t gymtracker`; `systemctl list-timers gymtracker-reboot.timer` |
| The page | `http://PI:8080/` from any browser on the LAN (also a phone) |
| Copy to the share now | `~/venv/bin/gymtracker export` |

To run the app by hand (e.g. with `-v` to watch the connection details), stop the service
first: `systemctl --user stop gymtracker`, and `start` it again afterwards.

To shut the Pi down before unplugging it, press the Pi 5 power button twice (the first
press opens the shutdown menu, the second shuts down); holding it is a hard power-off.

## License

MIT

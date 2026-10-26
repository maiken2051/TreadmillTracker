"""Render README screenshots of the touchscreen page from made-up states (never real workouts).

    python tools/screenshots.py [--chrome PATH]

Writes docs/screenshots/*.png at the touchscreen's 1280x720. Each state is injected into a
copy of the page by replacing its WebSocket, so no server or devices are needed; Chrome or
Edge runs headless to take the picture.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "src" / "gymtracker" / "web" / "static" / "index.html"
OUT = ROOT / "docs" / "screenshots"
CHROMES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "google-chrome", "chromium", "chromium-browser",
]

BASE = {
    "started": 1, "clock": "07:42", "units": {"speed": "mph", "distance": "mi", "pace": "/mi"},
    "hr": None, "hr_zone": None,
    "hrm": {"connected": False, "name": "", "battery_pct": None},
    "treadmill": {"connected": False, "name": ""},
    "nas": {"enabled": True, "pending": False, "error": ""},
    "end_reason": None, "live": None, "summary": None,
}
STRAP = {"connected": True, "name": "HR strap", "battery_pct": 86}
TREADMILL = {"connected": True, "name": "Treadmill 1234"}
STATES = {
    "ready": dict(BASE, state="ready", treadmill=TREADMILL),
    "recording": dict(BASE, state="recording", hr=128, hr_zone=2, hrm=STRAP, treadmill=TREADMILL,
                      live={"elapsed_s": 1458, "speed": 3.2, "distance": 1.27, "pace_s": 1125,
                            "kcal": 162, "incline_pct": 2.0}),
    "finished": dict(BASE, state="finished", end_reason="console ended", hrm=STRAP,
                     summary={"workout_id": 42, "moving_s": 2712, "distance": 2.31, "kcal": 287,
                              "min_hr": 96, "avg_hr": 124, "max_hr": 139, "min_speed": 2.8,
                              "avg_speed": 3.1, "max_speed": 3.4, "pauses": 1}),
}


def fake_socket(state: dict) -> str:
    return ("<script>window.WebSocket = function () { const ws = {};"
            " setTimeout(() => ws.onopen && ws.onopen(), 0);"
            " setTimeout(() => ws.onmessage && ws.onmessage({data: JSON.stringify("
            + json.dumps(state) +
            ")}), 10); return ws; }; window.fetch = () => new Promise(() => {});</script>\n")


def find_chrome(given: str | None) -> str:
    for candidate in [given] + CHROMES if given else CHROMES:
        if candidate and (Path(candidate).exists() or shutil.which(candidate)):
            return candidate
    raise SystemExit("no Chrome/Edge found; pass --chrome PATH")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--chrome")
    args = ap.parse_args()
    chrome = find_chrome(args.chrome)
    page = PAGE.read_text(encoding="utf-8")
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for name, state in STATES.items():
            html = Path(tmp) / f"{name}.html"
            html.write_text(page.replace("<script>", fake_socket(state) + "<script>", 1),
                            encoding="utf-8")
            png = OUT / f"{name}.png"
            subprocess.run([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                            "--window-size=1280,720", "--virtual-time-budget=1000",
                            f"--user-data-dir={Path(tmp) / 'profile'}",
                            f"--screenshot={png}", html.as_uri()],
                           check=True, capture_output=True)
            print(png.relative_to(ROOT))


if __name__ == "__main__":
    main()

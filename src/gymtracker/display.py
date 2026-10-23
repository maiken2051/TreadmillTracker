"""Screen backlight on/off through sysfs (DESIGN.md §4.4: off only in IDLE).

`bl_power` is 0 for on and 4 (FB_BLANK_POWERDOWN) for off. Anywhere without a writable
backlight (Windows, SSH-only testing, a display without one) this does nothing and warns once.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

SYS_BACKLIGHT = Path("/sys/class/backlight")
_ON, _OFF = "0", "4"


def find_backlight(setting: str = "auto", root: Path = SYS_BACKLIGHT) -> Path | None:
    """The bl_power file for `setting` ("auto" = the first backlight found), or None."""
    if setting != "auto":
        path = Path(setting) / "bl_power"
        return path if path.exists() else None
    if not root.is_dir():
        return None
    for device in sorted(root.iterdir()):
        if (device / "bl_power").exists():
            return device / "bl_power"
    return None


class Backlight:
    def __init__(self, setting: str = "auto", root: Path = SYS_BACKLIGHT) -> None:
        self.path = find_backlight(setting, root)
        self.on: bool | None = None
        if self.path is None:
            log.warning("no backlight found (%s); screen on/off is disabled", setting)
        else:
            log.info("backlight: %s", self.path)

    def set(self, on: bool) -> None:
        if on == self.on:
            return
        self.on = on
        if self.path is None:
            return
        try:
            self.path.write_text(_ON if on else _OFF)
        except OSError as e:
            log.warning("cannot write %s (%s); screen on/off is disabled", self.path, e)
            self.path = None


class NullBacklight:
    """Stands in for Backlight in replay mode."""

    def __init__(self) -> None:
        self.on: bool | None = None

    def set(self, on: bool) -> None:
        self.on = on

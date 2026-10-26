"""Configuration: config.toml (see config.example.toml), validated against these defaults."""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class HrmConfig:
    name_prefix: str = "COROS"
    address: str = ""  # optional: pin to one strap's address


@dataclass
class TreadmillConfig:
    name_prefix: str = "Treadmill"  # advertised as "Treadmill NNNN"; address changes every time


@dataclass
class SessionConfig:
    start_speed_kmh: float = 0.5  # speed fallback, only used if no state events arrive
    start_hold_s: float = 3.0
    autopause_after_s: float = 10.0
    end_after_paused_s: float = 180.0
    wake_timeout_s: float = 60.0  # WAKING with nothing connected -> IDLE
    ready_timeout_s: float = 600.0  # READY without a start -> IDLE
    idle_return_s: float = 300.0  # FINISHED -> IDLE
    min_session_s: float = 60.0  # shorter workouts are discarded
    checkpoint_s: float = 30.0
    lap_distance_m: float = 1000.0


@dataclass
class UserConfig:
    units: str = "imperial"  # or "metric"; everything user-facing. Storage is SI.
    max_hr: int = 0  # for HR zones; 0 = use hr_zones
    hr_zones: list[int] = field(default_factory=lambda: [100, 115, 130, 145, 160])


@dataclass
class StorageConfig:
    data_dir: str = "~/workouts"  # gymtracker.db, archive/, logs/


@dataclass
class ExportConfig:
    smb_mount: str = "/mnt/nas-workouts"  # "" turns the NAS copy off
    smb_subdir: str = ""  # folder inside the mount; "" when the mount is the folder itself
    spreadsheet: str = "Workouts.xlsx"


@dataclass
class DisplayConfig:
    backlight: str = "auto"  # or an explicit /sys/class/backlight/<name>


@dataclass
class WebConfig:
    port: int = 8080


@dataclass
class Config:
    hrm: HrmConfig = field(default_factory=HrmConfig)
    treadmill: TreadmillConfig = field(default_factory=TreadmillConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    user: UserConfig = field(default_factory=UserConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    web: WebConfig = field(default_factory=WebConfig)


def load_config(path: str | Path | None = None) -> Config:
    """Defaults, overridden by the TOML file if given. Unknown or mistyped keys are errors."""
    cfg = Config()
    if path is None:
        return cfg
    with open(path, "rb") as f:
        data = tomllib.load(f)
    for section, values in data.items():
        if section not in cfg.__dataclass_fields__ or not isinstance(values, dict):
            raise ValueError(f"{path}: unknown section [{section}]")
        _apply(getattr(cfg, section), values, f"{path} [{section}]")
    if cfg.user.units not in ("imperial", "metric"):
        raise ValueError(f"{path} [user]: units must be 'imperial' or 'metric'")
    return cfg


def _apply(obj: object, values: dict, where: str) -> None:
    fields = {f.name: f for f in dataclasses.fields(obj)}
    for key, value in values.items():
        if key not in fields:
            raise ValueError(f"{where}: unknown key {key!r}")
        current = getattr(obj, key)
        if isinstance(current, float) and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if type(value) is not type(current):
            raise ValueError(f"{where}: {key} should be {type(current).__name__}, "
                             f"not {type(value).__name__}")
        setattr(obj, key, value)

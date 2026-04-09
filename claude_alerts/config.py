"""Configuration loading from TOML with built-in defaults."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


@dataclass
class Config:
    color_working: str = "#22c55e"
    color_waiting: str = "#ef4444"
    border_thickness_px: int = 4
    log_level: str = "INFO"


def load_config(path: Path) -> Config:
    """Load config from TOML file. Missing file => all defaults."""
    cfg = Config()
    if not path.exists():
        return cfg

    with path.open("rb") as f:
        data = tomllib.load(f)

    colors = data.get("colors", {})
    if "working" in colors:
        cfg.color_working = str(colors["working"])
    if "waiting" in colors:
        cfg.color_waiting = str(colors["waiting"])

    border = data.get("border", {})
    if "thickness_px" in border:
        cfg.border_thickness_px = int(border["thickness_px"])

    debug = data.get("debug", {})
    if "log_level" in debug:
        cfg.log_level = str(debug["log_level"])

    return cfg

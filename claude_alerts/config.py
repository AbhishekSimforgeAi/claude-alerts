"""Configuration loading from TOML with built-in defaults."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

# tomllib is stdlib in Python >= 3.11; tomli is the backport declared in pyproject.toml.
if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


class ConfigError(ValueError):
    """Raised when the config file cannot be parsed or contains invalid values."""


@dataclass(frozen=True)
class Config:
    color_working: str = "#22c55e"
    color_waiting: str = "#ef4444"
    border_thickness_px: int = 4
    log_level: str = "INFO"


def _require_str(value, field: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(
            f"{field} must be a string, got {type(value).__name__}"
        )
    return value


def _require_int(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"{field} must be an integer, got {type(value).__name__}"
        )
    return value


def load_config(path: Path) -> Config:
    """Load config from TOML file. Missing file => all defaults.

    Raises ConfigError on malformed TOML or invalid field values.
    """
    if not path.exists():
        return Config()

    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError) as e:
        raise ConfigError(f"cannot parse config file {path}: {e}") from e

    if not isinstance(data, dict):
        raise ConfigError(f"config file {path} must be a TOML table at the root")

    overrides: dict[str, object] = {}

    colors = data.get("colors", {})
    if "working" in colors:
        overrides["color_working"] = _require_str(colors["working"], "colors.working")
    if "waiting" in colors:
        overrides["color_waiting"] = _require_str(colors["waiting"], "colors.waiting")

    border = data.get("border", {})
    if "thickness_px" in border:
        overrides["border_thickness_px"] = _require_int(
            border["thickness_px"], "border.thickness_px"
        )

    debug = data.get("debug", {})
    if "log_level" in debug:
        overrides["log_level"] = _require_str(debug["log_level"], "debug.log_level")

    return Config(**overrides)

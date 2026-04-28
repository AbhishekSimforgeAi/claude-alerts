"""Read rate-limit data captured by scripts/hooks/statusline.sh.

The statusLine helper writes a sidecar JSON file every time Claude Code
re-renders its prompt status. We read it on demand from the dashboard.

Sidecar shape (matches the `rate_limits` object Claude Code feeds to the
statusLine subprocess, with a `saved_at` Unix timestamp added):

```json
{
  "saved_at": 1777373944.391,
  "rate_limits": {
    "five_hour":       {"used_percentage": 42.5, "resets_at": 1777368000},
    "seven_day":       {"used_percentage": 18.3, "resets_at": 1777886400},
    "seven_day_opus":  {"used_percentage": 56.0, "resets_at": 1777886400},
    "seven_day_sonnet":{"used_percentage": 12.0, "resets_at": 1777886400}
  }
}
```

Each window is independently optional — Claude Code only emits the keys
it has data for. Returning a partially-populated `RateLimits` is normal.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# Order matters: this is the order rows render in the dashboard.
WINDOW_KEYS = ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet")
WINDOW_LABELS = {
    "five_hour":        "5-hour",
    "seven_day":        "weekly",
    "seven_day_opus":   "Opus",
    "seven_day_sonnet": "Sonnet",
}


@dataclass(frozen=True)
class Limit:
    used_percentage: float
    resets_at: int  # unix epoch seconds


@dataclass(frozen=True)
class RateLimits:
    saved_at: float = 0.0  # unix seconds when sidecar was written
    five_hour: Optional[Limit] = None
    seven_day: Optional[Limit] = None
    seven_day_opus: Optional[Limit] = None
    seven_day_sonnet: Optional[Limit] = None

    def get(self, key: str) -> Optional[Limit]:
        return getattr(self, key, None)

    def any_present(self) -> bool:
        return any(self.get(k) is not None for k in WINDOW_KEYS)


def default_sidecar_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "claude-alerts" / "rate_limits.json"


def _parse_limit(d: object) -> Optional[Limit]:
    if not isinstance(d, dict):
        return None
    try:
        return Limit(
            used_percentage=float(d["used_percentage"]),
            resets_at=int(d["resets_at"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def load(path: Path) -> Optional[RateLimits]:
    """Read and parse the sidecar. Returns None if missing/malformed."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as e:
        log.debug("cannot read %s: %s", path, e)
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.debug("malformed sidecar %s", path)
        return None
    if not isinstance(data, dict):
        return None
    rl = data.get("rate_limits")
    if not isinstance(rl, dict):
        return None
    return RateLimits(
        saved_at=float(data.get("saved_at", 0.0) or 0.0),
        five_hour=_parse_limit(rl.get("five_hour")),
        seven_day=_parse_limit(rl.get("seven_day")),
        seven_day_opus=_parse_limit(rl.get("seven_day_opus")),
        seven_day_sonnet=_parse_limit(rl.get("seven_day_sonnet")),
    )

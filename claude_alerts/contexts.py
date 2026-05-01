"""Read per-session context-window data captured by scripts/hooks/statusline.sh.

statusline.sh writes one sidecar per active Claude Code session every
prompt update:

```json
{
  "saved_at": 1777643245.812,
  "session_id": "abc",
  "context_window": {
    "context_window_size": 200000,
    "used_percentage": 7.9,
    "current_usage": {
      "input_tokens": 8500,
      "output_tokens": 1200,
      "cache_creation_input_tokens": 5000,
      "cache_read_input_tokens": 2000
    }
  }
}
```

`current_usage` is null before the first API call. `used_percentage` and
`context_window_size` may also be null/missing early in the session. The
formatter renders `—` for any field that's None.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContextUsage:
    saved_at: float
    used_percentage: Optional[float]
    used_tokens: Optional[int]
    total_tokens: Optional[int]


def default_contexts_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "claude-alerts" / "contexts"


def _sidecar_path(session_id: str, base_dir: Path) -> Path:
    return base_dir / f"{session_id}.json"


def load(session_id: str, base_dir: Path) -> Optional[ContextUsage]:
    """Read and parse the per-session sidecar.

    Returns None only when the file is missing, unreadable, or the JSON
    is malformed/wrong-shape. If the file parses but individual fields
    are missing or unusable (current_usage null, context_window_size <= 0,
    used_percentage null), the corresponding ContextUsage field is left
    as None and the formatter renders '—'.
    """
    path = _sidecar_path(session_id, base_dir)
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
    cw = data.get("context_window")
    if not isinstance(cw, dict):
        return None

    saved_at = float(data.get("saved_at", 0.0) or 0.0)

    total = cw.get("context_window_size")
    try:
        total_tokens: Optional[int] = int(total) if total is not None else None
    except (TypeError, ValueError):
        total_tokens = None
    if total_tokens is not None and total_tokens <= 0:
        total_tokens = None

    pct = cw.get("used_percentage")
    try:
        used_pct: Optional[float] = float(pct) if pct is not None else None
    except (TypeError, ValueError):
        used_pct = None

    cu = cw.get("current_usage")
    used_tokens: Optional[int] = None
    if isinstance(cu, dict):
        try:
            used_tokens = (
                int(cu.get("input_tokens") or 0)
                + int(cu.get("cache_creation_input_tokens") or 0)
                + int(cu.get("cache_read_input_tokens") or 0)
            )
        except (TypeError, ValueError):
            used_tokens = None

    return ContextUsage(
        saved_at=saved_at,
        used_percentage=used_pct,
        used_tokens=used_tokens,
        total_tokens=total_tokens,
    )

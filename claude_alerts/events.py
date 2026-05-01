"""Event types and parsing for Claude Code hook events."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

VALID_EVENTS = frozenset({
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "Notification",
    "PermissionRequest",
    "Elicitation",
    "SessionEnd",
})

REQUIRED_FIELDS = ("event", "session_id", "cwd", "claude_pid", "timestamp")


class EventParseError(ValueError):
    """Raised when an event file cannot be parsed."""


@dataclass(frozen=True)
class ClaudeEvent:
    event: str
    session_id: str
    cwd: str
    claude_pid: int
    timestamp: float
    tool_name: Optional[str] = None


def parse_event_file(path: Path) -> ClaudeEvent:
    """Parse a hook-emitted JSON event file. Raises EventParseError on any problem."""
    try:
        text = path.read_text()
    except OSError as e:
        raise EventParseError(f"cannot read event file: {e}") from e

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise EventParseError(f"invalid json: {e}") from e

    if not isinstance(raw, dict):
        raise EventParseError("invalid json: not an object")

    for field in REQUIRED_FIELDS:
        if field not in raw:
            raise EventParseError(f"missing field: {field}")

    # Strict type validation — no silent coercion.
    for field in ("event", "session_id", "cwd"):
        value = raw[field]
        if not isinstance(value, str):
            raise EventParseError(
                f"field {field!r} must be str, got {type(value).__name__}"
            )

    pid = raw["claude_pid"]
    if isinstance(pid, bool) or not isinstance(pid, int):
        raise EventParseError(
            f"field 'claude_pid' must be int, got {type(pid).__name__}"
        )

    ts = raw["timestamp"]
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        raise EventParseError(
            f"field 'timestamp' must be int or float, got {type(ts).__name__}"
        )
    ts = float(ts)  # safe — already validated as numeric

    tool_name = raw.get("tool_name")
    if tool_name is not None and not isinstance(tool_name, str):
        raise EventParseError(
            f"field 'tool_name' must be str or absent, got {type(tool_name).__name__}"
        )

    if raw["event"] not in VALID_EVENTS:
        raise EventParseError(f"unknown event: {raw['event']}")

    return ClaudeEvent(
        event=raw["event"],
        session_id=raw["session_id"],
        cwd=raw["cwd"],
        claude_pid=pid,
        timestamp=ts,
        tool_name=tool_name,
    )

"""Event types and parsing for Claude Code hook events."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

VALID_EVENTS = frozenset({
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "Notification",
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


def parse_event_file(path: Path) -> ClaudeEvent:
    """Parse a hook-emitted JSON event file. Raises EventParseError on any problem."""
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise EventParseError(f"invalid json: {e}") from e

    if not isinstance(raw, dict):
        raise EventParseError("invalid json: not an object")

    for field in REQUIRED_FIELDS:
        if field not in raw:
            raise EventParseError(f"missing field: {field}")

    if raw["event"] not in VALID_EVENTS:
        raise EventParseError(f"unknown event: {raw['event']}")

    return ClaudeEvent(
        event=raw["event"],
        session_id=str(raw["session_id"]),
        cwd=str(raw["cwd"]),
        claude_pid=int(raw["claude_pid"]),
        timestamp=float(raw["timestamp"]),
    )

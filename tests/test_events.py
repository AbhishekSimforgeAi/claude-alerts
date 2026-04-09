import json
from pathlib import Path

import pytest

from claude_alerts.events import ClaudeEvent, EventParseError, parse_event_file


def write_event(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "evt.json"
    p.write_text(json.dumps(payload))
    return p


def test_parses_valid_event(tmp_path):
    p = write_event(
        tmp_path,
        {
            "event": "PreToolUse",
            "session_id": "abc123",
            "cwd": "/home/u/proj",
            "claude_pid": 4242,
            "timestamp": 1712599823.123,
        },
    )
    evt = parse_event_file(p)
    assert evt == ClaudeEvent(
        event="PreToolUse",
        session_id="abc123",
        cwd="/home/u/proj",
        claude_pid=4242,
        timestamp=1712599823.123,
    )


def test_rejects_unknown_event(tmp_path):
    p = write_event(tmp_path, {"event": "Frobnicate", "session_id": "x", "cwd": "/", "claude_pid": 1, "timestamp": 0})
    with pytest.raises(EventParseError, match="unknown event"):
        parse_event_file(p)


def test_rejects_missing_field(tmp_path):
    p = write_event(tmp_path, {"event": "Stop", "session_id": "x"})
    with pytest.raises(EventParseError, match="missing field"):
        parse_event_file(p)


def test_rejects_invalid_json(tmp_path):
    p = tmp_path / "evt.json"
    p.write_text("{not json")
    with pytest.raises(EventParseError, match="invalid json"):
        parse_event_file(p)

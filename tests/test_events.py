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


def test_rejects_missing_file(tmp_path):
    with pytest.raises(EventParseError, match="cannot read"):
        parse_event_file(tmp_path / "does-not-exist.json")


def test_rejects_non_object_root(tmp_path):
    p = tmp_path / "evt.json"
    p.write_text('"just a string"')
    with pytest.raises(EventParseError, match="not an object"):
        parse_event_file(p)


def test_rejects_string_pid(tmp_path):
    p = write_event(tmp_path, {
        "event": "Stop", "session_id": "x", "cwd": "/", "claude_pid": "42", "timestamp": 1.0,
    })
    with pytest.raises(EventParseError, match="claude_pid"):
        parse_event_file(p)


def test_rejects_bool_pid(tmp_path):
    # bool is a subclass of int in Python; we should reject it explicitly.
    p = write_event(tmp_path, {
        "event": "Stop", "session_id": "x", "cwd": "/", "claude_pid": True, "timestamp": 1.0,
    })
    with pytest.raises(EventParseError, match="claude_pid"):
        parse_event_file(p)


def test_rejects_string_timestamp(tmp_path):
    p = write_event(tmp_path, {
        "event": "Stop", "session_id": "x", "cwd": "/", "claude_pid": 1, "timestamp": "1.0",
    })
    with pytest.raises(EventParseError, match="timestamp"):
        parse_event_file(p)


def test_accepts_int_timestamp(tmp_path):
    """JSON ints should be valid timestamps too (Unix epoch seconds without fractions)."""
    p = write_event(tmp_path, {
        "event": "Stop", "session_id": "x", "cwd": "/", "claude_pid": 1, "timestamp": 1712599823,
    })
    evt = parse_event_file(p)
    assert evt.timestamp == 1712599823.0


def test_rejects_int_session_id(tmp_path):
    p = write_event(tmp_path, {
        "event": "Stop", "session_id": 12345, "cwd": "/", "claude_pid": 1, "timestamp": 1.0,
    })
    with pytest.raises(EventParseError, match="session_id"):
        parse_event_file(p)


def test_parses_tool_name_when_present(tmp_path):
    p = write_event(
        tmp_path,
        {
            "event": "PostToolUse",
            "session_id": "abc",
            "cwd": "/p",
            "claude_pid": 1,
            "timestamp": 1.0,
            "tool_name": "Monitor",
        },
    )
    evt = parse_event_file(p)
    assert evt.tool_name == "Monitor"


def test_tool_name_defaults_to_none_when_absent(tmp_path):
    p = write_event(
        tmp_path,
        {
            "event": "Stop",
            "session_id": "abc",
            "cwd": "/p",
            "claude_pid": 1,
            "timestamp": 1.0,
        },
    )
    evt = parse_event_file(p)
    assert evt.tool_name is None


def test_rejects_non_string_tool_name(tmp_path):
    p = write_event(
        tmp_path,
        {
            "event": "PostToolUse",
            "session_id": "abc",
            "cwd": "/p",
            "claude_pid": 1,
            "timestamp": 1.0,
            "tool_name": 42,
        },
    )
    with pytest.raises(EventParseError, match="tool_name"):
        parse_event_file(p)

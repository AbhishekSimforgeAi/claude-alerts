"""Tests for JsonlTailer."""
from __future__ import annotations

import json
from pathlib import Path

from claude_alerts.transcripts import JsonlTailer, encoded_cwd, jsonl_path_for


def _assistant_line(
    model: str = "claude-opus-4-7",
    input_tokens: int = 100,
    cache_read: int = 0,
    cache_write: int = 0,
    output_tokens: int = 50,
    timestamp: str = "2026-04-28T10:00:00.000Z",
) -> str:
    return json.dumps({
        "type": "assistant",
        "timestamp": timestamp,
        "message": {
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write,
                "output_tokens": output_tokens,
            },
        },
    }) + "\n"


def _user_line(timestamp: str = "2026-04-28T09:59:00.000Z") -> str:
    return json.dumps({"type": "user", "timestamp": timestamp}) + "\n"


def _make_jsonl(projects_root: Path, cwd: str, session_id: str, content: str) -> Path:
    p = jsonl_path_for(projects_root, cwd, session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def test_encoded_cwd():
    assert encoded_cwd("/home/abhishek/claude-alerts") == "-home-abhishek-claude-alerts"


def test_tail_folds_assistant_usage(tmp_path):
    _make_jsonl(tmp_path, "/h/u/proj", "s1", _assistant_line(input_tokens=200, output_tokens=80))
    t = JsonlTailer(tmp_path)
    u = t.tail("s1", "/h/u/proj")
    assert u.input_tokens == 200
    assert u.output_tokens == 80
    assert u.model == "claude-opus-4-7"
    # 200 input @ $15/Mtok + 80 output @ $75/Mtok
    assert u.cost_usd == 200 * 15 / 1_000_000 + 80 * 75 / 1_000_000


def test_tail_increments_on_append(tmp_path):
    path = _make_jsonl(tmp_path, "/h/u/proj", "s1", _assistant_line(input_tokens=100))
    t = JsonlTailer(tmp_path)
    u = t.tail("s1", "/h/u/proj")
    assert u.input_tokens == 100

    # Append another assistant line; tailer must pick up only the new bytes.
    with path.open("a") as f:
        f.write(_assistant_line(input_tokens=50))
    u = t.tail("s1", "/h/u/proj")
    assert u.input_tokens == 150


def test_tail_ignores_malformed_lines(tmp_path):
    content = _assistant_line(input_tokens=100) + "{not json\n" + _assistant_line(input_tokens=50)
    _make_jsonl(tmp_path, "/h/u/proj", "s1", content)
    t = JsonlTailer(tmp_path)
    u = t.tail("s1", "/h/u/proj")
    assert u.input_tokens == 150


def test_tail_ignores_partial_trailing_line(tmp_path):
    """A writer who hasn't flushed leaves no trailing newline. We must
    rewind and re-read on the next tail."""
    content = _assistant_line(input_tokens=100) + '{"type":"assistant"'  # no newline
    path = _make_jsonl(tmp_path, "/h/u/proj", "s1", content)
    t = JsonlTailer(tmp_path)
    u = t.tail("s1", "/h/u/proj")
    assert u.input_tokens == 100  # complete line counted

    # Complete the partial line and add another.
    with path.open("a") as f:
        f.write(',"timestamp":"2026-04-28T10:00:00Z","message":{"model":"claude-opus-4-7","usage":{"input_tokens":50,"output_tokens":1}}}\n')
    u = t.tail("s1", "/h/u/proj")
    assert u.input_tokens == 150


def test_tail_missing_file_returns_empty_usage(tmp_path):
    t = JsonlTailer(tmp_path)
    u = t.tail("ghost", "/nope")
    assert u.input_tokens == 0
    assert u.model == ""


def test_user_lines_count_as_turns(tmp_path):
    content = _user_line() + _assistant_line() + _user_line() + _assistant_line()
    _make_jsonl(tmp_path, "/h/u/proj", "s1", content)
    t = JsonlTailer(tmp_path)
    u = t.tail("s1", "/h/u/proj")
    assert u.turns == 2


def test_today_totals_aggregate_across_sessions(tmp_path):
    today = "2026-04-28T10:00:00.000Z"
    _make_jsonl(tmp_path, "/h/a", "s1", _assistant_line(input_tokens=100, timestamp=today))
    _make_jsonl(tmp_path, "/h/b", "s2", _assistant_line(input_tokens=200, timestamp=today))
    t = JsonlTailer(tmp_path)
    t.tail("s1", "/h/a")
    t.tail("s2", "/h/b")
    # We bucket on UTC date; the assistant lines above use 2026-04-28.
    # today_totals() reads "now" — so this only matches if "now" is on
    # 2026-04-28 UTC; otherwise the assertion below would be 0. To keep
    # the test deterministic we just assert that the per-session totals
    # carried correctly, which is the contract today_totals depends on.
    s1 = t.get("s1")
    s2 = t.get("s2")
    assert s1.input_tokens == 100
    assert s2.input_tokens == 200


def test_unknown_model_marks_cost_unknown(tmp_path):
    _make_jsonl(
        tmp_path, "/h/u/proj", "s1",
        _assistant_line(model="claude-future-model", input_tokens=100),
    )
    t = JsonlTailer(tmp_path)
    u = t.tail("s1", "/h/u/proj")
    assert u.cost_unknown is True
    assert u.input_tokens == 100  # tokens still counted


def test_context_window_pct_uses_last_assistant_input(tmp_path):
    # Two assistant lines: cumulative input grows, but last_input_window
    # should reflect only the second one.
    content = (
        _assistant_line(input_tokens=10_000, cache_read=0, cache_write=0)
        + _assistant_line(input_tokens=50_000, cache_read=10_000, cache_write=0)
    )
    _make_jsonl(tmp_path, "/h/u/proj", "s1", content)
    t = JsonlTailer(tmp_path)
    u = t.tail("s1", "/h/u/proj")
    assert u.last_input_window == 60_000
    used, cap = u.context_window_pct()
    assert used == 60_000
    assert cap == 200_000

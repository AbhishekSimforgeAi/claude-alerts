"""Tests for the stdout dashboard."""
from __future__ import annotations

import io
import json
from pathlib import Path

from claude_alerts.dashboard import (
    Dashboard,
    _format_cost,
    _format_tokens,
    _short_cwd,
    _short_id,
    _short_model,
)
from claude_alerts.events import ClaudeEvent
from claude_alerts.sessions import SessionStore


def test_format_tokens_compresses():
    assert _format_tokens(0) == "0"
    assert _format_tokens(950) == "950"
    assert _format_tokens(1500) == "1.5k"
    assert _format_tokens(2_500_000) == "2.5M"


def test_format_cost_picks_scale():
    assert _format_cost(0.001) == "$0.001"
    assert _format_cost(0.42) == "$0.420"
    assert _format_cost(2.5) == "$2.50"
    assert _format_cost(1234) == "$1234"


def test_short_id_takes_uuid_prefix():
    assert _short_id("5756986d-da80-4494-af8d-35dccc263499") == "5756986d"


def test_short_model_drops_claude_prefix_and_datestamp():
    assert _short_model("claude-opus-4-7") == "opus-4-7"
    assert _short_model("claude-haiku-4-5-20251001") == "haiku-4-5"


def test_short_cwd_substitutes_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _short_cwd(str(tmp_path / "proj"), max_chars=40).startswith("~/")


def test_short_cwd_truncates_left():
    long = "/var/very/long/nested/path/with/many/components/project"
    out = _short_cwd(long, max_chars=20)
    assert len(out) == 20
    assert out.startswith("…")
    assert out.endswith("project")


def test_dashboard_is_disabled_when_stdout_is_not_a_tty():
    store = SessionStore()
    out = io.StringIO()
    d = Dashboard(store, out=out)
    assert d.enabled is False
    d.tick()  # should be a no-op
    assert out.getvalue() == ""


def test_dashboard_render_with_no_sessions(tmp_path):
    store = SessionStore()
    d = Dashboard(store, projects_root=tmp_path, force_render=True)
    text = d.render_string()
    assert "claude-alerts daemon" in text
    assert "0 active" in text


def test_dashboard_lists_active_session(tmp_path):
    store = SessionStore()
    store.apply_event(ClaudeEvent(
        event="UserPromptSubmit", session_id="abc12345-xxxx", cwd=str(tmp_path),
        claude_pid=1, timestamp=1.0,
    ))
    d = Dashboard(store, projects_root=tmp_path, force_render=True)
    text = d.render_string()
    assert "abc12345" in text
    assert "1 active" in text


def test_dashboard_includes_jsonl_token_counts(tmp_path):
    """When the JSONL exists for a session, its token counts should
    appear in the row."""
    cwd = "/x/y"
    sid = "deadbeef-aaaa-bbbb-cccc-dddddddddddd"
    project_dir = tmp_path / cwd.replace("/", "-")
    project_dir.mkdir(parents=True)
    (project_dir / f"{sid}.jsonl").write_text(json.dumps({
        "type": "assistant",
        "timestamp": "2026-04-28T10:00:00.000Z",
        "message": {
            "model": "claude-opus-4-7",
            "usage": {
                "input_tokens": 1234,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "output_tokens": 567,
            },
        },
    }) + "\n")

    store = SessionStore()
    d = Dashboard(store, projects_root=tmp_path, force_render=True)
    store.on_change(d.on_session_changed)
    store.apply_event(ClaudeEvent(
        event="UserPromptSubmit", session_id=sid, cwd=cwd,
        claude_pid=1, timestamp=1.0,
    ))
    text = d.render_string()
    assert "deadbeef" in text
    assert "1.2k" in text  # 1234 input tokens compressed
    assert "opus-4-7" in text


def test_dashboard_drops_columns_at_narrow_width(tmp_path):
    store = SessionStore()
    store.apply_event(ClaudeEvent(
        event="UserPromptSubmit", session_id="abc12345-x", cwd=str(tmp_path),
        claude_pid=1, timestamp=1.0,
    ))
    d = Dashboard(store, projects_root=tmp_path, force_render=True)
    wide = d._build_lines(width=130, with_ansi=False)
    narrow = d._build_lines(width=70, with_ansi=False)
    # At narrow width the row should be shorter (fewer columns rendered).
    wide_row = next(line for line in wide.splitlines() if "abc12345" in line)
    narrow_row = next(line for line in narrow.splitlines() if "abc12345" in line)
    assert len(wide_row) > len(narrow_row)


def test_dashboard_paint_is_no_op_when_disabled(tmp_path):
    store = SessionStore()
    out = io.StringIO()
    d = Dashboard(store, projects_root=tmp_path, out=out)
    # not a TTY
    assert not d.enabled
    d.mark_dirty()
    d.tick()
    assert out.getvalue() == ""

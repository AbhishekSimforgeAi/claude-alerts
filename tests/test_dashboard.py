"""Tests for the stdout dashboard."""
from __future__ import annotations

import io
import json
import time
from pathlib import Path

from claude_alerts.dashboard import (
    Dashboard,
    _bar,
    _format_age,
    _format_resets_in,
    _short_cwd,
    _short_id,
)
from claude_alerts.events import ClaudeEvent
from claude_alerts.sessions import SessionStore


def _write_sidecar(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "rate_limits.json"
    p.write_text(json.dumps(payload))
    return p


def test_short_id_takes_uuid_prefix():
    assert _short_id("5756986d-da80-4494-af8d-35dccc263499") == "5756986d"


def test_short_cwd_substitutes_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _short_cwd(str(tmp_path / "proj"), max_chars=40).startswith("~/")


def test_short_cwd_truncates_left():
    long = "/var/very/long/nested/path/with/many/components/project"
    out = _short_cwd(long, max_chars=20)
    assert len(out) == 20
    assert out.startswith("…")
    assert out.endswith("project")


def test_bar_renders_correct_fill():
    assert _bar(0, 10) == "░░░░░░░░░░"
    assert _bar(100, 10) == "██████████"
    assert _bar(50, 10) == "█████░░░░░"
    # Out-of-range values clamp.
    assert _bar(-5, 10) == "░░░░░░░░░░"
    assert _bar(150, 10) == "██████████"


def test_format_resets_in():
    now = 1_000_000
    assert _format_resets_in(now, now - 10) == "any moment"
    assert _format_resets_in(now, now + 30) == "30s"
    assert _format_resets_in(now, now + 600) == "10m"
    assert _format_resets_in(now, now + 7200) == "2h 0m"
    assert _format_resets_in(now, now + 90000) == "1d 1h"


def test_format_age():
    now = 1_000_000
    assert _format_age(now, now - 5) == "5s ago"
    assert _format_age(now, now - 90) == "1m ago"
    assert _format_age(now, now - 4000) == "1h ago"


def test_dashboard_disabled_when_stdout_is_not_a_tty():
    store = SessionStore()
    out = io.StringIO()
    d = Dashboard(store, out=out)
    assert d.enabled is False
    d.tick()
    assert out.getvalue() == ""


def test_dashboard_renders_message_when_no_sidecar(tmp_path):
    store = SessionStore()
    d = Dashboard(store, sidecar_path=tmp_path / "absent.json", force_render=True)
    text = d.render_string()
    assert "claude-alerts daemon" in text
    assert "no statusLine data yet" in text
    assert "no active sessions" in text


def test_dashboard_renders_limits_block(tmp_path):
    sidecar = _write_sidecar(tmp_path, {
        "saved_at": time.time(),
        "rate_limits": {
            "five_hour":        {"used_percentage": 42.5, "resets_at": int(time.time()) + 7200},
            "seven_day":        {"used_percentage": 18.3, "resets_at": int(time.time()) + 86400},
            "seven_day_opus":   {"used_percentage": 56.0, "resets_at": int(time.time()) + 86400},
        },
    })
    store = SessionStore()
    d = Dashboard(store, sidecar_path=sidecar, force_render=True)
    d.tick()  # populates the cache
    text = d.render_string()
    assert "5-hour" in text
    assert "weekly" in text
    assert "Opus" in text
    # 42.5% should appear with one decimal.
    assert "42.5%" in text


def test_dashboard_lists_active_session(tmp_path):
    store = SessionStore()
    store.apply_event(ClaudeEvent(
        event="UserPromptSubmit", session_id="abc12345-x", cwd=str(tmp_path),
        claude_pid=1, timestamp=1.0,
    ))
    d = Dashboard(store, sidecar_path=tmp_path / "absent.json", force_render=True)
    text = d.render_string()
    assert "abc12345" in text
    assert "1 session" in text
    assert "● working" in text


def test_dashboard_marks_data_stale_when_old(tmp_path):
    very_old = time.time() - 7200  # 2 hours ago, beyond 30-min stale threshold
    sidecar = _write_sidecar(tmp_path, {
        "saved_at": very_old,
        "rate_limits": {
            "five_hour": {"used_percentage": 50.0, "resets_at": int(time.time()) + 3600},
        },
    })
    store = SessionStore()
    d = Dashboard(store, sidecar_path=sidecar, force_render=True)
    d.tick()
    text = d.render_string()
    assert "stale" in text


def test_dashboard_skips_paint_when_disabled():
    store = SessionStore()
    out = io.StringIO()
    d = Dashboard(store, out=out)
    assert not d.enabled
    d.mark_dirty()
    d.tick()
    assert out.getvalue() == ""


def test_dashboard_session_changed_marks_dirty(tmp_path):
    store = SessionStore()
    d = Dashboard(store, sidecar_path=tmp_path / "absent.json", force_render=True)
    d._dirty = False
    d.on_session_changed("anything")
    assert d._dirty is True


def test_dashboard_strips_ansi_escapes_from_session_data(tmp_path):
    """A hostile cwd or session_id containing ANSI escape sequences must
    not flow through the dashboard onto the user's TTY — terminals could
    interpret them as cursor moves, title rewrites, or RCE primitives."""
    store = SessionStore()
    hostile_cwd = "/tmp/\x1b[31mhostile\x1b[0m"
    store.apply_event(ClaudeEvent(
        event="UserPromptSubmit",
        session_id="abc12\x1b[Hpwn", cwd=hostile_cwd,
        claude_pid=1, timestamp=1.0,
    ))
    d = Dashboard(store, sidecar_path=tmp_path / "absent.json", force_render=True)
    text = d.render_string()
    assert "\x1b" not in text
    assert "\x1b[" not in text


def test_dashboard_truncates_lines_to_width(tmp_path):
    """At narrow widths each line must be clipped, not wrapped — wrapping
    would corrupt the cursor-home repaint."""
    store = SessionStore()
    d = Dashboard(store, sidecar_path=tmp_path / "absent.json", force_render=True)
    text = d._build_lines(width=20)
    for line in text.splitlines():
        assert len(line) <= 20, f"line longer than 20: {line!r}"

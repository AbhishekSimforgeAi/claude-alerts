"""Tests for the stdout dashboard."""
from __future__ import annotations

import io
import json
import time
from pathlib import Path

from claude_alerts.contexts import ContextUsage
from claude_alerts.dashboard import (
    Dashboard,
    _bar,
    _format_age,
    _format_ctx,
    _format_resets_in,
    _short_cwd,
    _short_tokens,
)
from claude_alerts.events import ClaudeEvent
from claude_alerts.sessions import SessionStore


def _write_sidecar(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "rate_limits.json"
    p.write_text(json.dumps(payload))
    return p


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
    # Session-id hash is no longer rendered in the dashboard (#2).
    assert "abc12345" not in text
    assert "1 session" in text
    assert "● working" in text


def test_dashboard_session_header_omits_session_column(tmp_path):
    """Header is `STATUS     CTX               CWD` — no SESSION column (#2)."""
    store = SessionStore()
    store.apply_event(ClaudeEvent(
        event="UserPromptSubmit", session_id="abc12345-x", cwd=str(tmp_path),
        claude_pid=1, timestamp=1.0,
    ))
    d = Dashboard(store, sidecar_path=tmp_path / "absent.json", force_render=True)
    text = d.render_string()
    header_lines = [line for line in text.splitlines() if "STATUS" in line and "CTX" in line]
    assert len(header_lines) == 1
    header = header_lines[0]
    assert "SESSION" not in header
    assert header.lstrip() == "STATUS     CTX               CWD"


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


def test_short_tokens_under_thousand():
    from claude_alerts.dashboard import _short_tokens
    assert _short_tokens(0) == "0"
    assert _short_tokens(850) == "850"


def test_short_tokens_thousands():
    from claude_alerts.dashboard import _short_tokens
    assert _short_tokens(1000) == "1k"
    assert _short_tokens(90_000) == "90k"
    assert _short_tokens(199_500) == "200k"   # rounds to nearest 1000
    assert _short_tokens(200_000) == "200k"


def test_short_tokens_millions():
    from claude_alerts.dashboard import _short_tokens
    assert _short_tokens(1_000_000) == "1.0M"
    assert _short_tokens(1_200_000) == "1.2M"


def test_format_ctx_none_returns_dash():
    from claude_alerts.dashboard import _format_ctx
    assert _format_ctx(None).strip() == "—"


def test_format_ctx_partial_data_returns_dash():
    from claude_alerts.dashboard import _format_ctx
    cu = ContextUsage(saved_at=1.0, used_percentage=None,
                      used_tokens=100, total_tokens=200000)
    assert _format_ctx(cu).strip() == "—"
    cu = ContextUsage(saved_at=1.0, used_percentage=10.0,
                      used_tokens=None, total_tokens=200000)
    assert _format_ctx(cu).strip() == "—"
    cu = ContextUsage(saved_at=1.0, used_percentage=10.0,
                      used_tokens=100, total_tokens=None)
    assert _format_ctx(cu).strip() == "—"


def test_format_ctx_normal():
    from claude_alerts.dashboard import _format_ctx
    cu = ContextUsage(saved_at=1.0, used_percentage=45.2,
                      used_tokens=90_000, total_tokens=200_000)
    assert _format_ctx(cu).strip() == "45% (90k/200k)"


def test_format_ctx_under_one_percent():
    from claude_alerts.dashboard import _format_ctx
    cu = ContextUsage(saved_at=1.0, used_percentage=0.4,
                      used_tokens=800, total_tokens=200_000)
    assert _format_ctx(cu).strip() == "<1% (800/200k)"


def test_format_ctx_extended_context():
    from claude_alerts.dashboard import _format_ctx
    cu = ContextUsage(saved_at=1.0, used_percentage=55.5,
                      used_tokens=550_000, total_tokens=1_000_000)
    assert _format_ctx(cu).strip() == "56% (550k/1.0M)"


def test_format_ctx_padded_to_fixed_width():
    """The CTX field is left-justified, padded to 16 chars so CWD stays aligned."""
    from claude_alerts.dashboard import _format_ctx
    cu = ContextUsage(saved_at=1.0, used_percentage=45.2,
                      used_tokens=90_000, total_tokens=200_000)
    assert _format_ctx(cu) == "45% (90k/200k)  "  # 14 + 2 padding = 16
    assert len(_format_ctx(cu)) == 16
    assert len(_format_ctx(None)) == 16


def test_dashboard_renders_ctx_column_with_data(tmp_path):
    """When a session has a contexts sidecar, the CTX column shows the percent."""
    contexts_dir = tmp_path / "contexts"
    contexts_dir.mkdir()
    sid = "ctxtest1-aaaaaaaa"
    (contexts_dir / f"{sid}.json").write_text(json.dumps({
        "saved_at": 1.0,
        "session_id": sid,
        "context_window": {
            "context_window_size": 200000,
            "used_percentage": 45.2,
            "current_usage": {
                "input_tokens": 80_000,
                "output_tokens": 5_000,
                "cache_creation_input_tokens": 6_000,
                "cache_read_input_tokens": 4_000,
            },
        },
    }))

    store = SessionStore()
    store.apply_event(ClaudeEvent(
        event="UserPromptSubmit", session_id=sid, cwd=str(tmp_path),
        claude_pid=1, timestamp=1.0,
    ))

    d = Dashboard(
        store,
        sidecar_path=tmp_path / "absent.json",
        contexts_dir=contexts_dir,
        force_render=True,
    )
    text = d.render_string()
    assert "CTX" in text
    # 80_000 + 6_000 + 4_000 = 90_000 -> 90k. Percent rounds to 45.
    assert "45% (90k/200k)" in text


def test_dashboard_renders_em_dash_when_no_contexts_data(tmp_path):
    contexts_dir = tmp_path / "contexts"  # never created
    sid = "noctxd1-aaaaaaaa"
    store = SessionStore()
    store.apply_event(ClaudeEvent(
        event="UserPromptSubmit", session_id=sid, cwd=str(tmp_path),
        claude_pid=1, timestamp=1.0,
    ))
    d = Dashboard(
        store,
        sidecar_path=tmp_path / "absent.json",
        contexts_dir=contexts_dir,
        force_render=True,
    )
    text = d.render_string()
    # Em-dash appears in the rendered table (we don't assert exact alignment
    # here — the helper-level test already covers that).
    assert "—" in text


def test_dashboard_session_block_keeps_cwd_aligned(tmp_path):
    """Mixed rows (one with data, one without) — CWD column stays at the same
    horizontal position so the table stays readable."""
    contexts_dir = tmp_path / "contexts"
    contexts_dir.mkdir()
    sid_with = "withctx1-aaaaaaaa"
    (contexts_dir / f"{sid_with}.json").write_text(json.dumps({
        "saved_at": 1.0,
        "session_id": sid_with,
        "context_window": {
            "context_window_size": 200000,
            "used_percentage": 10.0,
            "current_usage": {
                "input_tokens": 18000, "output_tokens": 0,
                "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 1000,
            },
        },
    }))
    sid_without = "noctx1aa-bbbbbbbb"

    store = SessionStore()
    for sid in (sid_with, sid_without):
        store.apply_event(ClaudeEvent(
            event="UserPromptSubmit", session_id=sid, cwd="/work",
            claude_pid=1, timestamp=1.0,
        ))

    d = Dashboard(
        store,
        sidecar_path=tmp_path / "absent.json",
        contexts_dir=contexts_dir,
        force_render=True,
    )
    text = d.render_string()
    rows = [r for r in text.splitlines() if "/work" in r]
    assert len(rows) == 2
    # CWD lives at the same column index in both rows.
    cwd_cols = [r.index("/work") for r in rows]
    assert cwd_cols[0] == cwd_cols[1]

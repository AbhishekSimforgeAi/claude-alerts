"""Tests for daemon-level lifecycle of the per-session contexts sidecar."""
import json
from pathlib import Path

from claude_alerts import contexts
from claude_alerts.events import ClaudeEvent
from claude_alerts.sessions import SessionStore


def _write_ctx(base: Path, sid: str) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    p = base / f"{sid}.json"
    p.write_text(json.dumps({
        "saved_at": 1.0, "session_id": sid,
        "context_window": {
            "context_window_size": 200000, "used_percentage": 1.0,
            "current_usage": {
                "input_tokens": 1000, "output_tokens": 0,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            },
        },
    }))
    return p


def test_session_end_deletes_contexts_sidecar(tmp_path):
    """When SessionEnd fires, the per-session sidecar is removed.

    We don't spin up a real Daemon (X11). Instead we exercise the wiring:
    SessionStore.apply_event(SessionEnd) triggers an on_change callback
    that deletes the sidecar. Mirror what daemon.run() registers.
    """
    contexts_dir = tmp_path / "contexts"
    sid = "ending-sid"
    _write_ctx(contexts_dir, sid)
    store = SessionStore()

    # Pre-populate the session, then register the cleanup callback (matches
    # daemon wiring order — handler is added after restore, before events).
    store.apply_event(ClaudeEvent(
        event="UserPromptSubmit", session_id=sid, cwd="/x",
        claude_pid=1, timestamp=1.0,
    ))

    def cleanup(session_id: str) -> None:
        if store.get(session_id) is None:
            contexts.delete(session_id, contexts_dir)

    store.on_change(cleanup)

    store.apply_event(ClaudeEvent(
        event="SessionEnd", session_id=sid, cwd="/x",
        claude_pid=1, timestamp=2.0,
    ))

    assert not (contexts_dir / f"{sid}.json").exists()


def test_startup_sweep_removes_orphan_sidecars(tmp_path):
    """On startup, sidecars whose session_id isn't in the restored store are removed."""
    contexts_dir = tmp_path / "contexts"
    _write_ctx(contexts_dir, "alive")
    _write_ctx(contexts_dir, "orphan-1")
    _write_ctx(contexts_dir, "orphan-2")

    active = {"alive"}
    removed = contexts.sweep(active, contexts_dir)

    assert removed == 2
    assert (contexts_dir / "alive.json").exists()
    assert not (contexts_dir / "orphan-1.json").exists()
    assert not (contexts_dir / "orphan-2.json").exists()


def test_daemon_cleanup_handler_deletes_sidecar(tmp_path, monkeypatch):
    """Daemon._cleanup_contexts deletes the sidecar when the session is gone."""
    from claude_alerts.daemon import Daemon
    from claude_alerts.config import Config

    contexts_dir = tmp_path / "contexts"
    _write_ctx(contexts_dir, "gone-sid")

    # Construct a Daemon without running its main loop. We only need
    # __init__ side-effects, then we invoke the handler directly.
    monkeypatch.setattr("claude_alerts.daemon.X11Client", lambda: _DummyX11())
    monkeypatch.setattr(
        "claude_alerts.daemon.OverlayManager",
        lambda x11, store, config: _DummyOverlay(),
    )

    d = Daemon(
        events_dir=tmp_path / "events",
        config=Config(),
        persistence_path=None,
        dashboard_enabled=False,
        contexts_dir=contexts_dir,
    )

    # Session does not exist in the store -> handler deletes.
    d._cleanup_contexts("gone-sid")
    assert not (contexts_dir / "gone-sid.json").exists()


class _DummyX11:
    def fileno(self): return -1
    def subscribe_root_substructure(self): pass
    def get_visible_geometry(self, _wid): return None
    def pending_events(self): return False
    def next_event(self): return None


class _DummyOverlay:
    def sync_all(self): pass
    def on_window_configure(self, *a, **kw): pass

"""Verifies binding triggers only on UserPromptSubmit, never on SessionStart
or other events. This eliminates the focus race where the daemon would bind
the launcher terminal because the user had switched focus by the time the
SessionStart event was processed."""
from __future__ import annotations

import pytest

from claude_alerts.config import Config
from claude_alerts.events import ClaudeEvent


class _FakeBinder:
    """Records every try_bind call so tests can assert on the trigger logic."""
    def __init__(self, store, x11):
        self.store = store
        self.x11 = x11
        self.calls: list[str] = []

    def try_bind(self, session_id: str) -> None:
        self.calls.append(session_id)

    def unbind_window(self, window_id: int) -> None:
        pass

    def pending_manual_binds(self) -> list[str]:
        return []


class _NoopX11:
    """Just enough X11Client surface for Daemon.__init__ to succeed."""
    def __init__(self):
        self.screen = None
        self._NET_ACTIVE_WINDOW = 0
        self._NET_CLIENT_LIST = 0
        self._NET_WM_STATE = 0
        self._GTK_FRAME_EXTENTS = 0
        self._NET_WM_WINDOW_TYPE = 0
        self._NET_WM_WINDOW_TYPE_UTILITY = 0
        self._NET_WM_STATE_SKIP_TASKBAR = 0
        self._NET_WM_STATE_SKIP_PAGER = 0
        self._MOTIF_WM_HINTS = 0

    def fileno(self):
        return -1
    def flush(self):
        pass
    def subscribe_root_substructure(self):
        pass
    def get_active_window_id(self):
        return None
    def get_wm_class(self, wid):
        return ""
    def get_geometry(self, wid):
        return None
    def get_visible_geometry(self, wid):
        return None
    def get_frame_window_id(self, wid):
        return None
    def pending_events(self):
        return 0
    def next_event(self):
        return None


@pytest.fixture
def daemon_with_fake_binder(monkeypatch, tmp_path):
    from claude_alerts import daemon as daemon_mod
    monkeypatch.setattr(daemon_mod, "X11Client", _NoopX11)
    monkeypatch.setattr(daemon_mod, "Binder", _FakeBinder)
    d = daemon_mod.Daemon(events_dir=tmp_path / "events", config=Config())
    return d, d.binder


def _evt(event: str, session_id: str = "s1", t: float = 1.0) -> ClaudeEvent:
    return ClaudeEvent(
        event=event, session_id=session_id, cwd="/p", claude_pid=1, timestamp=t,
    )


def test_session_start_does_not_trigger_bind(daemon_with_fake_binder):
    daemon, binder = daemon_with_fake_binder
    daemon._on_event(_evt("SessionStart"))
    assert binder.calls == []
    assert daemon.store.get("s1") is not None


def test_user_prompt_submit_triggers_bind_for_unbound_session(daemon_with_fake_binder):
    daemon, binder = daemon_with_fake_binder
    daemon._on_event(_evt("SessionStart"))
    daemon._on_event(_evt("UserPromptSubmit", t=2.0))
    assert binder.calls == ["s1"]


def test_user_prompt_submit_skipped_when_already_bound(daemon_with_fake_binder):
    daemon, binder = daemon_with_fake_binder
    daemon._on_event(_evt("SessionStart"))
    daemon.store.set_bound_window("s1", 0x1234)
    daemon._on_event(_evt("UserPromptSubmit", t=2.0))
    assert binder.calls == []


def test_user_prompt_submit_creates_session_for_unknown_id(daemon_with_fake_binder):
    daemon, binder = daemon_with_fake_binder
    daemon._on_event(_evt("UserPromptSubmit"))
    assert daemon.store.get("s1") is not None
    assert binder.calls == ["s1"]


@pytest.mark.parametrize("event_name", ["PreToolUse", "PostToolUse", "Notification", "Stop"])
def test_non_prompt_events_never_trigger_bind(daemon_with_fake_binder, event_name):
    """These events fire while the user may be in any other window."""
    daemon, binder = daemon_with_fake_binder
    daemon._on_event(_evt("SessionStart"))
    daemon._on_event(_evt(event_name, t=2.0))
    assert binder.calls == []


def test_visibility_notify_does_not_call_raise_all(daemon_with_fake_binder):
    """The daemon used to call self.overlay.raise_all() on VisibilityNotify.
    With the always-on-top loop removed, feeding a VisibilityNotify must not
    crash and must not poke the overlay manager."""
    from Xlib import X
    daemon, _ = daemon_with_fake_binder

    poked: list[str] = []
    class _Tripwire:
        def __getattr__(self, name):
            def _f(*args, **kwargs):
                poked.append(name)
            return _f
    daemon.overlay = _Tripwire()

    class _FakeEvt:
        type = X.VisibilityNotify
    daemon._handle_x_event(_FakeEvt())

    assert poked == [], f"daemon poked overlay on VisibilityNotify: {poked}"

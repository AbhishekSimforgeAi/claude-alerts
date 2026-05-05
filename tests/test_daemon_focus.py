"""Verifies the daemon subscribes to _NET_ACTIVE_WINDOW PropertyNotify on
root, dispatches focus updates to the OverlayManager, and seeds the initial
focus at startup."""
from __future__ import annotations

from typing import Optional

import pytest
from Xlib import X

from claude_alerts.config import Config


_NET_ACTIVE_WINDOW_ATOM = 0xABCD


class _FocusFakeX11:
    """Just enough X11Client surface for Daemon.__init__ and _handle_x_event."""

    def __init__(self, active_window_id: Optional[int] = None) -> None:
        self.screen = None
        self._NET_ACTIVE_WINDOW = _NET_ACTIVE_WINDOW_ATOM
        self._NET_CLIENT_LIST = 0
        self._NET_WM_STATE = 0
        self._GTK_FRAME_EXTENTS = 0
        self._NET_WM_WINDOW_TYPE = 0
        self._NET_WM_WINDOW_TYPE_UTILITY = 0
        self._NET_WM_STATE_SKIP_TASKBAR = 0
        self._NET_WM_STATE_SKIP_PAGER = 0
        self._MOTIF_WM_HINTS = 0
        # Mimic python-xlib root: anything with a unique .id we can compare to.
        self.root = type("R", (), {"id": 0xDEAD})()
        self._active_window_id = active_window_id
        self.subscribe_substructure_called = False
        self.subscribe_property_called = False
        self.flushed = 0
        self.active_window_queries = 0

    def fileno(self):
        return -1

    def flush(self):
        self.flushed += 1

    def subscribe_root_substructure(self):
        self.subscribe_substructure_called = True

    def subscribe_root_property_changes(self):
        self.subscribe_property_called = True

    def get_active_window_id(self):
        self.active_window_queries += 1
        return self._active_window_id

    def get_wm_class(self, wid):
        return ""

    def get_geometry(self, wid):
        return None

    def get_visible_geometry(self, wid):
        return None

    def get_frame_window_id(self, wid):
        return None

    def list_top_level_windows(self):
        return []

    def pending_events(self):
        return 0

    def next_event(self):
        return None


@pytest.fixture
def daemon_with_focus_x11(monkeypatch, tmp_path):
    from claude_alerts import daemon as daemon_mod

    fake = _FocusFakeX11(active_window_id=0x1111)
    monkeypatch.setattr(daemon_mod, "X11Client", lambda: fake)
    d = daemon_mod.Daemon(events_dir=tmp_path / "events", config=Config())

    # Spy on the overlay manager's set_focused_window.
    received = []
    real = d.overlay.set_focused_window

    def _spy(wid):
        received.append(wid)
        return real(wid)
    d.overlay.set_focused_window = _spy
    return d, fake, received


def _make_property_notify(window, atom):
    """Synthesize a python-xlib-shaped PropertyNotify event."""
    class _Evt:
        type = X.PropertyNotify

    e = _Evt()
    e.window = window
    e.atom = atom
    return e


def test_property_notify_for_net_active_window_dispatches_focus(daemon_with_focus_x11):
    daemon, fake, received = daemon_with_focus_x11

    fake._active_window_id = 0xC0FFEE
    evt = _make_property_notify(fake.root, fake._NET_ACTIVE_WINDOW)
    daemon._handle_x_event(evt)

    assert received == [0xC0FFEE]


def test_property_notify_for_unrelated_atom_is_ignored(daemon_with_focus_x11):
    daemon, fake, received = daemon_with_focus_x11

    other_atom = fake._NET_ACTIVE_WINDOW + 99
    evt = _make_property_notify(fake.root, other_atom)
    daemon._handle_x_event(evt)

    assert received == []


def test_property_notify_for_non_root_window_is_ignored(daemon_with_focus_x11):
    daemon, fake, received = daemon_with_focus_x11

    other_window = type("R", (), {"id": 0xBAD})()
    evt = _make_property_notify(other_window, fake._NET_ACTIVE_WINDOW)
    daemon._handle_x_event(evt)

    assert received == []


def test_focus_dispatch_passes_none_when_active_window_missing(daemon_with_focus_x11):
    daemon, fake, received = daemon_with_focus_x11

    fake._active_window_id = None
    evt = _make_property_notify(fake.root, fake._NET_ACTIVE_WINDOW)
    daemon._handle_x_event(evt)

    assert received == [None]


def test_run_subscribes_to_property_changes_and_seeds_initial_focus(monkeypatch, tmp_path):
    """Daemon.run() must subscribe to property changes on root AND query the
    initial focus so the first paint after restart reflects reality."""
    from claude_alerts import daemon as daemon_mod

    fake = _FocusFakeX11(active_window_id=0xBEEF)
    monkeypatch.setattr(daemon_mod, "X11Client", lambda: fake)
    d = daemon_mod.Daemon(events_dir=tmp_path / "events", config=Config())

    received = []
    real = d.overlay.set_focused_window

    def _spy(wid):
        received.append(wid)
        return real(wid)
    d.overlay.set_focused_window = _spy

    # Stop the daemon immediately so run() does the startup work and exits.
    d._stop.set()
    d.run()

    assert fake.subscribe_property_called, "daemon must subscribe to root property changes"
    assert fake.active_window_queries >= 1, "daemon must query active window at startup"
    assert received == [0xBEEF]

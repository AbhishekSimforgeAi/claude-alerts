"""Verify the daemon does NOT call X11 from the ingester thread.

This is a regression test for a critical bug where the on_event callback
chain caused python-xlib calls on the wrong thread.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from claude_alerts.config import Config


@pytest.fixture
def tracking_daemon(monkeypatch, tmp_path):
    """A Daemon with a fake X11Client that records the thread of every method call."""
    from claude_alerts import daemon as daemon_mod

    main_thread_name = threading.current_thread().name
    call_threads = []

    class FakeX11:
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
            # A pipe whose read end never has data — keeps select() blocking.
            import os
            r, _w = os.pipe()
            return r
        def flush(self):
            call_threads.append(("flush", threading.current_thread().name))
        def get_active_window_id(self):
            call_threads.append(("get_active_window_id", threading.current_thread().name))
            return None
        def get_wm_class(self, wid):
            call_threads.append(("get_wm_class", threading.current_thread().name))
            return ""
        def get_geometry(self, wid):
            call_threads.append(("get_geometry", threading.current_thread().name))
            return None
        def list_top_level_windows(self):
            return []
        def subscribe_root_substructure(self):
            call_threads.append(("subscribe_root_substructure", threading.current_thread().name))
        def subscribe_root_property_changes(self):
            call_threads.append(("subscribe_root_property_changes", threading.current_thread().name))
        def pending_events(self):
            return 0
        def next_event(self):
            return None

    monkeypatch.setattr(daemon_mod, "X11Client", FakeX11)
    events_dir = tmp_path / "events"
    cfg = Config()
    daemon = daemon_mod.Daemon(events_dir=events_dir, config=cfg)
    return daemon, call_threads, main_thread_name


def test_x11_calls_only_happen_on_main_thread(tracking_daemon):
    """The on_event callback chain must NOT call X11 methods from the ingester thread."""
    daemon, call_threads, main_thread_name = tracking_daemon

    # Start the daemon in a "main" thread (the test thread acts as main)
    t = threading.Thread(target=daemon.run, name="DaemonMain", daemon=True)
    t.start()

    # Wait for the daemon to be running
    time.sleep(0.2)

    # Drop a synthetic event file. This would historically cause the ingester
    # thread to call X11 methods directly. With the queue fix, X11 calls only
    # happen on the main thread.
    events_dir = daemon.events_dir
    events_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "event": "SessionStart", "session_id": "s1",
        "cwd": "/p", "claude_pid": 1, "timestamp": 1.0,
    }
    tmp = events_dir / "evt.json.tmp"
    final = events_dir / "evt.json"
    tmp.write_text(json.dumps(payload))
    tmp.rename(final)

    # Wait for the event to be processed
    time.sleep(0.5)
    daemon.stop()
    t.join(timeout=2)

    # Look for any X11 call that was NOT made on DaemonMain. The ingester thread
    # is named "Thread-N" by default. If any X11 call was made off-main, fail.
    bad_calls = [(method, tn) for (method, tn) in call_threads if tn != "DaemonMain"]
    assert not bad_calls, f"X11 calls happened on the wrong thread: {bad_calls}"

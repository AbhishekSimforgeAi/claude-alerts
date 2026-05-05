"""End-to-end smoke test using Xvfb. Slow (~1-2s) but exercises the full stack."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from claude_alerts.config import Config
from claude_alerts.daemon import Daemon


XVFB_DISPLAY = ":99"


@pytest.fixture
def xvfb():
    if shutil.which("Xvfb") is None:
        pytest.skip("Xvfb not installed")
    proc = subprocess.Popen(
        ["Xvfb", XVFB_DISPLAY, "-screen", "0", "1280x720x24"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(0.3)
    old = os.environ.get("DISPLAY")
    os.environ["DISPLAY"] = XVFB_DISPLAY
    try:
        yield
    finally:
        if old is None:
            del os.environ["DISPLAY"]
        else:
            os.environ["DISPLAY"] = old
        proc.terminate()
        proc.wait(timeout=2)


def _spawn_fake_terminal_window():
    """Open a Python-side X11 window with WM_CLASS=gnome-terminal-server."""
    from Xlib import X, Xatom, display

    d = display.Display()
    s = d.screen()
    win = s.root.create_window(
        50, 50, 400, 300, 1,
        s.root_depth,
        X.InputOutput,
        X.CopyFromParent,
        background_pixel=s.white_pixel,
        event_mask=X.ExposureMask,
    )
    win.set_wm_class("gnome-terminal-server", "Gnome-terminal")
    win.set_wm_name("fake terminal")
    win.map()

    # Make ourselves the active window. The property type for _NET_ACTIVE_WINDOW is WINDOW.
    NET_ACTIVE_WINDOW = d.intern_atom("_NET_ACTIVE_WINDOW")
    s.root.change_property(NET_ACTIVE_WINDOW, Xatom.WINDOW, 32, [win.id])
    d.flush()
    return d, win


def _drop_event(events_dir: Path, payload: dict, name: str) -> None:
    tmp = events_dir / f"{name}.json.tmp"
    final = events_dir / f"{name}.json"
    tmp.write_text(json.dumps(payload))
    tmp.rename(final)


def _wait(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _set_active_window(disp, root, win_id):
    from Xlib import Xatom
    NET_ACTIVE_WINDOW = disp.intern_atom("_NET_ACTIVE_WINDOW")
    root.change_property(NET_ACTIVE_WINDOW, Xatom.WINDOW, 32, [win_id])
    disp.flush()


def test_focus_modulation_recolours_overlay_via_property_notify(xvfb, tmp_path):
    """End-to-end focus-modulation tracer bullet:
    bind a session → focus the bound terminal → daemon paints emissive →
    move focus to a different window → daemon paints dim.
    """
    from Xlib import X, display

    fake_disp, fake_win = _spawn_fake_terminal_window()

    # Spawn a second, non-Claude window so we have somewhere to move focus.
    other_disp = display.Display()
    other_screen = other_disp.screen()
    other_win = other_screen.root.create_window(
        500, 500, 200, 150, 1,
        other_screen.root_depth,
        X.InputOutput,
        X.CopyFromParent,
        background_pixel=other_screen.white_pixel,
        event_mask=X.ExposureMask,
    )
    other_win.set_wm_class("not-a-terminal", "Other")
    other_win.set_wm_name("other window")
    other_win.map()
    other_disp.flush()

    events_dir = tmp_path / "events"
    cfg = Config()
    daemon = Daemon(events_dir=events_dir, config=cfg)
    t = threading.Thread(target=daemon.run, daemon=True)
    t.start()
    try:
        # SessionStart + UserPromptSubmit so the binder grabs the fake terminal.
        _drop_event(
            events_dir,
            {
                "event": "SessionStart", "session_id": "s1",
                "cwd": "/proj", "claude_pid": 1, "timestamp": 1.0,
            },
            "01-start",
        )
        _drop_event(
            events_dir,
            {
                "event": "UserPromptSubmit", "session_id": "s1",
                "cwd": "/proj", "claude_pid": 1, "timestamp": 2.0,
            },
            "02-prompt",
        )
        assert _wait(lambda: daemon.overlay.has_overlay("s1"))

        from claude_alerts.colors import dim_hex
        from claude_alerts.overlay import _rgb_to_pixel, hex_to_rgb
        green = _rgb_to_pixel(hex_to_rgb(cfg.color_working))
        green_dim = _rgb_to_pixel(hex_to_rgb(dim_hex(cfg.color_working, 0.25)))

        # Focus on the bound terminal — overlay must paint emissive green.
        _set_active_window(fake_disp, fake_disp.screen().root, fake_win.id)
        assert _wait(lambda: daemon.overlay._overlays["s1"].color_pixel == green)

        # Focus moves to the other (non-Claude) window — overlay must dim.
        _set_active_window(fake_disp, fake_disp.screen().root, other_win.id)
        assert _wait(lambda: daemon.overlay._overlays["s1"].color_pixel == green_dim)
    finally:
        daemon.stop()
        t.join(timeout=2)
        try:
            fake_win.destroy()
            fake_disp.flush()
            fake_disp.close()
        except Exception:
            pass
        try:
            other_win.destroy()
            other_disp.flush()
            other_disp.close()
        except Exception:
            pass


def test_daemon_binds_overlay_and_changes_color(xvfb, tmp_path):
    fake_disp, fake_win = _spawn_fake_terminal_window()

    events_dir = tmp_path / "events"
    cfg = Config()
    daemon = Daemon(events_dir=events_dir, config=cfg)
    t = threading.Thread(target=daemon.run, daemon=True)
    t.start()
    try:
        # SessionStart -> binder grabs the active window (the fake terminal).
        _drop_event(
            events_dir,
            {
                "event": "SessionStart", "session_id": "s1",
                "cwd": "/proj", "claude_pid": 1, "timestamp": 1.0,
            },
            "01-start",
        )
        assert _wait(lambda: daemon.store.get("s1") is not None and daemon.store.get("s1").bound_window_id == fake_win.id)
        assert _wait(lambda: daemon.overlay.has_overlay("s1"))

        # Now flip to working
        _drop_event(
            events_dir,
            {
                "event": "PreToolUse", "session_id": "s1",
                "cwd": "/proj", "claude_pid": 1, "timestamp": 2.0,
            },
            "02-tool",
        )
        from claude_alerts.sessions import Status
        assert _wait(lambda: daemon.store.get("s1").status == Status.WORKING)

        # Then back to waiting
        _drop_event(
            events_dir,
            {
                "event": "Stop", "session_id": "s1",
                "cwd": "/proj", "claude_pid": 1, "timestamp": 3.0,
            },
            "03-stop",
        )
        assert _wait(lambda: daemon.store.get("s1").status == Status.WAITING)

        # Destroy fake terminal -> overlay should disappear
        fake_win.destroy()
        fake_disp.flush()
        assert _wait(lambda: not daemon.overlay.has_overlay("s1"))
    finally:
        daemon.stop()
        t.join(timeout=2)
        try:
            fake_disp.close()
        except Exception:
            pass

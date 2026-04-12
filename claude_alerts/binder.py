"""Binds Claude sessions to X11 terminal windows."""
from __future__ import annotations

import logging
from typing import Optional

from claude_alerts.sessions import SessionStore

log = logging.getLogger(__name__)

# Substring allowlist of known terminal WM_CLASS values.
TERMINAL_WM_CLASSES = (
    "gnome-terminal-server",
    "kitty",
    "alacritty",
    "wezterm",
    "xterm",
    "urxvt",
    "konsole",
    "tilix",
    "terminator",
)


def looks_like_terminal(wm_class: str) -> bool:
    if not wm_class:
        return False
    lc = wm_class.lower()
    return any(name in lc for name in TERMINAL_WM_CLASSES)


class Binder:
    def __init__(self, store: SessionStore, x11) -> None:
        self.store = store
        self.x11 = x11
        self._pending: list[str] = []

    def try_bind(self, session_id: str) -> None:
        """Try to bind the named session to the currently active window."""
        session = self.store.get(session_id)
        if session is None:
            return

        try:
            wid = self.x11.get_active_window_id()
        except Exception as e:
            log.warning("session %s: x11 query for active window failed: %s; queueing manual bind", session_id, e)
            self._enqueue(session_id)
            return

        if wid is None:
            self._enqueue(session_id)
            return

        try:
            wm_class = self.x11.get_wm_class(wid)
        except Exception as e:
            log.warning("session %s: x11 query for wm_class failed: %s; queueing manual bind", session_id, e)
            self._enqueue(session_id)
            return

        if not looks_like_terminal(wm_class):
            log.info(
                "session %s: active window %#x has WM_CLASS %r, queueing manual bind",
                session_id, wid, wm_class,
            )
            self._enqueue(session_id)
            return

        # Resolve client window to its frame (top-level child of root) so that
        # ConfigureNotify and DestroyNotify from root substructure events match.
        try:
            frame_wid = self.x11.get_frame_window_id(wid)
        except Exception as e:
            log.warning("session %s: failed to resolve frame window for %#x: %s", session_id, wid, e)
            self._enqueue(session_id)
            return

        if frame_wid is None:
            log.warning("session %s: could not find frame window for client %#x", session_id, wid)
            self._enqueue(session_id)
            return

        self.store.set_bound_window(session_id, frame_wid, client_window_id=wid)
        log.info("session %s bound to client %#x (frame %#x)", session_id, wid, frame_wid)

    def complete_manual_bind(self, session_id: str, window_id: int) -> None:
        if self.store.get(session_id) is None:
            return
        frame_wid = self.x11.get_frame_window_id(window_id)
        if frame_wid is None:
            # The clicked window may already be a top-level frame; fall back to it.
            frame_wid = window_id
        # Treat the input as the client; if it was already a frame, client == frame.
        self.store.set_bound_window(session_id, frame_wid, client_window_id=window_id)
        if session_id in self._pending:
            self._pending.remove(session_id)
        log.info("session %s manually bound to frame %#x", session_id, frame_wid)

    def pending_manual_binds(self) -> list[str]:
        return list(self._pending)

    def unbind_window(self, window_id: int) -> None:
        """Called when a bound window has been destroyed."""
        for s in self.store.all():
            if s.bound_window_id == window_id:
                log.info("session %s: window %#x destroyed, unbound", s.session_id, window_id)
                self.store.set_bound_window(s.session_id, None)

    def _enqueue(self, session_id: str) -> None:
        if session_id not in self._pending:
            self._pending.append(session_id)

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

        wid = self.x11.get_active_window_id()
        if wid is None:
            self._enqueue(session_id)
            return

        wm_class = self.x11.get_wm_class(wid)
        if not looks_like_terminal(wm_class):
            log.info(
                "session %s: active window %#x has WM_CLASS %r, queueing manual bind",
                session_id, wid, wm_class,
            )
            self._enqueue(session_id)
            return

        self.store.set_bound_window(session_id, wid)
        log.info("session %s bound to window %#x", session_id, wid)

    def complete_manual_bind(self, session_id: str, window_id: int) -> None:
        if self.store.get(session_id) is None:
            return
        self.store.set_bound_window(session_id, window_id)
        if session_id in self._pending:
            self._pending.remove(session_id)

    def pending_manual_binds(self) -> list[str]:
        return list(self._pending)

    def unbind_window(self, window_id: int) -> None:
        """Called when a bound window has been destroyed."""
        for s in self.store.all():
            if s.bound_window_id == window_id:
                self.store.set_bound_window(s.session_id, None)

    def _enqueue(self, session_id: str) -> None:
        if session_id not in self._pending:
            self._pending.append(session_id)

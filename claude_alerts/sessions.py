"""In-memory session store and state machine."""
from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Callable, Optional

from claude_alerts.events import ClaudeEvent

log = logging.getLogger(__name__)


class Status(enum.Enum):
    WORKING = "working"
    WAITING = "waiting"


# Maps each event to the status it should leave the session in.
_EVENT_TO_STATUS = {
    "SessionStart": Status.WAITING,
    "UserPromptSubmit": Status.WORKING,
    "PreToolUse": Status.WORKING,
    "PostToolUse": Status.WORKING,
    "Stop": Status.WAITING,
    "Notification": Status.WAITING,
    "PermissionRequest": Status.WAITING,
    "Elicitation": Status.WAITING,
}

# Events whose arrival means the user must take action before Claude can
# continue — the overlay paints red even when a background task is alive.
# Notification covers tool-permission and idle prompts; PermissionRequest
# covers sandbox prompts (e.g. "Network request outside of sandbox");
# Elicitation covers MCP servers asking for user input mid-tool-call.
USER_ACTION_EVENTS = frozenset({
    "Notification",
    "PermissionRequest",
    "Elicitation",
})

# Tools whose successful invocation means Claude has armed an autonomous
# wake-up: when this turn ends, something else will resume Claude without
# the user typing. The overlay uses this to keep the border green during
# the pause between Stop and the wake-up firing.
BACKGROUND_TASK_TOOLS = frozenset({
    "Monitor",
    "CronCreate",
    "RemoteTrigger",
    "ScheduleWakeup",
})


@dataclass
class Session:
    session_id: str
    cwd: str
    claude_pid: int
    status: Status
    last_event_at: float
    bound_window_id: Optional[int] = None
    client_window_id: Optional[int] = None
    last_event: str = ""
    background_active: bool = False


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._listeners: list[Callable[[str], None]] = []

    def on_change(self, callback: Callable[[str], None]) -> None:
        """Subscribe to status / lifecycle changes. Callback receives session_id.

        Fired on: session creation, status transitions, bound-window changes,
        session removal (SessionEnd or eviction). NOT fired on no-op same-status
        events or timestamp-only refreshes.

        Subscribers must not block. Exceptions raised by a subscriber are caught
        and logged; subsequent subscribers still run.
        """
        self._listeners.append(callback)

    def get(self, session_id: str) -> Optional[Session]:
        return self._sessions.get(session_id)

    def all(self) -> list[Session]:
        return list(self._sessions.values())

    def restore(self, session: Session) -> None:
        """Insert a fully-formed Session without going through apply_event.

        Used at daemon startup to reconstruct state from the persistence file.
        Bypasses the state machine and the background_active lifecycle — the
        persisted record is treated as the authoritative post-event state.

        Fires on_change listeners so the overlay manager can paint the border;
        the persister is wired AFTER restore so the load doesn't immediately
        rewrite the file with what we just read.
        """
        self._sessions[session.session_id] = session
        self._notify(session.session_id)

    def apply_event(self, evt: ClaudeEvent) -> None:
        if evt.event == "SessionEnd":
            if evt.session_id in self._sessions:
                del self._sessions[evt.session_id]
                self._notify(evt.session_id)
            return

        new_status = _EVENT_TO_STATUS.get(evt.event)
        if new_status is None:
            return

        session = self._sessions.get(evt.session_id)
        changed = False
        if session is None:
            session = Session(
                session_id=evt.session_id,
                cwd=evt.cwd,
                claude_pid=evt.claude_pid,
                status=new_status,
                last_event_at=evt.timestamp,
                last_event=evt.event,
            )
            self._sessions[evt.session_id] = session
            changed = True
        else:
            if session.status != new_status:
                session.status = new_status
                changed = True
            session.last_event_at = evt.timestamp
            session.last_event = evt.event

        # Background-active lifecycle. Set on successful PostToolUse for
        # any wake-up creator; cleared on UserPromptSubmit (user took over).
        # Stop / Notification / PermissionRequest leave it alone — the
        # overlay layer applies the user-action-overrides-green rule itself.
        if evt.event == "PostToolUse" and evt.tool_name in BACKGROUND_TASK_TOOLS:
            if not session.background_active:
                session.background_active = True
                changed = True
        elif evt.event == "UserPromptSubmit":
            if session.background_active:
                session.background_active = False
                changed = True

        if changed:
            self._notify(evt.session_id)

    def evict_idle(self, now: float, max_age_s: float) -> list[str]:
        """Remove unbound sessions whose last event is older than max_age_s.

        Bound sessions are NEVER evicted on idle: their overlay is tied to
        a live X11 window, and that window's destruction is signalled by
        DestroyNotify (handled by the binder), not by hook silence. A long
        pause between turns must not destroy the border.

        Returns evicted ids.
        """
        evicted = [
            sid for sid, s in self._sessions.items()
            if s.bound_window_id is None
            and now - s.last_event_at > max_age_s
        ]
        for sid in evicted:
            del self._sessions[sid]
            self._notify(sid)
        return evicted

    def set_bound_window(
        self,
        session_id: str,
        window_id: Optional[int],
        client_window_id: Optional[int] = None,
    ) -> None:
        """Update a session's bound (frame) window id and optional client window id.

        bound_window_id is what the daemon matches against root substructure events
        (ConfigureNotify, DestroyNotify) — the top-level frame on reparenting WMs.
        client_window_id is the inner content window the overlay sizes itself to,
        so the border hugs the actual terminal content rather than the WM decoration
        / invisible resize borders. Notifies once if either field changed.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return
        # When unbinding (window_id=None), also clear the client id even if the caller
        # didn't pass it — they're conceptually a pair.
        new_client = None if window_id is None else client_window_id
        if (
            session.bound_window_id == window_id
            and session.client_window_id == new_client
        ):
            return
        session.bound_window_id = window_id
        session.client_window_id = new_client
        self._notify(session_id)

    def _notify(self, session_id: str) -> None:
        for cb in self._listeners:
            try:
                cb(session_id)
            except Exception:
                log.exception("session_id=%s: subscriber raised", session_id)

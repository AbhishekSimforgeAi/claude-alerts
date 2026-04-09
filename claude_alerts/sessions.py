"""In-memory session store and state machine."""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Callable, Optional

from claude_alerts.events import ClaudeEvent


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
}


@dataclass
class Session:
    session_id: str
    cwd: str
    claude_pid: int
    status: Status
    last_event_at: float
    bound_window_id: Optional[int] = None


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._listeners: list[Callable[[str], None]] = []

    def on_change(self, callback: Callable[[str], None]) -> None:
        """Subscribe to status / lifecycle changes. Callback receives session_id."""
        self._listeners.append(callback)

    def get(self, session_id: str) -> Optional[Session]:
        return self._sessions.get(session_id)

    def all(self) -> list[Session]:
        return list(self._sessions.values())

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
            )
            self._sessions[evt.session_id] = session
            changed = True
        else:
            if session.status != new_status:
                session.status = new_status
                changed = True
            session.last_event_at = evt.timestamp

        if changed:
            self._notify(evt.session_id)

    def evict_idle(self, now: float, max_age_s: float) -> list[str]:
        """Remove sessions whose last event is older than max_age_s. Returns evicted ids."""
        evicted = [
            sid for sid, s in self._sessions.items()
            if now - s.last_event_at > max_age_s
        ]
        for sid in evicted:
            del self._sessions[sid]
            self._notify(sid)
        return evicted

    def _notify(self, session_id: str) -> None:
        for cb in self._listeners:
            cb(session_id)

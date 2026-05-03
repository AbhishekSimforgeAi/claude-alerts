"""Persist SessionStore bindings across daemon restarts.

Writes a JSON snapshot of bound sessions to a file on every save() call,
throttled so a burst of mutations collapses into one write. Atomic via
tmp + os.replace so a reader never sees a half-written file.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

from claude_alerts.sessions import Session, Status

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def _session_to_dict(s: Session) -> dict:
    return {
        "session_id": s.session_id,
        "cwd": s.cwd,
        "claude_pid": s.claude_pid,
        "status": s.status.value,
        "last_event_at": s.last_event_at,
        "last_event": s.last_event,
        "background_active": s.background_active,
        "bound_window_id": s.bound_window_id,
        "client_window_id": s.client_window_id,
        "first_seen_at": s.first_seen_at,
    }


def _dict_to_session(d: dict) -> Optional[Session]:
    """Reconstruct a Session from a persisted dict. Returns None on shape errors."""
    try:
        last_event_at = float(d["last_event_at"])
        # Pre-#3 bindings files don't carry first_seen_at — fall back to
        # last_event_at so existing sessions still get a plausible
        # appearance timestamp for the dashboard sort.
        first_seen_at = float(d["first_seen_at"]) if "first_seen_at" in d else last_event_at
        return Session(
            session_id=str(d["session_id"]),
            cwd=str(d["cwd"]),
            claude_pid=int(d["claude_pid"]),
            status=Status(d["status"]),
            last_event_at=last_event_at,
            bound_window_id=int(d["bound_window_id"]) if d.get("bound_window_id") is not None else None,
            client_window_id=int(d["client_window_id"]) if d.get("client_window_id") is not None else None,
            last_event=str(d.get("last_event", "")),
            background_active=bool(d.get("background_active", False)),
            first_seen_at=first_seen_at,
        )
    except (KeyError, TypeError, ValueError) as e:
        log.warning("persisted session entry rejected: %s (%r)", e, d)
        return None


class BindingPersister:
    """Throttled atomic writer for SessionStore bindings.

    save() takes a snapshot of bound sessions and schedules a deferred
    write at most one per throttle_s. A save() that arrives while a
    write window is open updates the pending snapshot in place — only
    the latest snapshot is written. Failures log WARNING and are
    otherwise swallowed.
    """

    def __init__(self, path: Path, throttle_s: float = 0.2) -> None:
        self.path = path
        self.throttle_s = throttle_s
        self._lock = threading.Lock()
        self._pending: Optional[list[dict]] = None
        self._timer: Optional[threading.Timer] = None
        self._last_write = 0.0

    def save(self, sessions: list[Session]) -> None:
        """Mark a new snapshot pending. Schedules a write if not already pending."""
        snapshot = [_session_to_dict(s) for s in sessions if s.bound_window_id is not None]
        with self._lock:
            self._pending = snapshot
            if self._timer is not None:
                return
            now = time.monotonic()
            delay = max(0.0, self._last_write + self.throttle_s - now)
            self._timer = threading.Timer(delay, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def stop(self) -> None:
        """Cancel any pending timer and synchronously flush whatever is pending.

        Called from daemon shutdown so a final state change isn't lost.
        """
        with self._lock:
            t = self._timer
            self._timer = None
        if t is not None:
            t.cancel()
        self._flush()

    def load(self) -> list[Session]:
        """Read and parse the snapshot file. Returns [] on missing/malformed/wrong-version."""
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return []
        except OSError as e:
            log.warning("cannot read %s: %s", self.path, e)
            return []
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            log.warning("malformed %s (%s); starting with empty bindings", self.path, e)
            return []
        if not isinstance(data, dict):
            log.warning("malformed %s (not an object); starting with empty bindings", self.path)
            return []
        version = data.get("version")
        if version != SCHEMA_VERSION:
            log.warning(
                "unsupported persistence version %r in %s (expected %d); starting empty",
                version, self.path, SCHEMA_VERSION,
            )
            return []
        raw_sessions = data.get("sessions")
        if not isinstance(raw_sessions, list):
            return []
        out: list[Session] = []
        for entry in raw_sessions:
            if not isinstance(entry, dict):
                continue
            s = _dict_to_session(entry)
            if s is not None and s.bound_window_id is not None:
                out.append(s)
        return out

    def _flush(self) -> None:
        with self._lock:
            snapshot = self._pending
            self._pending = None
            self._timer = None
            if snapshot is None:
                return
            try:
                self._write_atomic(snapshot)
            except Exception:
                log.warning("failed to write %s", self.path, exc_info=True)
            self._last_write = time.monotonic()

    def _write_atomic(self, sessions: list[dict]) -> None:
        """Write sessions.json atomically with fsync for power-loss durability.

        File is created mode 0600 — the snapshot contains pids, cwd paths,
        and session ids that fingerprint the user's workflow. tmp+rename
        is atomic w.r.t. concurrent readers, fsync makes it durable across
        crashes and power loss.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        data = {
            "version": SCHEMA_VERSION,
            "saved_at": time.time(),
            "sessions": sessions,
        }
        body = json.dumps(data, indent=2) + "\n"
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        # Open with explicit mode so umask doesn't widen permissions.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, body.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.path)
        # fsync the parent dir so the rename is durable too.
        try:
            dir_fd = os.open(self.path.parent, os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # Some filesystems (e.g. tmpfs) don't support directory fsync.
            pass

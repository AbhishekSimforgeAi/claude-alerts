"""Daemon entry point — wires ingester, sessions, binder, overlay on one event loop."""
from __future__ import annotations

import logging
import os
import queue
import select
import threading
import time
from pathlib import Path

from Xlib import X

from claude_alerts import contexts
from claude_alerts.binder import Binder
from claude_alerts.config import Config
from claude_alerts.dashboard import Dashboard
from claude_alerts.events import ClaudeEvent
from claude_alerts.ingester import EventIngester
from claude_alerts.overlay import OverlayManager
from claude_alerts.persistence import BindingPersister
from claude_alerts.sessions import SessionStore
from claude_alerts.x11 import Geometry, X11Client

log = logging.getLogger(__name__)

IDLE_SWEEP_INTERVAL_S = 30.0
IDLE_MAX_AGE_S = 300.0


class Daemon:
    def __init__(
        self,
        events_dir: Path,
        config: Config,
        persistence_path: Path | None = None,
        dashboard_enabled: bool = True,
        contexts_dir: Path | None = None,
    ) -> None:
        self.events_dir = events_dir
        self.config = config
        self.contexts_dir = contexts_dir or contexts.default_contexts_dir()
        self.store = SessionStore()
        self.x11 = X11Client()
        self.binder = Binder(self.store, self.x11)
        self.overlay = OverlayManager(self.x11, self.store, self.config)
        self.persister = (
            BindingPersister(persistence_path) if persistence_path is not None else None
        )
        self.dashboard = (
            Dashboard(self.store, contexts_dir=self.contexts_dir)
            if dashboard_enabled else None
        )
        if self.dashboard is not None and self.dashboard.enabled:
            self.store.on_change(self.dashboard.on_session_changed)
        # Bound the binder's manual-bind queue: drop ids whose session was
        # removed (SessionEnd or idle eviction). Without this, a long-running
        # daemon with many short-lived non-terminal sessions would grow the
        # queue unboundedly.
        self.store.on_change(self._reap_binder_queue)
        # Delete the per-session contexts sidecar when the session is removed
        # (SessionEnd or idle eviction), so contexts/ stays bounded.
        self.store.on_change(self._cleanup_contexts)
        # Marshal ingester events to the main thread via a thread-safe queue.
        # python-xlib is NOT thread-safe — all X11 calls must run on the main thread.
        self._event_queue: "queue.SimpleQueue[ClaudeEvent]" = queue.SimpleQueue()
        self.ingester = EventIngester(self.events_dir, on_event=self._event_queue.put)
        self._stop = threading.Event()
        self._ingester_thread: threading.Thread | None = None
        self._last_sweep = time.monotonic()

    def _reap_binder_queue(self, session_id: str) -> None:
        if self.store.get(session_id) is None:
            self.binder.forget_session(session_id)

    def _cleanup_contexts(self, session_id: str) -> None:
        """Delete the per-session contexts sidecar when a session goes away."""
        if self.store.get(session_id) is None:
            contexts.delete(session_id, self.contexts_dir)

    def _on_event(self, evt: ClaudeEvent) -> None:
        """Runs on the main thread only — drained from the queue in run().

        Binding only fires on UserPromptSubmit, and only if the session is
        not yet bound. UserPromptSubmit is the one event that proves the
        user is currently focused on the claude terminal — there is no way
        to submit a prompt to claude from another window.
        """
        self.store.apply_event(evt)
        if evt.event == "UserPromptSubmit":
            session = self.store.get(evt.session_id)
            if session is not None and session.bound_window_id is None:
                self.binder.try_bind(evt.session_id)

    def run(self) -> None:
        self.x11.subscribe_root_substructure()

        # Restore persisted bindings before subscribing to events, so the
        # first ingester event sees a fully reconstructed store. Drop entries
        # whose bound window no longer exists — the X server may have been
        # restarted, the terminal may have been closed while we were down.
        if self.persister is not None:
            for s in self.persister.load():
                target = s.client_window_id or s.bound_window_id
                if target is None:
                    continue
                try:
                    geo = self.x11.get_visible_geometry(target)
                except Exception as e:
                    log.debug(
                        "session %s: skipping restore (geometry query failed: %s)",
                        s.session_id, e,
                    )
                    continue
                if geo is None:
                    log.info(
                        "session %s: skipping restore (window %#x is gone)",
                        s.session_id, target,
                    )
                    continue
                self.store.restore(s)
            # Subscribe AFTER restore so we don't immediately rewrite the file
            # we just loaded.
            self.store.on_change(lambda _sid: self.persister.save(self.store.all()))
            # Paint borders for everything we restored.
            self.overlay.sync_all()

        # Remove any contexts/<sid>.json files left behind by a crash before
        # SessionEnd. After this point, every write to contexts/ is paired with
        # a matching delete on SessionEnd.
        contexts.sweep({s.session_id for s in self.store.all()}, self.contexts_dir)

        self._ingester_thread = threading.Thread(target=self.ingester.run, daemon=True)
        self._ingester_thread.start()

        x_fd = self.x11.fileno()
        log.info("daemon running; events_dir=%s", self.events_dir)
        try:
            while not self._stop.is_set():
                try:
                    ready, _, _ = select.select([x_fd], [], [], 1.0)
                except InterruptedError:
                    continue

                # Drain X11 events first (highest priority — geometry/destroy notifications).
                if x_fd in ready or self.x11.pending_events():
                    while self.x11.pending_events():
                        self._handle_x_event(self.x11.next_event())

                # Drain ingester events on the main thread (NOT the ingester thread).
                while True:
                    try:
                        evt = self._event_queue.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        self._on_event(evt)
                    except Exception:
                        log.exception("error processing event %r", evt)

                # Periodic idle sweep.
                now = time.monotonic()
                if now - self._last_sweep >= IDLE_SWEEP_INTERVAL_S:
                    self.store.evict_idle(now=time.time(), max_age_s=IDLE_MAX_AGE_S)
                    self._last_sweep = now

                # Dashboard tick (cheap when nothing has changed; no-op when not a TTY).
                if self.dashboard is not None:
                    self.dashboard.tick()
        finally:
            log.info("daemon stopping")
            self.ingester.stop()
            if self.persister is not None:
                self.persister.stop()
            if self.dashboard is not None:
                self.dashboard.shutdown()

    def stop(self) -> None:
        self._stop.set()
        self.ingester.stop()

    def _handle_x_event(self, event) -> None:
        et = event.type
        if et == X.ConfigureNotify:
            wid = event.window.id
            geo = Geometry(x=event.x, y=event.y, width=event.width, height=event.height)
            self.overlay.on_window_configure(wid, geo)
        elif et == X.DestroyNotify:
            wid = event.window.id
            # binder.unbind_window calls store.set_bound_window(None) which fires
            # on_change which the overlay handles via _sync_one -> _destroy.
            self.binder.unbind_window(wid)


def default_events_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "claude-alerts" / "events"


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "claude-alerts" / "config.toml"


def default_log_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "claude-alerts" / "daemon.log"


def default_persistence_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "claude-alerts" / "sessions.json"

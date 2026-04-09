"""Daemon entry point — wires ingester, sessions, binder, overlay on one event loop."""
from __future__ import annotations

import logging
import os
import select
import threading
import time
from pathlib import Path

from Xlib import X

from claude_alerts.binder import Binder
from claude_alerts.config import Config
from claude_alerts.events import ClaudeEvent
from claude_alerts.ingester import EventIngester
from claude_alerts.overlay import OverlayManager
from claude_alerts.sessions import SessionStore
from claude_alerts.x11 import Geometry, X11Client

log = logging.getLogger(__name__)

IDLE_SWEEP_INTERVAL_S = 30.0
IDLE_MAX_AGE_S = 300.0


class Daemon:
    def __init__(self, events_dir: Path, config: Config) -> None:
        self.events_dir = events_dir
        self.config = config
        self.store = SessionStore()
        self.x11 = X11Client()
        self.binder = Binder(self.store, self.x11)
        self.overlay = OverlayManager(self.x11, self.store, self.config)
        self.ingester = EventIngester(self.events_dir, on_event=self._on_event)
        self._stop = threading.Event()
        self._ingester_thread: threading.Thread | None = None
        self._last_sweep = time.monotonic()

    def _on_event(self, evt: ClaudeEvent) -> None:
        is_new = self.store.get(evt.session_id) is None
        self.store.apply_event(evt)
        if is_new and self.store.get(evt.session_id) is not None:
            # Try to bind. The binder calls store.set_bound_window which fires
            # the on_change callback the overlay is subscribed to, so the overlay
            # will be created automatically without an explicit call here.
            self.binder.try_bind(evt.session_id)

    def run(self) -> None:
        self.x11.subscribe_root_substructure()
        self._ingester_thread = threading.Thread(target=self.ingester.run, daemon=True)
        self._ingester_thread.start()

        x_fd = self.x11.fileno()
        log.info("daemon running; events_dir=%s", self.events_dir)
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([x_fd], [], [], 1.0)
            except InterruptedError:
                continue

            if x_fd in ready or self.x11.pending_events():
                while self.x11.pending_events():
                    self._handle_x_event(self.x11.next_event())

            now = time.monotonic()
            if now - self._last_sweep >= IDLE_SWEEP_INTERVAL_S:
                self.store.evict_idle(now=time.time(), max_age_s=IDLE_MAX_AGE_S)
                self._last_sweep = now

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
        elif et == X.VisibilityNotify:
            self.overlay.raise_all()


def default_events_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "claude-alerts" / "events"


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "claude-alerts" / "config.toml"


def default_log_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "claude-alerts" / "daemon.log"

"""Inotify-driven event file ingester."""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

from inotify_simple import INotify, flags

from claude_alerts.events import ClaudeEvent, EventParseError, parse_event_file

log = logging.getLogger(__name__)


class EventIngester:
    """Watches a directory for new event files and dispatches parsed events."""

    def __init__(self, events_dir: Path, on_event: Callable[[ClaudeEvent], None]) -> None:
        self.events_dir = events_dir
        self.on_event = on_event
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.rejected_dir = self.events_dir / "rejected"
        self.rejected_dir.mkdir(exist_ok=True)
        self._stop = threading.Event()
        self._inotify: INotify | None = None

    def run(self) -> None:
        """Blocking event loop. Drains backlog, then watches with inotify."""
        self._inotify = INotify()
        watch_flags = flags.MOVED_TO | flags.CLOSE_WRITE | flags.Q_OVERFLOW
        self._inotify.add_watch(str(self.events_dir), watch_flags)

        # Drain backlog AFTER the watch is registered so files written in the
        # gap between process start and watch registration are not lost.
        self._drain_backlog()

        while not self._stop.is_set():
            for event in self._inotify.read(timeout=200):
                if event.mask & flags.Q_OVERFLOW:
                    log.warning("inotify queue overflow; rescanning directory")
                    self._drain_backlog()
                    continue
                name = event.name
                if not name or not name.endswith(".json"):
                    continue
                self._process_file(self.events_dir / name)

    def stop(self) -> None:
        self._stop.set()

    def _drain_backlog(self) -> None:
        for path in sorted(self.events_dir.glob("*.json")):
            self._process_file(path)

    def _process_file(self, path: Path) -> None:
        if not path.exists():
            return
        try:
            evt = parse_event_file(path)
        except EventParseError as e:
            log.warning("rejecting %s: %s", path.name, e)
            try:
                path.rename(self.rejected_dir / path.name)
            except OSError:
                pass
            return
        try:
            self.on_event(evt)
        finally:
            try:
                path.unlink()
            except OSError:
                pass

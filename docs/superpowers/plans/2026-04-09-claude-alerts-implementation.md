# claude-alerts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a background Python daemon that draws colored overlay borders around Claude Code agent terminal windows on X11 — green when working, red when waiting on the user.

**Architecture:** Single Python process. Claude Code hooks write JSON event files to a watched directory; the daemon reads them via `inotify`, updates an in-memory session store, and renders click-through X11 overlay windows around bound terminals. No threads — one event loop multiplexes inotify and the X11 socket.

**Tech Stack:** Python 3.10+, `python-xlib` for X11, `inotify_simple` for filesystem events, `tomli` for config (Python 3.10 has no stdlib `tomllib`), `pytest` for tests, `Xvfb` for end-to-end testing. Target environment is Pop!_OS 22.04 with gnome-terminal.

**Spec:** `docs/superpowers/specs/2026-04-09-claude-agent-status-monitor-design.md`

---

## Task 1: Project scaffolding

Create the Python package, declare dependencies, get an empty `pytest` run passing. This task does not write any business logic — its only purpose is to confirm the test runner works before any TDD task does.

**Files:**
- Create: `pyproject.toml`
- Create: `claude_alerts/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "claude-alerts"
version = "0.1.0"
description = "Visual status monitor for Claude Code agents"
requires-python = ">=3.10"
dependencies = [
    "python-xlib>=0.33",
    "inotify_simple>=1.3.5",
    "tomli>=2.0; python_version<'3.11'",
]

[project.optional-dependencies]
dev = [
    "pytest>=7.4",
]

[project.scripts]
claude-alerts = "claude_alerts.__main__:main"

[tool.setuptools.packages.find]
include = ["claude_alerts*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 2: Create empty package files**

```bash
touch claude_alerts/__init__.py tests/__init__.py
```

- [ ] **Step 3: Create minimal `tests/conftest.py`**

```python
"""Shared test fixtures for claude-alerts."""
```

- [ ] **Step 4: Install in editable mode and run pytest**

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

Expected: `no tests ran in 0.0Xs` (zero failures, zero collected). If pip fails to install `python-xlib`, install system packages first with `sudo apt install libx11-dev` and retry.

- [ ] **Step 5: Add `.venv/` to gitignore and commit**

Append to `.gitignore`:
```
.venv/
__pycache__/
*.egg-info/
```

```bash
git add .gitignore pyproject.toml claude_alerts/__init__.py tests/__init__.py tests/conftest.py
git commit -m "chore: scaffold python package with pytest"
```

---

## Task 2: Event types and parsing

Define the `ClaudeEvent` dataclass and a function that parses an event file. Used by the ingester later.

**Files:**
- Create: `claude_alerts/events.py`
- Create: `tests/test_events.py`

- [ ] **Step 1: Write the failing test**

`tests/test_events.py`:
```python
import json
from pathlib import Path

import pytest

from claude_alerts.events import ClaudeEvent, EventParseError, parse_event_file


def write_event(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "evt.json"
    p.write_text(json.dumps(payload))
    return p


def test_parses_valid_event(tmp_path):
    p = write_event(
        tmp_path,
        {
            "event": "PreToolUse",
            "session_id": "abc123",
            "cwd": "/home/u/proj",
            "claude_pid": 4242,
            "timestamp": 1712599823.123,
        },
    )
    evt = parse_event_file(p)
    assert evt == ClaudeEvent(
        event="PreToolUse",
        session_id="abc123",
        cwd="/home/u/proj",
        claude_pid=4242,
        timestamp=1712599823.123,
    )


def test_rejects_unknown_event(tmp_path):
    p = write_event(tmp_path, {"event": "Frobnicate", "session_id": "x", "cwd": "/", "claude_pid": 1, "timestamp": 0})
    with pytest.raises(EventParseError, match="unknown event"):
        parse_event_file(p)


def test_rejects_missing_field(tmp_path):
    p = write_event(tmp_path, {"event": "Stop", "session_id": "x"})
    with pytest.raises(EventParseError, match="missing field"):
        parse_event_file(p)


def test_rejects_invalid_json(tmp_path):
    p = tmp_path / "evt.json"
    p.write_text("{not json")
    with pytest.raises(EventParseError, match="invalid json"):
        parse_event_file(p)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_events.py -v
```
Expected: `ImportError: cannot import name 'ClaudeEvent' from 'claude_alerts.events'`

- [ ] **Step 3: Implement `claude_alerts/events.py`**

```python
"""Event types and parsing for Claude Code hook events."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

VALID_EVENTS = frozenset({
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "Notification",
    "SessionEnd",
})

REQUIRED_FIELDS = ("event", "session_id", "cwd", "claude_pid", "timestamp")


class EventParseError(ValueError):
    """Raised when an event file cannot be parsed."""


@dataclass(frozen=True)
class ClaudeEvent:
    event: str
    session_id: str
    cwd: str
    claude_pid: int
    timestamp: float


def parse_event_file(path: Path) -> ClaudeEvent:
    """Parse a hook-emitted JSON event file. Raises EventParseError on any problem."""
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise EventParseError(f"invalid json: {e}") from e

    if not isinstance(raw, dict):
        raise EventParseError("invalid json: not an object")

    for field in REQUIRED_FIELDS:
        if field not in raw:
            raise EventParseError(f"missing field: {field}")

    if raw["event"] not in VALID_EVENTS:
        raise EventParseError(f"unknown event: {raw['event']}")

    return ClaudeEvent(
        event=raw["event"],
        session_id=str(raw["session_id"]),
        cwd=str(raw["cwd"]),
        claude_pid=int(raw["claude_pid"]),
        timestamp=float(raw["timestamp"]),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_events.py -v
```
Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add claude_alerts/events.py tests/test_events.py
git commit -m "feat(events): add ClaudeEvent dataclass and event file parser"
```

---

## Task 3: Session store and state machine

Pure in-memory store. Maps event types to status transitions. Exposes an `on_change` callback for the renderer to subscribe to. Includes the idle eviction sweep.

**Files:**
- Create: `claude_alerts/sessions.py`
- Create: `tests/test_sessions.py`

- [ ] **Step 1: Write the failing test**

`tests/test_sessions.py`:
```python
from claude_alerts.events import ClaudeEvent
from claude_alerts.sessions import Session, SessionStore, Status


def evt(event, session_id="s1", t=1.0):
    return ClaudeEvent(
        event=event, session_id=session_id, cwd="/p", claude_pid=1, timestamp=t,
    )


def test_session_start_creates_waiting_session():
    store = SessionStore()
    store.apply_event(evt("SessionStart"))
    s = store.get("s1")
    assert s is not None
    assert s.status == Status.WAITING


def test_user_prompt_submit_sets_working():
    store = SessionStore()
    store.apply_event(evt("SessionStart"))
    store.apply_event(evt("UserPromptSubmit", t=2.0))
    assert store.get("s1").status == Status.WORKING


def test_pre_tool_use_sets_working():
    store = SessionStore()
    store.apply_event(evt("SessionStart"))
    store.apply_event(evt("PreToolUse", t=2.0))
    assert store.get("s1").status == Status.WORKING


def test_post_tool_use_keeps_working():
    store = SessionStore()
    store.apply_event(evt("SessionStart"))
    store.apply_event(evt("PreToolUse", t=2.0))
    store.apply_event(evt("PostToolUse", t=3.0))
    assert store.get("s1").status == Status.WORKING


def test_stop_sets_waiting():
    store = SessionStore()
    store.apply_event(evt("SessionStart"))
    store.apply_event(evt("PreToolUse", t=2.0))
    store.apply_event(evt("Stop", t=3.0))
    assert store.get("s1").status == Status.WAITING


def test_notification_sets_waiting():
    store = SessionStore()
    store.apply_event(evt("SessionStart"))
    store.apply_event(evt("PreToolUse", t=2.0))
    store.apply_event(evt("Notification", t=3.0))
    assert store.get("s1").status == Status.WAITING


def test_session_end_removes_session():
    store = SessionStore()
    store.apply_event(evt("SessionStart"))
    store.apply_event(evt("SessionEnd", t=2.0))
    assert store.get("s1") is None


def test_orphan_notification_creates_session():
    store = SessionStore()
    store.apply_event(evt("Notification"))
    s = store.get("s1")
    assert s is not None
    assert s.status == Status.WAITING


def test_on_change_callback_fires_on_status_change():
    store = SessionStore()
    seen = []
    store.on_change(lambda sid: seen.append(sid))
    store.apply_event(evt("SessionStart"))  # creates -> change
    store.apply_event(evt("PreToolUse", t=2.0))  # waiting -> working
    store.apply_event(evt("PreToolUse", t=3.0))  # working -> working: NO change
    assert seen == ["s1", "s1"]


def test_idle_sweep_evicts_old_sessions():
    store = SessionStore()
    store.apply_event(evt("SessionStart", t=100.0))
    store.apply_event(evt("SessionStart", session_id="s2", t=900.0))
    # now=1000, idle threshold=300 -> s1 (last_event=100) should be evicted
    store.evict_idle(now=1000.0, max_age_s=300.0)
    assert store.get("s1") is None
    assert store.get("s2") is not None


def test_session_carries_cwd_and_pid():
    store = SessionStore()
    store.apply_event(evt("SessionStart"))
    s = store.get("s1")
    assert s.cwd == "/p"
    assert s.claude_pid == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_sessions.py -v
```
Expected: `ImportError: cannot import name 'SessionStore' from 'claude_alerts.sessions'`

- [ ] **Step 3: Implement `claude_alerts/sessions.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_sessions.py -v
```
Expected: `11 passed`.

- [ ] **Step 5: Commit**

```bash
git add claude_alerts/sessions.py tests/test_sessions.py
git commit -m "feat(sessions): in-memory session store with state machine"
```

---

## Task 4: Config loading

TOML config with built-in defaults. The user only needs to create the file if they want to override colors or border thickness.

**Files:**
- Create: `claude_alerts/config.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

`tests/test_config.py`:
```python
from pathlib import Path

from claude_alerts.config import Config, load_config


def test_defaults_when_no_file(tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.color_working == "#22c55e"
    assert cfg.color_waiting == "#ef4444"
    assert cfg.border_thickness_px == 4
    assert cfg.log_level == "INFO"


def test_overrides_from_file(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        '[colors]\n'
        'working = "#00ff00"\n'
        'waiting = "#ff0000"\n'
        '\n'
        '[border]\n'
        'thickness_px = 8\n'
        '\n'
        '[debug]\n'
        'log_level = "DEBUG"\n'
    )
    cfg = load_config(p)
    assert cfg.color_working == "#00ff00"
    assert cfg.color_waiting == "#ff0000"
    assert cfg.border_thickness_px == 8
    assert cfg.log_level == "DEBUG"


def test_partial_override(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[border]\nthickness_px = 12\n')
    cfg = load_config(p)
    assert cfg.border_thickness_px == 12
    assert cfg.color_working == "#22c55e"  # default preserved


def test_config_is_a_dataclass():
    cfg = Config()
    assert cfg.color_working == "#22c55e"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_config.py -v
```
Expected: `ImportError: cannot import name 'Config' from 'claude_alerts.config'`

- [ ] **Step 3: Implement `claude_alerts/config.py`**

```python
"""Configuration loading from TOML with built-in defaults."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


@dataclass
class Config:
    color_working: str = "#22c55e"
    color_waiting: str = "#ef4444"
    border_thickness_px: int = 4
    log_level: str = "INFO"


def load_config(path: Path) -> Config:
    """Load config from TOML file. Missing file => all defaults."""
    cfg = Config()
    if not path.exists():
        return cfg

    with path.open("rb") as f:
        data = tomllib.load(f)

    colors = data.get("colors", {})
    if "working" in colors:
        cfg.color_working = str(colors["working"])
    if "waiting" in colors:
        cfg.color_waiting = str(colors["waiting"])

    border = data.get("border", {})
    if "thickness_px" in border:
        cfg.border_thickness_px = int(border["thickness_px"])

    debug = data.get("debug", {})
    if "log_level" in debug:
        cfg.log_level = str(debug["log_level"])

    return cfg
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_config.py -v
```
Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add claude_alerts/config.py tests/test_config.py
git commit -m "feat(config): TOML config loading with defaults"
```

---

## Task 5: Inotify event ingester

Watches the events directory with `inotify_simple`. On a new file (`IN_MOVED_TO`), parses it and dispatches to a callback. Drains backlog on startup. Moves malformed files to `events/rejected/`.

**Files:**
- Create: `claude_alerts/ingester.py`
- Create: `tests/test_ingester.py`

- [ ] **Step 1: Write the failing test**

`tests/test_ingester.py`:
```python
import json
import threading
import time
from pathlib import Path

from claude_alerts.events import ClaudeEvent
from claude_alerts.ingester import EventIngester


def write_event_atomically(events_dir: Path, payload: dict, name: str = "evt") -> None:
    tmp = events_dir / f"{name}.json.tmp"
    final = events_dir / f"{name}.json"
    tmp.write_text(json.dumps(payload))
    tmp.rename(final)


def good_payload(session_id="s1"):
    return {
        "event": "Stop",
        "session_id": session_id,
        "cwd": "/p",
        "claude_pid": 99,
        "timestamp": 1.0,
    }


def run_ingester_in_thread(ingester):
    t = threading.Thread(target=ingester.run, daemon=True)
    t.start()
    return t


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_dispatches_event_when_file_arrives(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    received = []
    ingester = EventIngester(events_dir, on_event=received.append)
    run_ingester_in_thread(ingester)
    try:
        write_event_atomically(events_dir, good_payload())
        assert wait_for(lambda: len(received) == 1)
        assert isinstance(received[0], ClaudeEvent)
        assert received[0].session_id == "s1"
    finally:
        ingester.stop()


def test_drains_backlog_on_start(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    write_event_atomically(events_dir, good_payload("backlog1"), name="b1")
    write_event_atomically(events_dir, good_payload("backlog2"), name="b2")
    received = []
    ingester = EventIngester(events_dir, on_event=received.append)
    run_ingester_in_thread(ingester)
    try:
        assert wait_for(lambda: len(received) == 2)
        assert {e.session_id for e in received} == {"backlog1", "backlog2"}
    finally:
        ingester.stop()


def test_deletes_event_file_after_processing(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    received = []
    ingester = EventIngester(events_dir, on_event=received.append)
    run_ingester_in_thread(ingester)
    try:
        write_event_atomically(events_dir, good_payload())
        assert wait_for(lambda: len(received) == 1)
        # The .json file should be gone
        assert list(events_dir.glob("*.json")) == []
    finally:
        ingester.stop()


def test_moves_malformed_file_to_rejected(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    received = []
    ingester = EventIngester(events_dir, on_event=received.append)
    run_ingester_in_thread(ingester)
    try:
        bad = events_dir / "bad.json.tmp"
        bad.write_text("{not json")
        bad.rename(events_dir / "bad.json")
        assert wait_for(lambda: (events_dir / "rejected" / "bad.json").exists())
        assert received == []
    finally:
        ingester.stop()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_ingester.py -v
```
Expected: `ImportError: cannot import name 'EventIngester' from 'claude_alerts.ingester'`

- [ ] **Step 3: Implement `claude_alerts/ingester.py`**

```python
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
        self._drain_backlog()

        self._inotify = INotify()
        watch_flags = flags.MOVED_TO | flags.CLOSE_WRITE | flags.Q_OVERFLOW
        self._inotify.add_watch(str(self.events_dir), watch_flags)

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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_ingester.py -v
```
Expected: `4 passed`. The tests use threads because `EventIngester.run()` blocks; this is real I/O against a real inotify watch and a real temp directory.

- [ ] **Step 5: Commit**

```bash
git add claude_alerts/ingester.py tests/test_ingester.py
git commit -m "feat(ingester): inotify event file watcher with backlog drain"
```

---

## Task 6: X11 connection helpers

A small wrapper around `python-xlib` that exposes only the operations we need: connect to the display, query the active window, query a window's `WM_CLASS` and geometry, list top-level windows, subscribe to root window substructure events. Centralized so the binder and overlay both use the same connection.

**Files:**
- Create: `claude_alerts/x11.py`
- Create: `tests/test_x11.py`

- [ ] **Step 1: Write the failing test**

This task is mostly an I/O wrapper. We test only the pieces that are testable without a display: that the module imports, exposes the expected names, and the helper that converts a `WM_CLASS` tuple to a class string handles edge cases.

`tests/test_x11.py`:
```python
import pytest

from claude_alerts.x11 import wm_class_string


def test_wm_class_string_basic():
    assert wm_class_string(("gnome-terminal-server", "Gnome-terminal")) == "gnome-terminal-server"


def test_wm_class_string_handles_none():
    assert wm_class_string(None) == ""


def test_wm_class_string_handles_empty():
    assert wm_class_string(()) == ""


def test_module_exposes_expected_api():
    from claude_alerts import x11
    for name in ("X11Client", "wm_class_string", "Geometry"):
        assert hasattr(x11, name), f"missing {name}"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_x11.py -v
```
Expected: `ImportError: cannot import name 'wm_class_string' from 'claude_alerts.x11'`

- [ ] **Step 3: Implement `claude_alerts/x11.py`**

```python
"""X11 connection helpers — a thin wrapper around python-xlib for the operations we need."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from Xlib import X, display
from Xlib.protocol import event as xevent


@dataclass(frozen=True)
class Geometry:
    x: int
    y: int
    width: int
    height: int


def wm_class_string(wm_class: Optional[Iterable[str]]) -> str:
    """Convert a WM_CLASS tuple (instance, class) into the instance string. Empty if absent."""
    if not wm_class:
        return ""
    items = list(wm_class)
    if not items:
        return ""
    return str(items[0])


class X11Client:
    """Owns the connection to the X server. One per daemon process."""

    def __init__(self) -> None:
        self.display = display.Display()
        self.screen = self.display.screen()
        self.root = self.screen.root
        self._NET_ACTIVE_WINDOW = self.display.intern_atom("_NET_ACTIVE_WINDOW")
        self._NET_CLIENT_LIST = self.display.intern_atom("_NET_CLIENT_LIST")
        self._NET_WM_STATE = self.display.intern_atom("_NET_WM_STATE")
        self._NET_WM_STATE_ABOVE = self.display.intern_atom("_NET_WM_STATE_ABOVE")

    def fileno(self) -> int:
        return self.display.fileno()

    def flush(self) -> None:
        self.display.flush()

    def get_active_window_id(self) -> Optional[int]:
        prop = self.root.get_full_property(self._NET_ACTIVE_WINDOW, X.AnyPropertyType)
        if not prop or not prop.value:
            return None
        wid = int(prop.value[0])
        return wid or None

    def get_wm_class(self, window_id: int) -> str:
        try:
            win = self.display.create_resource_object("window", window_id)
            cls = win.get_wm_class()
            return wm_class_string(cls)
        except Exception:
            return ""

    def get_geometry(self, window_id: int) -> Optional[Geometry]:
        try:
            win = self.display.create_resource_object("window", window_id)
            geo = win.get_geometry()
            # Translate to root coordinates
            coords = win.translate_coords(self.root, 0, 0)
            return Geometry(
                x=-coords.x, y=-coords.y, width=geo.width, height=geo.height,
            )
        except Exception:
            return None

    def list_top_level_windows(self) -> list[int]:
        prop = self.root.get_full_property(self._NET_CLIENT_LIST, X.AnyPropertyType)
        if not prop:
            return []
        return [int(w) for w in prop.value]

    def subscribe_root_substructure(self) -> None:
        """Receive ConfigureNotify and DestroyNotify for all top-level windows."""
        self.root.change_attributes(event_mask=X.SubstructureNotifyMask)
        self.display.flush()

    def pending_events(self) -> int:
        return self.display.pending_events()

    def next_event(self):
        return self.display.next_event()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_x11.py -v
```
Expected: `4 passed`. (No display required — the X11Client class itself is not instantiated in unit tests, only the pure helper.)

- [ ] **Step 5: Commit**

```bash
git add claude_alerts/x11.py tests/test_x11.py
git commit -m "feat(x11): connection helpers wrapping python-xlib"
```

---

## Task 7: Window binder

Holds the active-window heuristic and the click-to-bind fallback queue. Uses a fake `X11Client` in tests so it can run without a display.

**Files:**
- Create: `claude_alerts/binder.py`
- Create: `tests/test_binder.py`

- [ ] **Step 1: Write the failing test**

`tests/test_binder.py`:
```python
from claude_alerts.binder import Binder
from claude_alerts.events import ClaudeEvent
from claude_alerts.sessions import SessionStore


class FakeX11:
    """Hand-controlled fake of the parts of X11Client the binder uses."""
    def __init__(self):
        self.active_window_id = None
        self.wm_classes: dict[int, str] = {}

    def get_active_window_id(self):
        return self.active_window_id

    def get_wm_class(self, window_id):
        return self.wm_classes.get(window_id, "")


def evt(event="SessionStart", session_id="s1", t=1.0):
    return ClaudeEvent(
        event=event, session_id=session_id, cwd="/p", claude_pid=1, timestamp=t,
    )


def test_binds_active_window_when_it_is_a_terminal():
    store = SessionStore()
    x = FakeX11()
    x.active_window_id = 0xAA
    x.wm_classes[0xAA] = "gnome-terminal-server"
    binder = Binder(store, x)
    store.apply_event(evt("SessionStart"))
    binder.try_bind("s1")
    assert store.get("s1").bound_window_id == 0xAA
    assert binder.pending_manual_binds() == []


def test_queues_for_manual_bind_when_active_window_is_not_a_terminal():
    store = SessionStore()
    x = FakeX11()
    x.active_window_id = 0xBB
    x.wm_classes[0xBB] = "firefox"
    binder = Binder(store, x)
    store.apply_event(evt("SessionStart"))
    binder.try_bind("s1")
    assert store.get("s1").bound_window_id is None
    assert binder.pending_manual_binds() == ["s1"]


def test_queues_for_manual_bind_when_no_active_window():
    store = SessionStore()
    x = FakeX11()
    x.active_window_id = None
    binder = Binder(store, x)
    store.apply_event(evt("SessionStart"))
    binder.try_bind("s1")
    assert binder.pending_manual_binds() == ["s1"]


def test_complete_manual_bind_assigns_window_and_clears_queue():
    store = SessionStore()
    x = FakeX11()
    x.active_window_id = None
    binder = Binder(store, x)
    store.apply_event(evt("SessionStart"))
    binder.try_bind("s1")
    binder.complete_manual_bind("s1", 0xCC)
    assert store.get("s1").bound_window_id == 0xCC
    assert binder.pending_manual_binds() == []


def test_binder_recognises_kitty_alacritty_too():
    """Forward-compat allowlist: gnome-terminal is the target but allow other terminals."""
    store = SessionStore()
    x = FakeX11()
    x.active_window_id = 0xDD
    x.wm_classes[0xDD] = "kitty"
    binder = Binder(store, x)
    store.apply_event(evt("SessionStart"))
    binder.try_bind("s1")
    assert store.get("s1").bound_window_id == 0xDD


def test_try_bind_no_op_for_unknown_session():
    store = SessionStore()
    x = FakeX11()
    binder = Binder(store, x)
    binder.try_bind("ghost")
    # No exception, no entry created.
    assert store.get("ghost") is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_binder.py -v
```
Expected: `ImportError: cannot import name 'Binder' from 'claude_alerts.binder'`

- [ ] **Step 3: Implement `claude_alerts/binder.py`**

```python
"""Binds Claude sessions to X11 terminal windows."""
from __future__ import annotations

import logging

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

        session.bound_window_id = wid
        log.info("session %s bound to window %#x", session_id, wid)

    def complete_manual_bind(self, session_id: str, window_id: int) -> None:
        session = self.store.get(session_id)
        if session is None:
            return
        session.bound_window_id = window_id
        if session_id in self._pending:
            self._pending.remove(session_id)

    def pending_manual_binds(self) -> list[str]:
        return list(self._pending)

    def unbind_window(self, window_id: int) -> None:
        """Called when a bound window has been destroyed."""
        for s in self.store.all():
            if s.bound_window_id == window_id:
                s.bound_window_id = None

    def _enqueue(self, session_id: str) -> None:
        if session_id not in self._pending:
            self._pending.append(session_id)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_binder.py -v
```
Expected: `6 passed`.

- [ ] **Step 5: Commit**

```bash
git add claude_alerts/binder.py tests/test_binder.py
git commit -m "feat(binder): active-window heuristic with manual bind queue"
```

---

## Task 8: Overlay renderer

Manages one click-through X11 window per bound session. Uses `XShape` to clip the interior of the overlay so clicks fall through to the terminal underneath. Real X11 — only tested via the end-to-end Xvfb test in Task 12, but smoke-imports here.

**Files:**
- Create: `claude_alerts/overlay.py`
- Create: `tests/test_overlay_smoke.py`

- [ ] **Step 1: Write the failing smoke test**

`tests/test_overlay_smoke.py`:
```python
"""Import-only smoke test. Real behaviour is verified in test_e2e_xvfb.py."""

def test_overlay_module_imports():
    from claude_alerts import overlay
    assert hasattr(overlay, "OverlayManager")


def test_hex_to_rgb_pixel_pure_helper():
    from claude_alerts.overlay import hex_to_rgb
    assert hex_to_rgb("#ff0000") == (0xff, 0x00, 0x00)
    assert hex_to_rgb("#22c55e") == (0x22, 0xc5, 0x5e)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_overlay_smoke.py -v
```
Expected: `ModuleNotFoundError: No module named 'claude_alerts.overlay'`.

- [ ] **Step 3: Implement `claude_alerts/overlay.py`**

```python
"""X11 click-through border overlays for bound terminal windows."""
from __future__ import annotations

import logging
from typing import Optional

from Xlib import X, Xatom
from Xlib.ext import shape

from claude_alerts.config import Config
from claude_alerts.sessions import Session, SessionStore, Status
from claude_alerts.x11 import Geometry, X11Client

log = logging.getLogger(__name__)


def hex_to_rgb(s: str) -> tuple[int, int, int]:
    s = s.lstrip("#")
    if len(s) != 6:
        raise ValueError(f"bad hex color: {s}")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def _rgb_to_pixel(rgb: tuple[int, int, int]) -> int:
    r, g, b = rgb
    return (r << 16) | (g << 8) | b


class _OverlayWindow:
    """One overlay window tracking one terminal."""

    def __init__(self, x11: X11Client, target_geo: Geometry, color_pixel: int, thickness: int) -> None:
        self.x11 = x11
        self.thickness = thickness
        self.color_pixel = color_pixel
        self.win = x11.screen.root.create_window(
            target_geo.x, target_geo.y, target_geo.width, target_geo.height, 0,
            X.CopyFromParent, X.InputOutput, X.CopyFromParent,
            override_redirect=1,
            background_pixel=color_pixel,
            event_mask=X.ExposureMask | X.VisibilityChangeMask,
        )
        # _NET_WM_STATE is a list of ATOMs, so the property type must be ATOM (not AnyPropertyType,
        # which is only valid for get_property queries).
        self.win.change_property(
            x11._NET_WM_STATE, Xatom.ATOM, 32, [x11._NET_WM_STATE_ABOVE],
        )
        self._apply_shape(target_geo)
        self.win.map()
        x11.flush()

    def _apply_shape(self, geo: Geometry) -> None:
        """Restrict the overlay's input region to just the border edges so clicks pass through the middle."""
        t = self.thickness
        edges = [
            (0, 0, geo.width, t),                              # top
            (0, geo.height - t, geo.width, t),                 # bottom
            (0, 0, t, geo.height),                             # left
            (geo.width - t, 0, t, geo.height),                 # right
        ]
        # python-xlib exposes shape constants as SO_Set / SK_Input. The ordering constant
        # lives in Xlib.X (X.YXBanded == 3).
        self.win.shape_rectangles(
            shape.SO_Set,
            shape.SK_Input,
            X.YXBanded,
            0, 0,
            edges,
        )

    def update_geometry(self, geo: Geometry) -> None:
        self.win.configure(x=geo.x, y=geo.y, width=geo.width, height=geo.height)
        self._apply_shape(geo)
        self.x11.flush()

    def set_color(self, color_pixel: int) -> None:
        self.color_pixel = color_pixel
        self.win.change_attributes(background_pixel=color_pixel)
        self.win.clear_area(0, 0, 0, 0, True)
        self.x11.flush()

    def raise_above(self) -> None:
        self.win.configure(stack_mode=X.Above)
        self.x11.flush()

    def destroy(self) -> None:
        try:
            self.win.destroy()
            self.x11.flush()
        except Exception:
            pass


class OverlayManager:
    """One per daemon. Maintains an _OverlayWindow per bound session."""

    def __init__(self, x11: X11Client, store: SessionStore, config: Config) -> None:
        self.x11 = x11
        self.store = store
        self.config = config
        self._overlays: dict[str, _OverlayWindow] = {}
        self._working_pixel = _rgb_to_pixel(hex_to_rgb(config.color_working))
        self._waiting_pixel = _rgb_to_pixel(hex_to_rgb(config.color_waiting))
        store.on_change(self.on_session_changed)

    def color_for(self, status: Status) -> int:
        return self._working_pixel if status == Status.WORKING else self._waiting_pixel

    def on_session_changed(self, session_id: str) -> None:
        session = self.store.get(session_id)
        if session is None:
            self._destroy(session_id)
            return
        self._sync_one(session)

    def on_window_configure(self, window_id: int, geo: Geometry) -> None:
        for s in self.store.all():
            if s.bound_window_id == window_id and s.session_id in self._overlays:
                self._overlays[s.session_id].update_geometry(geo)

    def on_window_destroyed(self, window_id: int) -> None:
        for s in self.store.all():
            if s.bound_window_id == window_id:
                self._destroy(s.session_id)
                s.bound_window_id = None

    def refresh_all_geometry(self) -> None:
        for s in self.store.all():
            if s.bound_window_id and s.session_id in self._overlays:
                geo = self.x11.get_geometry(s.bound_window_id)
                if geo is not None:
                    self._overlays[s.session_id].update_geometry(geo)

    def raise_all(self) -> None:
        """Re-raise every overlay above the stack. Called on VisibilityNotify."""
        for ov in self._overlays.values():
            ov.raise_above()

    def has_overlay(self, session_id: str) -> bool:
        return session_id in self._overlays

    def _sync_one(self, session: Session) -> None:
        if session.bound_window_id is None:
            self._destroy(session.session_id)
            return
        geo = self.x11.get_geometry(session.bound_window_id)
        if geo is None:
            self._destroy(session.session_id)
            return
        existing = self._overlays.get(session.session_id)
        color_pixel = self.color_for(session.status)
        if existing is None:
            self._overlays[session.session_id] = _OverlayWindow(
                self.x11, geo, color_pixel, self.config.border_thickness_px,
            )
        else:
            existing.update_geometry(geo)
            existing.set_color(color_pixel)

    def _destroy(self, session_id: str) -> None:
        ov = self._overlays.pop(session_id, None)
        if ov is not None:
            ov.destroy()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_overlay_smoke.py -v
```
Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add claude_alerts/overlay.py tests/test_overlay_smoke.py
git commit -m "feat(overlay): X11 click-through border overlay manager"
```

---

## Task 9: Hook script

A 5-line bash script that reads the hook payload from stdin and writes a JSON event file atomically. Installed by Task 10.

**Files:**
- Create: `scripts/hooks/emit-event.sh`
- Create: `tests/test_hook_script.py`

- [ ] **Step 1: Write the failing test**

`tests/test_hook_script.py`:
```python
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

HOOK_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "emit-event.sh"


def has_jq():
    return shutil.which("jq") is not None


@pytest.mark.skipif(not has_jq(), reason="jq is required by the hook script")
def test_hook_script_writes_event_file(tmp_path):
    env = os.environ.copy()
    env["CLAUDE_ALERTS_EVENTS_DIR"] = str(tmp_path)
    payload = json.dumps({"session_id": "abc123", "cwd": "/proj"})
    subprocess.run(
        ["bash", str(HOOK_SCRIPT), "PreToolUse"],
        input=payload, text=True, env=env, check=True,
    )
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["event"] == "PreToolUse"
    assert data["session_id"] == "abc123"
    assert data["cwd"] == "/proj"
    assert "claude_pid" in data
    assert "timestamp" in data


@pytest.mark.skipif(not has_jq(), reason="jq is required by the hook script")
def test_hook_script_atomic_write(tmp_path):
    """No .tmp files left lying around."""
    env = os.environ.copy()
    env["CLAUDE_ALERTS_EVENTS_DIR"] = str(tmp_path)
    subprocess.run(
        ["bash", str(HOOK_SCRIPT), "Stop"],
        input='{"session_id":"x","cwd":"/y"}', text=True, env=env, check=True,
    )
    assert list(tmp_path.glob("*.tmp")) == []
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_hook_script.py -v
```
Expected: `FileNotFoundError` for `scripts/hooks/emit-event.sh`. (Or skipped if `jq` is missing — install with `sudo apt install jq`.)

- [ ] **Step 3: Implement `scripts/hooks/emit-event.sh`**

```bash
#!/usr/bin/env bash
# emit-event.sh — write a Claude Code hook event to the claude-alerts events directory.
# Usage: emit-event.sh <EVENT_NAME>
# Reads the hook JSON payload from stdin.

set -euo pipefail

EVENT="${1:?event name required}"
EVENTS_DIR="${CLAUDE_ALERTS_EVENTS_DIR:-$HOME/.local/state/claude-alerts/events}"
mkdir -p "$EVENTS_DIR"

PAYLOAD="$(cat || true)"
SESSION_ID="$(printf '%s' "$PAYLOAD" | jq -r '.session_id // empty')"
CWD="$(printf '%s' "$PAYLOAD" | jq -r '.cwd // empty')"
[ -z "$SESSION_ID" ] && SESSION_ID="unknown"
[ -z "$CWD" ] && CWD="$(pwd)"

TS="$(date +%s.%N)"
NAME="${TS}-${SESSION_ID}.json"
TMP="${EVENTS_DIR}/${NAME}.tmp"
FINAL="${EVENTS_DIR}/${NAME}"

jq -cn \
    --arg event "$EVENT" \
    --arg session_id "$SESSION_ID" \
    --arg cwd "$CWD" \
    --argjson claude_pid "$$" \
    --argjson timestamp "$TS" \
    '{event:$event, session_id:$session_id, cwd:$cwd, claude_pid:$claude_pid, timestamp:$timestamp}' \
    > "$TMP"
mv "$TMP" "$FINAL"
exit 0
```

```bash
chmod +x scripts/hooks/emit-event.sh
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_hook_script.py -v
```
Expected: `2 passed` (or `2 skipped` if `jq` isn't installed; install it then re-run).

- [ ] **Step 5: Commit**

```bash
git add scripts/hooks/emit-event.sh tests/test_hook_script.py
git commit -m "feat(hooks): emit-event.sh writes atomic JSON event files"
```

---

## Task 10: Hook installer

Python script that does a JSON-aware merge of the hook configuration into `~/.claude/settings.json`, preserving any unrelated settings the user already has.

**Files:**
- Create: `scripts/install-hooks.py`
- Create: `tests/test_install.py`

- [ ] **Step 1: Write the failing test**

`tests/test_install.py`:
```python
import json
import sys
from pathlib import Path

import pytest

# Make sure scripts/ is importable as if it were a package directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import importlib.util
spec = importlib.util.spec_from_file_location(
    "install_hooks",
    Path(__file__).resolve().parents[1] / "scripts" / "install-hooks.py",
)
install_hooks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install_hooks)


HOOK_PATH = "/abs/path/to/emit-event.sh"
HOOK_EVENTS = (
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "Stop", "Notification", "SessionEnd",
)


def test_creates_settings_when_absent(tmp_path):
    settings = tmp_path / "settings.json"
    install_hooks.merge_hooks_into(settings, HOOK_PATH)
    data = json.loads(settings.read_text())
    assert "hooks" in data
    for evt in HOOK_EVENTS:
        assert evt in data["hooks"]
        assert any(HOOK_PATH in str(item) for item in data["hooks"][evt])


def test_preserves_unrelated_settings(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"theme": "dark", "model": "claude-opus-4-6"}))
    install_hooks.merge_hooks_into(settings, HOOK_PATH)
    data = json.loads(settings.read_text())
    assert data["theme"] == "dark"
    assert data["model"] == "claude-opus-4-6"
    assert "hooks" in data


def test_idempotent_no_duplicates(tmp_path):
    settings = tmp_path / "settings.json"
    install_hooks.merge_hooks_into(settings, HOOK_PATH)
    install_hooks.merge_hooks_into(settings, HOOK_PATH)
    data = json.loads(settings.read_text())
    for evt in HOOK_EVENTS:
        matches = [i for i in data["hooks"][evt] if HOOK_PATH in str(i)]
        assert len(matches) == 1, f"event {evt} got {len(matches)} matches"


def test_preserves_other_users_hooks(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "PreToolUse": [
                {"hooks": [{"type": "command", "command": "/other/hook.sh"}]}
            ]
        }
    }))
    install_hooks.merge_hooks_into(settings, HOOK_PATH)
    data = json.loads(settings.read_text())
    pretool = data["hooks"]["PreToolUse"]
    assert any("/other/hook.sh" in json.dumps(item) for item in pretool)
    assert any(HOOK_PATH in json.dumps(item) for item in pretool)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_install.py -v
```
Expected: `FileNotFoundError` for `scripts/install-hooks.py`.

- [ ] **Step 3: Implement `scripts/install-hooks.py`**

```python
#!/usr/bin/env python3
"""Install claude-alerts hook entries into ~/.claude/settings.json without clobbering."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "Notification",
    "SessionEnd",
)


def _hook_entry(hook_path: str, event: str) -> dict:
    return {
        "hooks": [
            {
                "type": "command",
                "command": f"{hook_path} {event}",
            }
        ]
    }


def merge_hooks_into(settings_path: Path, hook_path: str) -> None:
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            data = {}
    else:
        data = {}

    if not isinstance(data, dict):
        data = {}

    hooks = data.setdefault("hooks", {})
    for event in HOOK_EVENTS:
        bucket = hooks.setdefault(event, [])
        already = any(hook_path in json.dumps(item) for item in bucket)
        if not already:
            bucket.append(_hook_entry(hook_path, event))

    settings_path.write_text(json.dumps(data, indent=2) + "\n")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--settings",
        default=str(Path.home() / ".claude" / "settings.json"),
        help="Path to Claude settings.json",
    )
    p.add_argument(
        "--hook-path",
        default=str(Path(__file__).resolve().parent / "hooks" / "emit-event.sh"),
        help="Absolute path to emit-event.sh",
    )
    args = p.parse_args()
    merge_hooks_into(Path(args.settings), os.path.abspath(args.hook_path))
    print(f"Installed claude-alerts hooks into {args.settings}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_install.py -v
```
Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add scripts/install-hooks.py tests/test_install.py
git commit -m "feat(install): JSON-aware hook installer for ~/.claude/settings.json"
```

---

## Task 11: Daemon main and CLI entrypoint

Wires every component on a single event loop. The loop multiplexes the inotify file descriptor (via the ingester running in a thread because `inotify_simple.read()` is blocking and that's fine for our purposes) and the X11 connection (drained in the main thread). Performs the periodic idle sweep. Handles command-line flags for the events directory and config file path.

**Files:**
- Create: `claude_alerts/daemon.py`
- Create: `claude_alerts/__main__.py`

- [ ] **Step 1: Implement `claude_alerts/daemon.py`**

(No unit test for this file — it's pure wiring and is exercised by the end-to-end test in Task 12.)

```python
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
            self.binder.try_bind(evt.session_id)
            # Re-sync overlay now that the session may be bound.
            self.overlay.on_session_changed(evt.session_id)

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
            self.overlay.on_window_destroyed(wid)
            self.binder.unbind_window(wid)
        elif et == X.VisibilityNotify:
            # Re-raise overlays if occluded.
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
```

- [ ] **Step 2: Implement `claude_alerts/__main__.py`**

```python
"""CLI entrypoint: `python -m claude_alerts` or `claude-alerts`."""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
from pathlib import Path

from claude_alerts.config import load_config
from claude_alerts.daemon import (
    Daemon,
    default_config_path,
    default_events_dir,
    default_log_path,
)


def configure_logging(level: str, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=2,
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    if os.environ.get("CLAUDE_ALERTS_DEBUG") == "1":
        root.setLevel(logging.DEBUG)
        root.addHandler(logging.StreamHandler(sys.stderr))


def main() -> int:
    p = argparse.ArgumentParser(prog="claude-alerts")
    p.add_argument("--events-dir", type=Path, default=None)
    p.add_argument("--config", type=Path, default=None)
    args = p.parse_args()

    config_path = args.config or default_config_path()
    cfg = load_config(config_path)
    log_path = default_log_path()
    configure_logging(cfg.log_level, log_path)

    events_dir = args.events_dir or default_events_dir()
    daemon = Daemon(events_dir=events_dir, config=cfg)
    try:
        daemon.run()
    except KeyboardInterrupt:
        daemon.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Smoke-import the daemon module**

```bash
.venv/bin/pytest -q -k "" tests/  # full suite, ensure nothing regressed
.venv/bin/python -c "from claude_alerts.daemon import Daemon; print('ok')"
```
Expected: full test suite still green; `ok` printed.

- [ ] **Step 4: Commit**

```bash
git add claude_alerts/daemon.py claude_alerts/__main__.py
git commit -m "feat(daemon): wire components on single event loop with CLI entrypoint"
```

---

## Task 12: End-to-end Xvfb test

One slow test that boots a headless X server, spawns a fake terminal window, runs the real daemon, drops synthetic events, and asserts overlay behaviour. Marked so it can be skipped in environments without `Xvfb`.

**Files:**
- Create: `tests/test_e2e_xvfb.py`

- [ ] **Step 1: Write the test**

`tests/test_e2e_xvfb.py`:
```python
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
```

- [ ] **Step 2: Install Xvfb if not present and run the e2e test**

```bash
which Xvfb || sudo apt install -y xvfb
.venv/bin/pytest tests/test_e2e_xvfb.py -v
```
Expected: `1 passed`. If you see `Xvfb not installed`, the apt install above didn't take effect — try `apt-cache policy xvfb`.

- [ ] **Step 3: Run the entire suite as a regression check**

```bash
.venv/bin/pytest -v
```
Expected: every test from Tasks 2–12 passes.

- [ ] **Step 4: Commit**

```bash
git add tests/test_e2e_xvfb.py
git commit -m "test(e2e): xvfb smoke test for full daemon round-trip"
```

---

## Task 13: systemd user unit and README

A user-level systemd unit so the daemon starts on login and restarts on crash, plus a README that documents installation and operation.

**Files:**
- Create: `scripts/claude-alerts.service`
- Create: `README.md`

- [ ] **Step 1: Write `scripts/claude-alerts.service`**

```ini
[Unit]
Description=claude-alerts: visual status borders for Claude Code agents
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=%h/.local/bin/claude-alerts
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
```

- [ ] **Step 2: Write `README.md`**

```markdown
# claude-alerts

Background daemon that draws colored borders around Claude Code agent terminal
windows on X11 — **green when the agent is working, red when it is waiting on
you for any reason** (a permission prompt, an idle turn, anything).

Target environment: Pop!_OS 22.04, X11, gnome-terminal. Other terminals with a
distinctive `WM_CLASS` may work but are not tested.

## Install

Requires Python 3.10+, `jq`, and `Xvfb` for tests.

    sudo apt install -y python3-venv python3-dev libx11-dev jq
    git clone <this repo> ~/src/claude-alerts
    cd ~/src/claude-alerts
    python3 -m venv ~/.local/share/claude-alerts/venv
    ~/.local/share/claude-alerts/venv/bin/pip install .
    ln -sf ~/.local/share/claude-alerts/venv/bin/claude-alerts ~/.local/bin/claude-alerts

Then install the hook entries into your Claude settings:

    python3 scripts/install-hooks.py

This is a JSON-aware merge — your existing settings are preserved.

## Run

One-shot:

    claude-alerts

Background as a systemd user unit:

    mkdir -p ~/.config/systemd/user
    cp scripts/claude-alerts.service ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now claude-alerts.service
    systemctl --user status claude-alerts.service

Logs:

    tail -f ~/.local/state/claude-alerts/daemon.log

## Configuration

Optional. Defaults are baked in. To override, create
`~/.config/claude-alerts/config.toml`:

```toml
[colors]
working = "#22c55e"
waiting = "#ef4444"

[border]
thickness_px = 4

[debug]
log_level = "INFO"
```

## Troubleshooting

- **No border appears around a new agent.** Check `~/.local/state/claude-alerts/daemon.log`. Most common cause: the binder couldn't identify the active window as a terminal at SessionStart. The daemon will queue a manual bind — click your terminal window when prompted.
- **Daemon won't start with `Xlib.error.DisplayConnectionError`.** You're not in an X11 session. This tool does not support Wayland.
- **Border drawn under the terminal after raise-on-click.** The daemon re-raises overlays on `VisibilityNotify`, but if your window manager is aggressive, file an issue.

## Development

    python3 -m venv .venv
    .venv/bin/pip install -e ".[dev]"
    .venv/bin/pytest

The end-to-end test under `tests/test_e2e_xvfb.py` requires Xvfb.
```

- [ ] **Step 3: Final smoke test**

```bash
.venv/bin/pytest -v
```
Expected: every test passes.

- [ ] **Step 4: Commit**

```bash
git add scripts/claude-alerts.service README.md
git commit -m "docs: add README and systemd user unit"
```

---

## Done

After Task 13, the project is feature-complete per the spec:

- Hook script + installer (Tasks 9, 10) — `scripts/hooks/emit-event.sh`, `scripts/install-hooks.py`
- Event types + parser (Task 2) — `claude_alerts/events.py`
- Session store (Task 3) — `claude_alerts/sessions.py`
- Config (Task 4) — `claude_alerts/config.py`
- Inotify ingester (Task 5) — `claude_alerts/ingester.py`
- X11 helpers (Task 6) — `claude_alerts/x11.py`
- Window binder (Task 7) — `claude_alerts/binder.py`
- Overlay renderer (Task 8) — `claude_alerts/overlay.py`
- Daemon main loop + CLI (Task 11) — `claude_alerts/daemon.py`, `claude_alerts/__main__.py`
- E2E test (Task 12) — `tests/test_e2e_xvfb.py`
- systemd unit + README (Task 13) — `scripts/claude-alerts.service`, `README.md`

**Manual smoke test before declaring victory:** open two real Claude Code windows in gnome-terminal under your normal desktop session, run the daemon, and verify that one turning red while the other is green is immediately visible across both monitors.

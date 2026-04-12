# Overlay Binding and Stacking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bind sessions only to terminals where the user actively types prompts, and let the WM stack the border together with its terminal instead of floating it always-on-top.

**Architecture:** Move the `try_bind` trigger from `SessionStart` (which races focus) to `UserPromptSubmit` (which proves the user is in the claude terminal). Switch the overlay window from `override_redirect=1` (bypasses WM stacking) to a transient utility window with `WM_TRANSIENT_FOR` pointing at the bound terminal frame, plus EWMH/Motif hints to suppress decorations and taskbar entries. Delete the `VisibilityNotify` re-raise loop entirely.

**Tech Stack:** Python 3.10, python-xlib, pytest. Linux X11 (Mutter / GNOME on Pop_OS!).

**Spec:** `docs/superpowers/specs/2026-04-09-overlay-binding-and-stacking-design.md`

---

## File Structure

| File | Status | Responsibility |
|------|--------|----------------|
| `claude_alerts/daemon.py` | Modify | Wire events to binder/overlay; the only file whose binding-trigger logic changes. Drops the `VisibilityNotify` branch. |
| `claude_alerts/overlay.py` | Modify | `_OverlayWindow.__init__` drops `override_redirect`, sets transient/EWMH/Motif hints, takes a new `transient_for_window_id` parameter. `OverlayManager._sync_one` passes the bound frame id. `OverlayManager.raise_all` is deleted. |
| `claude_alerts/x11.py` | Modify | Drops the unused `_NET_WM_STATE_ABOVE` atom; interns five new atoms used by the overlay (`_NET_WM_WINDOW_TYPE`, `_NET_WM_WINDOW_TYPE_UTILITY`, `_NET_WM_STATE_SKIP_TASKBAR`, `_NET_WM_STATE_SKIP_PAGER`, `_MOTIF_WM_HINTS`). |
| `claude_alerts/binder.py` | No change | `try_bind` is unchanged; only its caller moves. |
| `claude_alerts/sessions.py` | No change | Already tracks `bound_window_id` and `client_window_id`. |
| `tests/test_daemon_binding_trigger.py` | Create | Unit tests for the new "bind only on `UserPromptSubmit`-for-unbound-session" rule. |
| `tests/test_overlay_smoke.py` | Modify | Adds tests for `_OverlayWindow.__init__`'s property writes (no `override_redirect`, no `VisibilityChangeMask`, transient_for set, UTILITY type set, skip taskbar/pager set, Motif decorations cleared). Updates the existing `_FakeOverlayWindow` to accept the new constructor parameter. Asserts `OverlayManager.raise_all` is gone. |
| `tests/test_daemon_threading.py` | Modify | Drops `_NET_WM_STATE_ABOVE` from its FakeX11 (matches the real class) and adds the new atom attributes the overlay needs. |

---

## Task 1: Stop binding on SessionStart, start binding on UserPromptSubmit

**Files:**
- Create: `tests/test_daemon_binding_trigger.py`
- Modify: `claude_alerts/daemon.py:44-49`

This task moves the `try_bind` trigger so the launcher terminal stops getting bound. The binder itself does not change.

- [ ] **Step 1.1: Write the new test file with the first failing test**

Create `tests/test_daemon_binding_trigger.py`:

```python
"""Verifies binding triggers only on UserPromptSubmit, never on SessionStart
or other events. This eliminates the focus race where the daemon would bind
the launcher terminal because the user had switched focus by the time the
SessionStart event was processed."""
from __future__ import annotations

import pytest

from claude_alerts.daemon import Daemon
from claude_alerts.config import Config
from claude_alerts.events import ClaudeEvent


class _FakeBinder:
    """Records every try_bind call so tests can assert on the trigger logic."""
    def __init__(self, store, x11):
        self.store = store
        self.x11 = x11
        self.calls: list[str] = []

    def try_bind(self, session_id: str) -> None:
        self.calls.append(session_id)

    def unbind_window(self, window_id: int) -> None:
        pass

    def pending_manual_binds(self) -> list[str]:
        return []


class _NoopX11:
    """Just enough X11Client surface for Daemon.__init__ to succeed without
    touching a real display. None of these methods are actually called by
    _on_event, which is what we test."""
    def __init__(self):
        self.screen = None

    def fileno(self):
        return -1
    def flush(self):
        pass
    def subscribe_root_substructure(self):
        pass
    def get_active_window_id(self):
        return None
    def get_wm_class(self, wid):
        return ""
    def get_geometry(self, wid):
        return None
    def get_visible_geometry(self, wid):
        return None
    def get_frame_window_id(self, wid):
        return None
    def pending_events(self):
        return 0
    def next_event(self):
        return None


@pytest.fixture
def daemon_with_fake_binder(monkeypatch, tmp_path):
    """Constructs a Daemon with the binder and X11 swapped for fakes.
    Returns (daemon, fake_binder)."""
    from claude_alerts import daemon as daemon_mod
    monkeypatch.setattr(daemon_mod, "X11Client", _NoopX11)
    monkeypatch.setattr(daemon_mod, "Binder", _FakeBinder)
    d = Daemon(events_dir=tmp_path / "events", config=Config())
    return d, d.binder


def _evt(event: str, session_id: str = "s1", t: float = 1.0) -> ClaudeEvent:
    return ClaudeEvent(
        event=event,
        session_id=session_id,
        cwd="/p",
        claude_pid=1,
        timestamp=t,
    )


def test_session_start_does_not_trigger_bind(daemon_with_fake_binder):
    """SessionStart used to trigger try_bind, which raced focus changes and
    sometimes bound the launcher terminal. It must NOT trigger binding now."""
    daemon, binder = daemon_with_fake_binder
    daemon._on_event(_evt("SessionStart"))
    assert binder.calls == []
    assert daemon.store.get("s1") is not None  # session still created
```

- [ ] **Step 1.2: Run the test and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_daemon_binding_trigger.py::test_session_start_does_not_trigger_bind -x`
Expected: FAIL with `assert ['s1'] == []` because `_on_event` currently calls `try_bind` for any new session.

- [ ] **Step 1.3: Update `Daemon._on_event` to bind only on UserPromptSubmit-for-unbound**

Replace `claude_alerts/daemon.py:44-49`:

```python
    def _on_event(self, evt: ClaudeEvent) -> None:
        """Runs on the main thread only — drained from the queue in run().

        Binding only fires on UserPromptSubmit, and only if the session is
        not yet bound. UserPromptSubmit is the one event that proves the
        user is currently focused on the claude terminal — there is no way
        to submit a prompt to claude from another window. Other events
        (PreToolUse, PostToolUse, Notification, Stop) fire while the user
        may be in any other window, so they would re-introduce the focus
        race they're meant to fix.
        """
        self.store.apply_event(evt)
        if evt.event == "UserPromptSubmit":
            session = self.store.get(evt.session_id)
            if session is not None and session.bound_window_id is None:
                self.binder.try_bind(evt.session_id)
```

- [ ] **Step 1.4: Run the test and watch it pass**

Run: `.venv/bin/python -m pytest tests/test_daemon_binding_trigger.py::test_session_start_does_not_trigger_bind -x`
Expected: PASS.

- [ ] **Step 1.5: Add the second failing test — UserPromptSubmit triggers bind for an unbound session**

Append to `tests/test_daemon_binding_trigger.py`:

```python
def test_user_prompt_submit_triggers_bind_for_unbound_session(daemon_with_fake_binder):
    """The first prompt to a session is the moment we know the user is in
    the right terminal. That's when binding fires."""
    daemon, binder = daemon_with_fake_binder
    daemon._on_event(_evt("SessionStart"))
    daemon._on_event(_evt("UserPromptSubmit", t=2.0))
    assert binder.calls == ["s1"]
```

- [ ] **Step 1.6: Run it and watch it pass (no implementation change needed)**

Run: `.venv/bin/python -m pytest tests/test_daemon_binding_trigger.py::test_user_prompt_submit_triggers_bind_for_unbound_session -x`
Expected: PASS — Step 1.3 already implements the trigger.

- [ ] **Step 1.7: Add the third failing test — already-bound sessions are not rebound**

Append to `tests/test_daemon_binding_trigger.py`:

```python
def test_user_prompt_submit_skipped_when_already_bound(daemon_with_fake_binder):
    """Once a session is bound, subsequent prompts must NOT re-trigger
    binding. The fake binder records each call, so re-binding would show
    up as a duplicate id in calls."""
    daemon, binder = daemon_with_fake_binder
    daemon._on_event(_evt("SessionStart"))
    daemon._on_event(_evt("UserPromptSubmit", t=2.0))
    daemon.store.set_bound_window("s1", 0xABCD, client_window_id=0xABCD)
    binder.calls.clear()
    daemon._on_event(_evt("UserPromptSubmit", t=3.0))
    assert binder.calls == []
```

- [ ] **Step 1.8: Run it and watch it pass**

Run: `.venv/bin/python -m pytest tests/test_daemon_binding_trigger.py::test_user_prompt_submit_skipped_when_already_bound -x`
Expected: PASS — the `bound_window_id is None` guard handles this.

- [ ] **Step 1.9: Add the fourth failing test — daemon-started-after-claude case**

Append to `tests/test_daemon_binding_trigger.py`:

```python
def test_user_prompt_submit_creates_session_for_unknown_id(daemon_with_fake_binder):
    """If the daemon starts after a claude session is already running, the
    very first event we see for that session might be UserPromptSubmit
    (no SessionStart). The session must be created on the fly AND bound."""
    daemon, binder = daemon_with_fake_binder
    daemon._on_event(_evt("UserPromptSubmit", session_id="newbie"))
    assert daemon.store.get("newbie") is not None
    assert binder.calls == ["newbie"]
```

- [ ] **Step 1.10: Run it and watch it pass**

Run: `.venv/bin/python -m pytest tests/test_daemon_binding_trigger.py::test_user_prompt_submit_creates_session_for_unknown_id -x`
Expected: PASS — `apply_event` creates unknown sessions automatically (`SessionStore.apply_event` creates a new `Session` if not present).

- [ ] **Step 1.11: Add the negative tests for the four other event types**

Append to `tests/test_daemon_binding_trigger.py`:

```python
@pytest.mark.parametrize("event_name", ["PreToolUse", "PostToolUse", "Notification", "Stop"])
def test_non_prompt_events_never_trigger_bind(daemon_with_fake_binder, event_name):
    """These events fire while the user may be in any other window
    (claude is grinding away on a tool, or notifying the user from the
    background). Using them as a binding trigger would reintroduce the
    focus race we are eliminating."""
    daemon, binder = daemon_with_fake_binder
    daemon._on_event(_evt("SessionStart"))
    daemon._on_event(_evt(event_name, t=2.0))
    assert binder.calls == []
```

- [ ] **Step 1.12: Run the parametrized test and watch all four cases pass**

Run: `.venv/bin/python -m pytest tests/test_daemon_binding_trigger.py::test_non_prompt_events_never_trigger_bind -v`
Expected: PASS for all four parameters.

- [ ] **Step 1.13: Run the whole new test file**

Run: `.venv/bin/python -m pytest tests/test_daemon_binding_trigger.py -v`
Expected: 8 passed (1 + 1 + 1 + 1 + 4 parametrized).

- [ ] **Step 1.14: Run the existing daemon-threading test to confirm no regression**

Run: `.venv/bin/python -m pytest tests/test_daemon_threading.py -v`
Expected: PASS. (`get_active_window_id` returns None in that fixture, so the binding-trigger change is invisible to it.)

- [ ] **Step 1.15: Commit**

```bash
git add tests/test_daemon_binding_trigger.py claude_alerts/daemon.py
git commit -m "$(cat <<'EOF'
fix(daemon): bind on UserPromptSubmit only, not SessionStart

SessionStart is processed asynchronously through a queue, so by the
time the daemon reads _NET_ACTIVE_WINDOW the user has often switched
focus away from the claude terminal — the launcher terminal then gets
bound by mistake. UserPromptSubmit is the only event that proves the
user is currently focused on the claude terminal, since you can't
submit a prompt to claude from anywhere else. Bind there instead, and
only when the session is still unbound, so existing sessions get
adopted on their next prompt without re-binding sessions that are
already correctly attached.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Add the new X11 atoms and drop the unused one

**Files:**
- Modify: `claude_alerts/x11.py:62-65` (atom interning in `X11Client.__init__`)
- Modify: `tests/test_daemon_threading.py:29-32` (FakeX11 attributes)

This is a small scaffolding task that prepares the atoms `_OverlayWindow.__init__` will use in Task 3. No standalone behavior to test — atom interning is a no-op until something writes a property.

- [ ] **Step 2.1: Update `X11Client.__init__` to intern the new atoms and drop the unused one**

Replace `claude_alerts/x11.py:62-65`:

```python
        self._NET_ACTIVE_WINDOW = self.display.intern_atom("_NET_ACTIVE_WINDOW")
        self._NET_CLIENT_LIST = self.display.intern_atom("_NET_CLIENT_LIST")
        self._NET_WM_STATE = self.display.intern_atom("_NET_WM_STATE")
        # _NET_WM_STATE_ABOVE was used for the old override_redirect always-on-top
        # behavior; the overlay no longer needs it now that the WM handles stacking.
        self._GTK_FRAME_EXTENTS = self.display.intern_atom("_GTK_FRAME_EXTENTS")
        self._NET_WM_WINDOW_TYPE = self.display.intern_atom("_NET_WM_WINDOW_TYPE")
        self._NET_WM_WINDOW_TYPE_UTILITY = self.display.intern_atom("_NET_WM_WINDOW_TYPE_UTILITY")
        self._NET_WM_STATE_SKIP_TASKBAR = self.display.intern_atom("_NET_WM_STATE_SKIP_TASKBAR")
        self._NET_WM_STATE_SKIP_PAGER = self.display.intern_atom("_NET_WM_STATE_SKIP_PAGER")
        self._MOTIF_WM_HINTS = self.display.intern_atom("_MOTIF_WM_HINTS")
```

(Note: this removes the `_NET_WM_STATE_ABOVE = self.display.intern_atom(...)` line from earlier in the constructor. Keep the existing `_GTK_FRAME_EXTENTS` line if the prior fix already added it; if not, this snippet adds it.)

- [ ] **Step 2.2: Update `tests/test_daemon_threading.py` FakeX11 to match the real class**

Replace `tests/test_daemon_threading.py:29-32`:

```python
            self._NET_ACTIVE_WINDOW = 0
            self._NET_CLIENT_LIST = 0
            self._NET_WM_STATE = 0
            self._GTK_FRAME_EXTENTS = 0
            self._NET_WM_WINDOW_TYPE = 0
            self._NET_WM_WINDOW_TYPE_UTILITY = 0
            self._NET_WM_STATE_SKIP_TASKBAR = 0
            self._NET_WM_STATE_SKIP_PAGER = 0
            self._MOTIF_WM_HINTS = 0
```

- [ ] **Step 2.3: Run the threading test to confirm the fake still satisfies the daemon**

Run: `.venv/bin/python -m pytest tests/test_daemon_threading.py -v`
Expected: PASS.

- [ ] **Step 2.4: Run the full suite for a sanity check**

Run: `.venv/bin/python -m pytest -q`
Expected: all green except the skipped Xvfb test.

- [ ] **Step 2.5: Commit**

```bash
git add claude_alerts/x11.py tests/test_daemon_threading.py
git commit -m "$(cat <<'EOF'
refactor(x11): intern overlay-stacking atoms, drop unused _NET_WM_STATE_ABOVE

Prepares the atoms _OverlayWindow will need to set the WM_TYPE,
SKIP_TASKBAR/PAGER, and Motif decoration hints. _NET_WM_STATE_ABOVE
was only used by the old always-on-top behavior and is unused now.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Rewrite `_OverlayWindow.__init__` to be WM-managed and transient

**Files:**
- Modify: `claude_alerts/overlay.py:31-45` (`_OverlayWindow.__init__`)
- Modify: `tests/test_overlay_smoke.py` (add real-constructor tests + fakes)

This is the largest task. The constructor signature gains a `transient_for_window_id: int` parameter, drops `override_redirect=1`, drops `VisibilityChangeMask`, and writes five properties on the new window.

- [ ] **Step 3.1: Add the test fakes that capture `change_property` / `set_wm_transient_for` / `create_window` calls**

Append to `tests/test_overlay_smoke.py` (after the existing fakes, before the geometry-source tests):

```python
# --- Real-constructor fixture ---------------------------------------------
# These fakes are used to drive the REAL _OverlayWindow.__init__, not the
# stubbed _FakeOverlayWindow above. They record every X11 call the
# constructor makes so we can assert on them.

class _FakeWindow:
    def __init__(self):
        self.property_writes: list[tuple] = []  # (atom, type_atom, format, data)
        self.shape_calls: list = []
        self.mapped: bool = False

    def change_property(self, prop_atom, type_atom, fmt, data, mode=0):
        self.property_writes.append((prop_atom, type_atom, fmt, list(data)))

    def shape_rectangles(self, *args, **kwargs):
        self.shape_calls.append((args, kwargs))

    def map(self):
        self.mapped = True

    def configure(self, **kwargs):
        pass

    def change_attributes(self, **kwargs):
        pass

    def clear_area(self, *args, **kwargs):
        pass

    def destroy(self):
        pass


class _FakeRoot:
    def __init__(self):
        self.last_create_args: tuple = ()
        self.last_create_kwargs: dict = {}
        self.created_window: _FakeWindow | None = None

    def create_window(self, *args, **kwargs):
        self.last_create_args = args
        self.last_create_kwargs = kwargs
        self.created_window = _FakeWindow()
        return self.created_window


class _FakeScreen:
    def __init__(self):
        self.root = _FakeRoot()


class _FakeX11ForCtor:
    """Just enough X11Client surface for _OverlayWindow.__init__ to run."""
    def __init__(self):
        self.screen = _FakeScreen()
        # Distinguishable atom ids — the constructor will pass these to
        # change_property and we assert against them by identity.
        self._NET_WM_WINDOW_TYPE = 1001
        self._NET_WM_WINDOW_TYPE_UTILITY = 1002
        self._NET_WM_STATE = 1003
        self._NET_WM_STATE_SKIP_TASKBAR = 1004
        self._NET_WM_STATE_SKIP_PAGER = 1005
        self._MOTIF_WM_HINTS = 1006

    def flush(self):
        pass


def _build_real_overlay_window(transient_for=0xFF22):
    """Instantiate the REAL _OverlayWindow with all-fake X11. Returns
    (the_overlay_window_instance, the_fake_root, the_fake_window)."""
    from claude_alerts.overlay import _OverlayWindow
    fake_x11 = _FakeX11ForCtor()
    geo = Geometry(x=10, y=20, width=400, height=300)
    ow = _OverlayWindow(
        fake_x11, geo, color_pixel=0xff0000, thickness=4,
        transient_for_window_id=transient_for,
    )
    return ow, fake_x11.screen.root, fake_x11.screen.root.created_window
```

- [ ] **Step 3.2: Add the first real-constructor test (override_redirect must be gone)**

Append to `tests/test_overlay_smoke.py`:

```python
def test_overlay_window_is_not_override_redirect():
    """The overlay must NOT use override_redirect=1 anymore — that's what
    made it bypass WM stacking and float above other windows."""
    _, root, _ = _build_real_overlay_window()
    # The constructor passes either positional or keyword args; we accept
    # either, but override_redirect must not be set to 1.
    assert root.last_create_kwargs.get("override_redirect", 0) == 0
```

- [ ] **Step 3.3: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_overlay_smoke.py::test_overlay_window_is_not_override_redirect -x`
Expected: FAIL — current code passes `override_redirect=1`. The failure may also be a `TypeError: __init__() got an unexpected keyword argument 'transient_for_window_id'` since the constructor doesn't accept that parameter yet. Either failure is fine; both are fixed in the next step.

- [ ] **Step 3.4: Rewrite `_OverlayWindow.__init__` to drop override_redirect, drop VisibilityChangeMask, accept transient_for_window_id, and set the five properties**

Replace `claude_alerts/overlay.py:28-44`:

```python
class _OverlayWindow:
    """One overlay window tracking one terminal."""

    def __init__(
        self,
        x11: X11Client,
        target_geo: Geometry,
        color_pixel: int,
        thickness: int,
        transient_for_window_id: int,
    ) -> None:
        self.x11 = x11
        self.thickness = thickness
        self.color_pixel = color_pixel
        self.win = x11.screen.root.create_window(
            target_geo.x, target_geo.y, target_geo.width, target_geo.height, 0,
            X.CopyFromParent, X.InputOutput, X.CopyFromParent,
            background_pixel=color_pixel,
            event_mask=X.ExposureMask,
        )
        self._set_wm_hints(transient_for_window_id)
        self._apply_shape(target_geo)
        self.win.map()
        x11.flush()

    def _set_wm_hints(self, transient_for_window_id: int) -> None:
        """Configure the WM to stack this overlay together with its terminal
        (transient_for) as a chrome window with no decorations or taskbar
        entry. The combination is belt-and-suspenders so the overlay
        renders correctly across Mutter, KWin, and other WMs:
            - WM_TRANSIENT_FOR  — load-bearing for stacking
            - WM_TYPE = UTILITY — normal stacking, secondary window
            - SKIP_TASKBAR/PAGER— stay out of taskbars and alt-tab
            - Motif decorations=0 — no titlebar even if EWMH is ignored

        All four use change_property uniformly so the test fixture can
        capture them with a single recording method, and so we don't
        depend on python-xlib's set_wm_transient_for convenience helper
        (which expects a window resource object, not a bare id).
        """
        from Xlib import Xatom
        try:
            self.win.change_property(
                Xatom.WM_TRANSIENT_FOR, Xatom.WINDOW, 32,
                [transient_for_window_id],
            )
            self.win.change_property(
                self.x11._NET_WM_WINDOW_TYPE, Xatom.ATOM, 32,
                [self.x11._NET_WM_WINDOW_TYPE_UTILITY],
            )
            self.win.change_property(
                self.x11._NET_WM_STATE, Xatom.ATOM, 32,
                [
                    self.x11._NET_WM_STATE_SKIP_TASKBAR,
                    self.x11._NET_WM_STATE_SKIP_PAGER,
                ],
            )
            # _MOTIF_WM_HINTS layout is 5 CARDINAL[32]:
            #   flags, functions, decorations, input_mode, status
            # flags=2 sets the "decorations field is meaningful" bit;
            # decorations=0 means no titlebar / no border.
            self.win.change_property(
                self.x11._MOTIF_WM_HINTS, self.x11._MOTIF_WM_HINTS, 32,
                [2, 0, 0, 0, 0],
            )
        except Exception as e:
            log.debug("setting overlay WM hints failed: %s", e)
```

(The old `__init__` body up to and including `x11.flush()` is replaced wholesale. The `_apply_shape`, `update_geometry`, `set_color`, `raise_above`, `destroy` methods stay as they were.)

- [ ] **Step 3.5: Update `OverlayManager._sync_one` to pass `transient_for_window_id`**

Replace the `_OverlayWindow(...)` construction in `claude_alerts/overlay.py:_sync_one`:

```python
        if existing is None:
            self._overlays[session.session_id] = _OverlayWindow(
                self.x11, geo, color_pixel, self.config.border_thickness_px,
                transient_for_window_id=session.bound_window_id,
            )
```

(`session.bound_window_id` is the FRAME id, which is exactly what `WM_TRANSIENT_FOR` should point at — the WM stacks transients with their parent frame.)

- [ ] **Step 3.6: Update `_FakeOverlayWindow` in `tests/test_overlay_smoke.py` to accept the new keyword**

Replace the existing `_FakeOverlayWindow.__init__` in `tests/test_overlay_smoke.py`:

```python
    def __init__(self, x11, geo, color_pixel, thickness, transient_for_window_id=None):
        self.geo = geo
        self.color_pixel = color_pixel
        self.thickness = thickness
        self.transient_for_window_id = transient_for_window_id
        self.update_calls: list[Geometry] = []
        self.destroyed = False
        _FakeOverlayWindow.instances.append(self)
```

- [ ] **Step 3.7: Run the override_redirect test and the broader overlay suite**

Run: `.venv/bin/python -m pytest tests/test_overlay_smoke.py -v`
Expected: `test_overlay_window_is_not_override_redirect` PASSES; the older `test_overlay_creates_window_at_visible_client_geometry` etc. continue to PASS (because `_FakeOverlayWindow` now accepts the new kwarg).

- [ ] **Step 3.8: Add the `event_mask` test**

Append to `tests/test_overlay_smoke.py`:

```python
def test_overlay_window_does_not_request_visibility_events():
    """We no longer re-raise on VisibilityNotify, so we don't need to
    receive VisibilityChangeMask events at all. Subscribing to them is a
    soft signal that the always-on-top loop is still wired up."""
    from Xlib import X as Xconst
    _, root, _ = _build_real_overlay_window()
    mask = root.last_create_kwargs.get("event_mask", 0)
    assert mask & Xconst.VisibilityChangeMask == 0
```

- [ ] **Step 3.9: Run it and watch it pass**

Run: `.venv/bin/python -m pytest tests/test_overlay_smoke.py::test_overlay_window_does_not_request_visibility_events -x`
Expected: PASS — Step 3.4 already replaced `event_mask` with just `ExposureMask`.

- [ ] **Step 3.10: Add the `WM_TRANSIENT_FOR` test**

Append to `tests/test_overlay_smoke.py`:

```python
def test_overlay_sets_transient_for_terminal_frame():
    """WM_TRANSIENT_FOR is what tells the WM 'stack me with this window'.
    Without it, the overlay has its own independent stack position and
    doesn't follow the terminal."""
    from Xlib import Xatom
    _, _, fake_win = _build_real_overlay_window(transient_for=0xFF22)
    transient_writes = [
        w for w in fake_win.property_writes
        if w[0] == Xatom.WM_TRANSIENT_FOR  # builtin atom id 68
    ]
    assert len(transient_writes) == 1
    _, type_atom, _, data = transient_writes[0]
    assert type_atom == Xatom.WINDOW
    assert data == [0xFF22]
```

- [ ] **Step 3.11: Run it and watch it pass**

Run: `.venv/bin/python -m pytest tests/test_overlay_smoke.py::test_overlay_sets_transient_for_terminal_frame -x`
Expected: PASS.

- [ ] **Step 3.12: Add the `_NET_WM_WINDOW_TYPE = UTILITY` test**

Append to `tests/test_overlay_smoke.py`:

```python
def test_overlay_sets_utility_window_type():
    """The overlay must register as _NET_WM_WINDOW_TYPE_UTILITY (normal
    stacking, secondary window). It must NOT register as
    _NET_WM_WINDOW_TYPE_DOCK, which per EWMH is always-on-top — exactly
    the behavior we are removing."""
    _, _, fake_win = _build_real_overlay_window()
    type_writes = [
        w for w in fake_win.property_writes
        if w[0] == 1001  # _NET_WM_WINDOW_TYPE atom from _FakeX11ForCtor
    ]
    assert len(type_writes) == 1
    _, _, _, data = type_writes[0]
    assert data == [1002]  # _NET_WM_WINDOW_TYPE_UTILITY atom
```

- [ ] **Step 3.13: Run it and watch it pass**

Run: `.venv/bin/python -m pytest tests/test_overlay_smoke.py::test_overlay_sets_utility_window_type -x`
Expected: PASS.

- [ ] **Step 3.14: Add the SKIP_TASKBAR / SKIP_PAGER test**

Append to `tests/test_overlay_smoke.py`:

```python
def test_overlay_sets_skip_taskbar_and_pager():
    """Don't appear in taskbars or alt-tab lists."""
    _, _, fake_win = _build_real_overlay_window()
    state_writes = [
        w for w in fake_win.property_writes
        if w[0] == 1003  # _NET_WM_STATE atom from _FakeX11ForCtor
    ]
    assert len(state_writes) == 1
    _, _, _, data = state_writes[0]
    assert 1004 in data  # SKIP_TASKBAR
    assert 1005 in data  # SKIP_PAGER
```

- [ ] **Step 3.15: Run it and watch it pass**

Run: `.venv/bin/python -m pytest tests/test_overlay_smoke.py::test_overlay_sets_skip_taskbar_and_pager -x`
Expected: PASS.

- [ ] **Step 3.16: Add the Motif decorations=0 test**

Append to `tests/test_overlay_smoke.py`:

```python
def test_overlay_sets_motif_no_decorations():
    """_MOTIF_WM_HINTS layout: [flags, functions, decorations, input_mode, status].
    flags=2 enables the decorations field; decorations=0 means no titlebar
    / no border. This is the most reliable cross-WM way to suppress chrome
    on a non-override-redirect window."""
    _, _, fake_win = _build_real_overlay_window()
    motif_writes = [
        w for w in fake_win.property_writes
        if w[0] == 1006  # _MOTIF_WM_HINTS atom from _FakeX11ForCtor
    ]
    assert len(motif_writes) == 1
    _, _, _, data = motif_writes[0]
    assert len(data) == 5
    assert data[0] == 2  # flags = decorations bit
    assert data[2] == 0  # decorations = none
```

- [ ] **Step 3.17: Run it and watch it pass**

Run: `.venv/bin/python -m pytest tests/test_overlay_smoke.py::test_overlay_sets_motif_no_decorations -x`
Expected: PASS.

- [ ] **Step 3.18: Run all overlay tests together for a sanity check**

Run: `.venv/bin/python -m pytest tests/test_overlay_smoke.py -v`
Expected: all tests PASS (the original geometry tests + the six new constructor tests).

- [ ] **Step 3.19: Commit**

```bash
git add claude_alerts/overlay.py tests/test_overlay_smoke.py
git commit -m "$(cat <<'EOF'
fix(overlay): drop override_redirect, become a transient utility window

The overlay used override_redirect=1 to bypass WM stacking entirely,
which made it always-on-top: it floated over any window placed in
front of the bound terminal. It now goes through the WM as a normal
client window, with WM_TRANSIENT_FOR pointing at the terminal frame
so the WM keeps the two windows stacked together. EWMH/Motif hints
ask for no decorations and no taskbar entry:
  - WM_TRANSIENT_FOR  — load-bearing for stacking
  - _NET_WM_WINDOW_TYPE = UTILITY — normal stacking, secondary window
  - _NET_WM_STATE += SKIP_TASKBAR, SKIP_PAGER
  - _MOTIF_WM_HINTS decorations=0
The constructor takes a new transient_for_window_id parameter; the
manager passes the bound frame id (which is exactly what we want the
WM to attach the overlay to).

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Delete `OverlayManager.raise_all` and the `VisibilityNotify` daemon branch

**Files:**
- Modify: `claude_alerts/overlay.py` (delete `raise_all` and `_OverlayWindow.raise_above`)
- Modify: `claude_alerts/daemon.py:94-106` (delete the `VisibilityNotify` branch)
- Modify: `tests/test_overlay_smoke.py` (add deletion-check tests)

The previous task replaced the constructor logic that drove always-on-top, but the `raise_all` method and its `VisibilityNotify` plumbing are still wired up and would re-introduce the bug if anything ever called them. Delete them and pin the deletion with tests.

- [ ] **Step 4.1: Add the failing deletion-check test for `OverlayManager.raise_all`**

Append to `tests/test_overlay_smoke.py`:

```python
def test_overlay_manager_has_no_raise_all_method():
    """raise_all was the always-on-top loop. It must not exist anymore —
    if it does, something will start calling it again and the overlay
    will float over other windows."""
    from claude_alerts.overlay import OverlayManager
    assert not hasattr(OverlayManager, "raise_all")
```

- [ ] **Step 4.2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_overlay_smoke.py::test_overlay_manager_has_no_raise_all_method -x`
Expected: FAIL — the method still exists.

- [ ] **Step 4.3: Delete `OverlayManager.raise_all` and `_OverlayWindow.raise_above`**

In `claude_alerts/overlay.py`, delete this method from `_OverlayWindow`:

```python
    def raise_above(self) -> None:
        self.win.configure(stack_mode=X.Above)
        self.x11.flush()
```

And delete this method from `OverlayManager`:

```python
    def raise_all(self) -> None:
        """Re-raise every overlay above the stack. Called on VisibilityNotify."""
        for ov in self._overlays.values():
            ov.raise_above()
```

- [ ] **Step 4.4: Run the deletion-check test and watch it pass**

Run: `.venv/bin/python -m pytest tests/test_overlay_smoke.py::test_overlay_manager_has_no_raise_all_method -x`
Expected: PASS.

- [ ] **Step 4.5: Add the failing test for the daemon `VisibilityNotify` no-op**

Append to `tests/test_daemon_binding_trigger.py`:

```python
def test_visibility_notify_does_not_call_raise_all(daemon_with_fake_binder):
    """The daemon used to call self.overlay.raise_all() on VisibilityNotify.
    With the always-on-top loop removed, the branch must be gone — feeding
    a fake VisibilityNotify event must not crash and must not poke the
    overlay manager."""
    from Xlib import X
    daemon, _ = daemon_with_fake_binder

    # Patch the overlay manager so any unexpected method call is loud.
    poked: list[str] = []
    class _Tripwire:
        def __getattr__(self, name):
            def _f(*args, **kwargs):
                poked.append(name)
            return _f
    daemon.overlay = _Tripwire()

    # Build a minimal fake X event with type=VisibilityNotify.
    class _FakeEvt:
        type = X.VisibilityNotify
    daemon._handle_x_event(_FakeEvt())

    assert poked == [], f"daemon poked overlay on VisibilityNotify: {poked}"
```

- [ ] **Step 4.6: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_daemon_binding_trigger.py::test_visibility_notify_does_not_call_raise_all -x`
Expected: FAIL — `_handle_x_event` still has `elif et == X.VisibilityNotify: self.overlay.raise_all()`, so `poked` becomes `["raise_all"]`.

- [ ] **Step 4.7: Delete the `VisibilityNotify` branch from `Daemon._handle_x_event`**

Replace `claude_alerts/daemon.py:94-106`:

```python
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
        # VisibilityNotify is intentionally ignored — the overlay is now a
        # transient utility window and the WM handles its stacking. There is
        # no always-on-top re-raise loop anymore.
```

- [ ] **Step 4.8: Run the daemon visibility test and watch it pass**

Run: `.venv/bin/python -m pytest tests/test_daemon_binding_trigger.py::test_visibility_notify_does_not_call_raise_all -x`
Expected: PASS.

- [ ] **Step 4.9: Run the entire test suite and confirm no regressions**

Run: `.venv/bin/python -m pytest -q`
Expected: all green (skipped Xvfb test only).

- [ ] **Step 4.10: Commit**

```bash
git add claude_alerts/overlay.py claude_alerts/daemon.py tests/test_overlay_smoke.py tests/test_daemon_binding_trigger.py
git commit -m "$(cat <<'EOF'
fix(overlay): delete the always-on-top raise_all loop

raise_all and its VisibilityNotify trigger were the second half of the
always-on-top behavior. Now that the overlay is a WM-managed transient,
the WM handles its stacking and re-raising would defeat that. Pinned
with two deletion-check tests so the methods can't be silently
re-introduced.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Manual verification — restart the daemon and confirm the three behaviors

This is a manual checklist, not an automated task. The goal is to confirm the running daemon picks up the new code and that the three observable issues from the spec are resolved.

- [ ] **Step 5.1: Stop the current daemon**

Find the running daemon's PID (`pgrep -f claude-alerts`), then `kill <pid>`. If a systemd user unit is managing it, `systemctl --user stop claude-alerts.service`.

- [ ] **Step 5.2: Reinstall the package into the venv**

Run: `cd /home/abhishek/claude-alerts && .venv/bin/pip install -e . --quiet`
Expected: no errors. Confirms the new code is on disk for the installed entry point.

- [ ] **Step 5.3: Start a fresh daemon**

Either restart the systemd user unit or run `claude-alerts &` from a terminal. Watch the log at `~/.local/state/claude-alerts/daemon.log` (or stderr) for `daemon running`.

- [ ] **Step 5.4: Verify Issue B (wrong terminal) is fixed**

Open a fresh terminal window. Run `claude` in it. Do NOT type a prompt yet. Switch back to the daemon launcher terminal. Confirm: NO border has appeared on either terminal.

- [ ] **Step 5.5: Trigger the bind**

Switch back to the claude terminal. Type any prompt. Confirm: a border appears on the claude terminal in the WAITING/WORKING color, hugging the visible window edge with no gap. The launcher terminal still has no border.

- [ ] **Step 5.6: Verify Issue C (always-on-top) is fixed**

Place another window (text editor, file manager, anything) in front of the claude terminal so it covers part of the border. Confirm: the covering window is unobstructed — the colored border does NOT paint over it. Bring the claude terminal back to the front. Confirm: the border returns with it.

- [ ] **Step 5.7: Verify Issue 3 (gap) is fixed**

While the claude terminal is in front, look closely at the four edges. Confirm: the colored band sits flush against the visible window edge with no shadow padding. If a gap remains, it is a separate investigation; capture a screenshot and the output of `xprop -id $(xdotool getactivewindow) _GTK_FRAME_EXTENTS _NET_FRAME_EXTENTS` for diagnosis.

- [ ] **Step 5.8: Verify the existing-session adoption case**

Without restarting the daemon, leave the claude session running. Open a SECOND fresh terminal, run `claude`, immediately switch to the launcher terminal, then come back and submit a prompt in the second claude terminal. Confirm: the border appears on the second terminal (not the launcher) the moment you submit the prompt.

---

## Self-review checklist (run after writing the plan, before saving)

1. **Spec coverage:**
   - Issue B (wrong-terminal binding) → Task 1 ✓
   - Issue C (always-on-top removal) → Tasks 2, 3, 4 ✓
   - Issue 3 (lingering gap) → Task 5 (manual restart resolves the stale-daemon hypothesis) ✓
   - Spec test plan section → Tasks 1, 3, 4 ✓
   - Manual verification section → Task 5 ✓
2. **Placeholder scan:** no TBDs, no "implement appropriate error handling", no "similar to Task N" — every step has either code or an exact command. ✓
3. **Type consistency:**
   - `transient_for_window_id` parameter name is identical in `_OverlayWindow.__init__`, `OverlayManager._sync_one`, and `_FakeOverlayWindow.__init__`. ✓
   - Atom names (`_NET_WM_WINDOW_TYPE`, `_NET_WM_WINDOW_TYPE_UTILITY`, `_NET_WM_STATE_SKIP_TASKBAR`, `_NET_WM_STATE_SKIP_PAGER`, `_MOTIF_WM_HINTS`) match between Task 2 (atom interning), Task 3 (property writes), and Task 3 tests (atom-id assertions). ✓
   - The fake atom ids in `_FakeX11ForCtor` (1001–1006) line up with the assertions in the tests (`1001` = `_NET_WM_WINDOW_TYPE`, `1002` = `..._UTILITY`, `1003` = `_NET_WM_STATE`, `1004` = `..._SKIP_TASKBAR`, `1005` = `..._SKIP_PAGER`, `1006` = `_MOTIF_WM_HINTS`). ✓

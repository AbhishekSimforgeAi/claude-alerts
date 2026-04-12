# Overlay binding and stacking redesign

**Date:** 2026-04-09
**Status:** Approved
**Owner:** Abhishek Shinde

## Problem

Three observable issues with the running overlay daemon:

1. **Wrong terminal gets the border.** The launcher terminal — the one running the `claude-alerts` daemon — sometimes gets a colored border, but the user only wants borders on terminals where a `claude` session is actually running. Root cause: `try_bind` is triggered on `SessionStart`, which is queued and processed asynchronously. By the time the daemon reads `_NET_ACTIVE_WINDOW`, the user has often switched focus, so the heuristic binds the wrong terminal. On gnome-terminal every tab/window shares one server PID (`gnome-terminal-server`), so PPID-walking from `claude_pid` cannot disambiguate either.

2. **Border floats above other windows.** The overlay window is created with `override_redirect=1` (bypassing WM stacking) and re-raises itself on every `VisibilityNotify`. This makes it always-on-top: when another window is placed in front of the bound terminal, the colored border still paints over the covering window. The user wants the border to stack naturally with its terminal — covered when the terminal is covered, in front when the terminal is in front.

3. **Visible gap between border and terminal edge** (suspected stale daemon). The currently running daemon (PID 11480) was started before the prior `_GTK_FRAME_EXTENTS` fix landed on disk, so the gap visible in the user's screenshots is most likely the old code. A daemon restart is part of this plan; if a gap remains afterwards it is a separate investigation.

## Goals

- Bind a session to its terminal exactly when the user demonstrably *is* in that terminal — eliminate the focus race.
- Stack the border naturally with its terminal under the WM, so it gets covered when the terminal gets covered.
- Keep the border free of WM chrome (no titlebar, no taskbar/alt-tab entry).
- Add no new runtime dependencies, no hook script changes, no installation steps.

## Non-goals

- Tracking *which* X11 window owns which `claude` PID via process tree walking. PPID is unreliable for `gnome-terminal-server` and not worth the complexity.
- Adding `xdotool` / `xprop` calls in the hook script.
- Re-architecting the binder. `try_bind` is fine; only its trigger needs to change.
- Fixing the `claude_pid=$$` quirk in `scripts/hooks/emit-event.sh` (the field actually holds the hook script's bash PID, not the claude PID — out of scope).
- Visual stacking tests under a real WM. Unit tests cover the surface that we control; visual behavior is verified manually after restart.

## Design

### 1. Bind on `UserPromptSubmit`, not `SessionStart`

The trigger for `try_bind` moves from "session is new" to "an event arrived that proves the user is currently typing in the terminal where claude is running." `UserPromptSubmit` is the only event that satisfies this property: the user must be focused on the claude terminal at the moment they submit a prompt — there is no way to submit a prompt to claude from another window.

`PreToolUse`, `PostToolUse`, `Notification`, and `Stop` are explicitly **not** valid binding triggers, because they fire while the user may be working in any other window.

`SessionStart` becomes a no-op for binding purposes — the session is created in the store as before, but no `try_bind` call is made.

#### Daemon change

`Daemon._on_event` (`claude_alerts/daemon.py`):

```python
def _on_event(self, evt: ClaudeEvent) -> None:
    self.store.apply_event(evt)
    if evt.event == "UserPromptSubmit":
        s = self.store.get(evt.session_id)
        if s is not None and s.bound_window_id is None:
            self.binder.try_bind(evt.session_id)
```

This handles three cases uniformly:

- **Brand-new session, first prompt:** session created by `apply_event`, then bound.
- **Daemon started after claude was already running, user submits next prompt:** session created on the fly by `apply_event`, then bound.
- **Already-bound session, subsequent prompts:** `bound_window_id` is set, second `if` branch is skipped.

The `is_new` short-circuit and the unconditional `try_bind` from the previous design are removed.

### 2. Drop always-on-top, let the WM stack the overlay with its terminal

The overlay window becomes a regular client window from the X server's perspective, marked as a transient of the bound terminal frame. The WM then stacks the overlay together with the terminal — when the terminal is raised, the overlay rides along; when the terminal is buried, the overlay goes with it. EWMH/Motif hints prevent the WM from adding decorations or putting the overlay in the taskbar/alt-tab list.

#### Overlay changes

`_OverlayWindow.__init__` (`claude_alerts/overlay.py`):

- Drop `override_redirect=1` from `create_window`. The window now goes through normal WM stacking.
- Drop `VisibilityChangeMask` from `event_mask`. We no longer react to visibility changes.
- After creation, set the following properties via `set_wm_transient_for` / `change_property`:
  - `WM_TRANSIENT_FOR` = the bound terminal's frame window id. This is the load-bearing hint: it tells the WM "stack this window together with that one." Mutter, KWin, and most other WMs respect this and keep the transient at-or-just-above its parent in stack order without us doing anything else. **Without this hint, the overlay would have its own independent stack position and would not follow the terminal.**
  - `_NET_WM_WINDOW_TYPE` = `_NET_WM_WINDOW_TYPE_UTILITY`. Per EWMH, utility windows are "small persistent secondary windows" with normal (not always-on-top) stacking. Crucially we do **not** use `_NET_WM_WINDOW_TYPE_DOCK` — DOCK windows are always-on-top per EWMH spec, which is exactly the behavior we are removing.
  - `_NET_WM_STATE` = `[_NET_WM_STATE_SKIP_TASKBAR, _NET_WM_STATE_SKIP_PAGER]` — keep the overlay out of taskbars and alt-tab lists.
  - `_MOTIF_WM_HINTS` with `flags=2` (decorations bit present) and `decorations=0` — no titlebar / no border even if the WM ignores the EWMH hints. This is the most reliable cross-WM way to suppress decorations on a non-override-redirect window.
- Do **not** set `_NET_WM_STATE_ABOVE`.
- Do **not** call `configure(stack_mode=X.Above)` after creation. Let the WM choose the stack position based on `WM_TRANSIENT_FOR`.
- The constructor signature gains a `transient_for_window_id: int` parameter so the manager can pass the bound frame id at creation time.

`OverlayManager.raise_all` is deleted entirely. Its only caller (`Daemon._handle_x_event`'s `VisibilityNotify` branch) is also deleted.

`X11Client._NET_WM_STATE_ABOVE` atom is deleted (now unused). New atoms added to `X11Client.__init__` for the property writes: `_NET_WM_WINDOW_TYPE`, `_NET_WM_WINDOW_TYPE_UTILITY`, `_NET_WM_STATE_SKIP_TASKBAR`, `_NET_WM_STATE_SKIP_PAGER`, `_MOTIF_WM_HINTS`. (`_NET_WM_STATE` already exists.)

#### Daemon event-loop change

`Daemon._handle_x_event` (`claude_alerts/daemon.py`) drops the `X.VisibilityNotify` branch. The atom and the import stay only if needed elsewhere.

### 3. Daemon restart as a verification step

The plan's final step stops the running daemon (`kill 11480` or equivalent), reinstalls the package, starts a fresh daemon. This both deploys the changes and resolves Issue 3 (the suspected stale-daemon gap).

## Components

The change touches four files; each unit retains a single, clear purpose:

- **`claude_alerts/daemon.py`** — owns the wiring between events, session store, binder, overlay. The change is isolated to two methods (`_on_event`, `_handle_x_event`).
- **`claude_alerts/overlay.py`** — owns the window-creation primitives and the per-session overlay lifecycle. The change is isolated to `_OverlayWindow.__init__` (drops `override_redirect`, sets transient/EWMH/Motif hints, takes a new `transient_for_window_id` argument), `OverlayManager._sync_one` (passes the bound frame id as the new argument), and the deletion of `OverlayManager.raise_all`.
- **`claude_alerts/binder.py`** — unchanged. `try_bind` is still the right primitive; only its caller moves.
- **`claude_alerts/sessions.py`** — unchanged. Already tracks `bound_window_id` and `client_window_id`.
- **`claude_alerts/x11.py`** — drops the unused `_NET_WM_STATE_ABOVE` atom; adds atoms for the new property writes (`_NET_WM_WINDOW_TYPE`, `_NET_WM_WINDOW_TYPE_UTILITY`, `_NET_WM_STATE_SKIP_TASKBAR`, `_NET_WM_STATE_SKIP_PAGER`, `_MOTIF_WM_HINTS`).

No new modules. No new abstractions. Each unit can be understood and tested independently.

## Test plan

TDD throughout: failing test first, then minimal implementation, then green. Tests are organized by responsibility, not by file:

### Binding-trigger tests (new file: `tests/test_daemon_binding_trigger.py`)

Uses a fake `Binder` that records calls to `try_bind`, plus the real `SessionStore`. Spins up a `Daemon` (or its `_on_event` method) and feeds events directly without running the X11 loop.

- `test_session_start_does_not_trigger_bind` — feed `SessionStart`; assert `try_bind` count is 0 and the session exists in the store.
- `test_user_prompt_submit_triggers_bind_for_unbound_session` — feed `SessionStart`, then `UserPromptSubmit`; assert exactly one `try_bind` call with the right session id.
- `test_user_prompt_submit_skipped_when_already_bound` — pre-populate the session with a bound window; feed `UserPromptSubmit`; assert no new `try_bind` call.
- `test_user_prompt_submit_creates_session_for_unknown_id` — covers the daemon-started-after-claude case. Feed only `UserPromptSubmit` for a never-seen session id; assert the session is created in the store AND `try_bind` is called once.
- `test_pre_tool_use_does_not_trigger_bind` — feed `PreToolUse` for an unbound session; assert no `try_bind` call. Same expectation for an unknown session id.
- `test_notification_does_not_trigger_bind` — same rationale.
- `test_post_tool_use_does_not_trigger_bind` — same rationale.
- `test_stop_does_not_trigger_bind` — same rationale.

### Overlay stacking tests (extend `tests/test_overlay_smoke.py`)

For the override-redirect and property assertions we use a fake `screen.root` whose `create_window` returns a fake window object that records all `change_property` and `set_wm_transient_for` calls. This lets us assert against the exact kwargs and property writes without touching X11.

- `test_overlay_window_is_not_override_redirect` — capture `create_window` kwargs; assert `override_redirect` is absent or 0.
- `test_overlay_window_does_not_request_visibility_events` — assert `VisibilityChangeMask` is not in the requested `event_mask`.
- `test_overlay_sets_transient_for_terminal_frame` — assert `set_wm_transient_for` (or the equivalent `change_property` on `WM_TRANSIENT_FOR`) was called with the bound terminal's frame window id passed at construction.
- `test_overlay_sets_utility_window_type` — assert `_NET_WM_WINDOW_TYPE` was set to `_NET_WM_WINDOW_TYPE_UTILITY` (and explicitly NOT `_NET_WM_WINDOW_TYPE_DOCK`).
- `test_overlay_sets_skip_taskbar_and_pager` — assert `_NET_WM_STATE` was set to a list containing both `_NET_WM_STATE_SKIP_TASKBAR` and `_NET_WM_STATE_SKIP_PAGER`.
- `test_overlay_sets_motif_no_decorations` — assert `_MOTIF_WM_HINTS` was set with the decorations bit explicitly cleared (`flags=2`, `decorations=0`).
- `test_overlay_manager_has_no_raise_all_method` — explicit deletion check: `assert not hasattr(OverlayManager, "raise_all")`.

### Daemon event-loop test (extend `tests/test_daemon_threading.py` or new test)

- `test_visibility_notify_is_a_noop` — feed a synthetic `VisibilityNotify` X event into `Daemon._handle_x_event`; assert it does not crash and does not call any overlay-manager method.

### Manual verification (post-implementation)

After all unit tests pass:

1. Stop the existing daemon: `kill 11480` (or whatever PID is current).
2. Reinstall: `pip install -e .` from the repo root in `.venv`.
3. Start daemon: `claude-alerts &` (or via the systemd user unit).
4. Open a fresh terminal, run `claude`, type a prompt.
5. Verify: border appears on the new terminal only, hugs the visible window edge with no gap, color matches state.
6. Cover the terminal with another window — verify the covering window is unobstructed.
7. Bring the terminal back forward — verify the border returns with it.
8. The launcher terminal must never receive a border for the duration of this test.

### Tests not included

- **Real-WM stacking behavior** — Xvfb has no WM, so neither does `tests/test_e2e_xvfb.py`. After this change, `override_redirect` is gone, so a window created under bare-Xvfb may not even map visibly. The e2e test will be adjusted to assert overlay creation/lifecycle (which is what it was actually testing), not visibility. If that adjustment turns out non-trivial, the test stays skipped and a comment notes why.
- **CSD frame-extents math** — already covered by the prior fix's `test_inset_by_frame_extents_*` cases.

## Error handling

No new failure modes are introduced. Existing failure handling is preserved:

- `try_bind` continues to enqueue for manual binding if the active window is not a terminal or if X11 calls fail.
- `change_property` calls in `_OverlayWindow.__init__` are wrapped in a `try/except (XError, ConnectionError)` and logged at debug level — failure to set hints is not fatal; the window is still usable, it just may have the wrong stack/decoration.
- The `visibility_notify_is_a_noop` test pins down that we don't crash if a `VisibilityNotify` arrives from the X server (e.g., racing with subscription teardown).

## Migration / rollout

- This is a development branch (`feat/initial-implementation`); no users besides the author. Roll forward with a daemon restart.
- The previous fixes (frame window id, client window id, `_GTK_FRAME_EXTENTS`) are still uncommitted in the working tree. They stay — this design layers on top of them.
- No data migration. The session store is in-memory only.

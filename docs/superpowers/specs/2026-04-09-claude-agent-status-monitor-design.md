# claude-alerts — Visual Status Monitor for Claude Code Agents

**Date:** 2026-04-09
**Status:** Design approved, ready for implementation planning

## Problem

Running multiple Claude Code agents in parallel is frustrating because an agent will silently stop and wait for permission or for the next prompt, and the user doesn't notice for minutes. There is no peripheral signal that an agent needs attention.

## Goal

A background tool that draws a colored border around each Claude agent's terminal window — **green when the agent is working, red when it is waiting on the user for any reason**. The user can glance at any of their monitors and instantly see which agents need them.

## Non-goals

- Wayland support. Target environment is X11.
- Terminals other than gnome-terminal. Target environment is Pop!_OS 22.04 default terminal.
- Sound or desktop notifications. Pure visual.
- Multi-user / shared installation.
- Distinguishing "waiting for permission" from "waiting for next prompt". Both are red.

## Target environment

- Pop!_OS 22.04
- X11
- gnome-terminal (gnome-terminal-server architecture: all windows hosted by a single server process)
- Multiple monitors
- Each Claude agent runs in its own gnome-terminal window, launched via `claude worktree`
- Python 3.10+

## States

Two states only:

| State | Color | Triggered by |
|---|---|---|
| working | green (`#22c55e`, configurable) | `UserPromptSubmit`, `PreToolUse`, `PostToolUse` hooks |
| waiting | red (`#ef4444`, configurable) | `Stop`, `Notification` hooks; also the initial state on `SessionStart` |

`UserPromptSubmit` is essential: it covers the gap between the moment you press Enter and the first tool call (which can be many seconds if the model thinks before acting), as well as turns that contain no tool calls at all. Without it, conversational replies would render as red for their entire duration.

## Architecture

A single Python process — `claude-alerts` — runs in the background under the user's session. It does three jobs:

1. Watches `~/.local/state/claude-alerts/events/` for new event files written by Claude Code hooks (via inotify).
2. Maintains an in-memory table of live sessions: `session_id → {cwd, claude_pid, status, bound_window_id, last_event_at}`.
3. Renders a thin colored border overlay X11 window around each bound terminal, updating color on state changes and position whenever the terminal moves or resizes.

The hooks themselves are trivial shell one-liners installed in `~/.claude/settings.json`. They write a JSON event file and exit. They have no dependency on whether the daemon is running — if the daemon is down, hooks still complete instantly and events accumulate on disk to be drained on next daemon start.

```
┌───────────────────────┐     writes JSON file      ┌─────────────────────┐
│  Claude Code hooks    │ ─────────────────────────▶│  events/ directory  │
│  (PreToolUse, Stop,   │                           │  (watched via       │
│   Notification, etc.) │                           │   inotify)          │
└───────────────────────┘                           └─────────┬───────────┘
                                                              │
                                                              ▼
                           ┌──────────────────────────────────────────────┐
                           │   claude-alerts daemon (single Python proc)  │
                           │  ┌────────────┐  ┌────────────┐  ┌────────┐  │
                           │  │ event loop │─▶│ session    │─▶│ X11    │  │
                           │  │ (inotify)  │  │ state      │  │ overlay│  │
                           │  └────────────┘  └────────────┘  │ render │  │
                           │         ▲                         └────────┘  │
                           │         │                              │      │
                           │    X11 events for                 draws on    │
                           │    window move/resize/close       each screen │
                           └──────────────────────────────────────────────┘
```

### Why one process, not two

At ~10 windows max there is no benefit to splitting daemon and renderer, and the renderer needs the session state anyway. Single process is simpler to start, stop, and debug.

### Why file-based events and not a socket

Hooks become one-line shell commands with zero error handling. The daemon can be restarted without re-connecting clients. `tail -f events/*` is a trivial debug tool.

### Why no threads

Everything runs on one event loop (asyncio or a hand-rolled select loop over inotify + the X11 socket). Renderer, ingester, and binder are all callbacks on the same loop. No locks, no races.

## Components

### 1. Hook scripts — `scripts/hooks/emit-event.sh`

One shell script, installed once via `~/.claude/settings.json`, wired to seven hook events: `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`, `Notification`, `SessionEnd`. The settings.json wires each event to the same script with the event name as an argument.

The script:
- Reads the hook's stdin JSON payload (Claude Code passes session info that way).
- Extracts `session_id`, `cwd`, `pid` with `jq`.
- Writes `~/.local/state/claude-alerts/events/<timestamp>-<session_id>.json.tmp` containing `{event, session_id, cwd, claude_pid, timestamp}`.
- `mv` to drop the `.tmp` extension (atomic on the same filesystem).
- Exits 0 unconditionally — never blocks Claude, never prints to the terminal.

### 2. Event ingester — `claude_alerts/ingester.py`

Watches `events/` with `inotify_simple` for `IN_MOVED_TO` events. On notification, parses the file, dispatches the event into the session store, deletes the file. On daemon startup, drains any backlog in `events/` first so that events written while the daemon was down are not lost. On `IN_Q_OVERFLOW`, falls back to a full directory rescan and logs a warning. Malformed event files are moved to `events/rejected/` with a logged reason.

### 3. Session store — `claude_alerts/sessions.py`

Pure data, no I/O. In-memory dict keyed by `session_id`. Each entry: `{cwd, claude_pid, status, bound_window_id, last_event_at}`. The status transitions are:

| Event | New status | Side effect |
|---|---|---|
| `SessionStart` | `waiting` | Trigger binder |
| `UserPromptSubmit` | `working` | — |
| `PreToolUse` | `working` | — |
| `PostToolUse` | `working` | — |
| `Stop` | `waiting` | — |
| `Notification` | `waiting` | If session unknown, create as orphan and trigger binder |
| `SessionEnd` | (removed) | Destroy overlay |

A 30-second cleanup sweep evicts sessions whose `last_event_at` is older than 5 minutes — guards against `kill -9`'d Claude processes that never fire `SessionEnd`.

The store exposes a single observer callback `on_change(session_id)` that the renderer subscribes to.

### 4. Window binder — `claude_alerts/binder.py`

On `SessionStart` (or orphan event), queries `_NET_ACTIVE_WINDOW`, then verifies the window's `WM_CLASS` looks like a terminal (`gnome-terminal-server` for the target environment, with a small allowlist for forward-compatibility). If the check passes, binds the session to that window ID.

If the check fails — wrong WM_CLASS, or no active window, or active window is the daemon's own click-prompt — pushes the session onto a `needs_manual_bind` queue and shows a small floating click-to-bind prompt window labeled with the worktree path. One click on the user's intended terminal window completes the binding.

Bindings are cached by `session_id` until `SessionEnd` or until the bound window emits `DestroyNotify`.

### 5. Overlay renderer — `claude_alerts/overlay.py`

For each bound session, maintains one X11 override-redirect window:
- Sized exactly to the target terminal's outer geometry.
- Solid colored border (default 4px, configurable).
- Transparent middle hole using the `XShape` extension so clicks pass through to the terminal underneath.
- `_NET_WM_STATE_ABOVE` set so it stays on top.
- Re-raises itself on `VisibilityNotify` if it detects occlusion.

The renderer subscribes to `SubstructureNotifyMask` on the root window, which delivers `ConfigureNotify` (move/resize) and `DestroyNotify` (closed) events for every top-level window. When a target terminal's `ConfigureNotify` arrives, the overlay updates its geometry in the same frame with `XMoveResizeWindow`. No polling loop. CPU is idle when nothing changes.

On `on_change(session_id)` from the session store, the renderer repaints just that session's overlay border pixmap from green to red or vice versa — one `XCopyArea` per border edge plus an `XFlush`.

## Data flow examples

### Claude asks for permission to run a tool

1. Claude Code fires the `Notification` hook. It spawns `scripts/hooks/emit-event.sh Notification` with the JSON payload on stdin.
2. Hook script runs (inside the claude process tree, a grandchild of `gnome-terminal-server`). It parses stdin with `jq`, writes `events/1712599823.123-abc123.json.tmp`, then `mv`s to drop the suffix. Exits 0.
3. Daemon's inotify watcher fires on `IN_MOVED_TO`. Reads the file, parses, deletes.
4. Ingester calls `sessions.apply_event(event)`. Session `abc123` is updated to `status = "waiting"`.
5. Session store calls `renderer.on_change("abc123")`. Renderer repaints the overlay from green to red. One `XFlush`.
6. User sees the red border within roughly 10–50ms of the hook firing.

### User drags a terminal to another monitor

1. Window manager moves the gnome-terminal window. X11 emits `ConfigureNotify` on root.
2. Renderer receives the event automatically (subscribed to `SubstructureNotifyMask`).
3. Renderer matches the reported window ID against its bound-window map, finds the matching session, calls `XMoveResizeWindow` on the overlay with the new geometry.

### User closes a terminal mid-tool-call

1. Window destroyed → `DestroyNotify` on root.
2. Renderer destroys the corresponding overlay, marks `bound_window_id = None` in the session.
3. Either `SessionEnd` fires shortly and removes the session, or the 5-minute idle sweep evicts it.

## Configuration

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

Defaults are baked in; the user only creates this file if they want to override.

## Failure handling

| Failure mode | Response |
|---|---|
| Daemon not running when hook fires | Hook still completes; events accumulate and are drained on next daemon start |
| Malformed event file | Moved to `events/rejected/` with logged reason; daemon continues |
| Active-window heuristic binds the wrong window | `WM_CLASS` guard catches it; falls through to click-to-bind queue |
| Bound terminal closed by user | `DestroyNotify` → overlay destroyed → session marked unbound; `SessionEnd` or idle sweep removes session |
| `gnome-terminal-server` crashes / restarts | All windows destroyed → all sessions evicted on next sweep |
| Overlay drawn behind terminal after raise-on-click | Overlay re-raises on `VisibilityNotify` |
| `inotify` queue overflow (`IN_Q_OVERFLOW`) | Full directory rescan + warning log |
| Daemon crashes | Systemd user unit restarts it; `.service` file shipped |
| `~/.claude/settings.json` does not exist or has other content | Install script does a JSON-aware merge that preserves existing settings |

Logging: single file at `~/.local/state/claude-alerts/daemon.log`, rotated at 10MB. Default level `INFO`. `CLAUDE_ALERTS_DEBUG=1` enables `DEBUG`.

## Testing

Three layers.

### Layer 1 — Unit tests

Pure-logic tests of `sessions.py`. Feed event sequences through `apply_event` and assert resulting state. Covers all seven event types, the orphan-Notification recovery path, the `SessionEnd` cleanup, and the idle sweep eviction. No display, no filesystem. Fast — sub-second to run all of them.

### Layer 2 — Component tests against fakes

- **Ingester** tested against a real temp directory with `inotify_simple` running, but the session store is replaced with a recording fake. Asserts that files dropped into the directory are parsed, dispatched, and deleted, including the malformed-file rejection path.
- **Binder** tested against a fake X11 client that returns scripted `_NET_ACTIVE_WINDOW`, scripted `WM_CLASS` lookups, scripted client list. Asserts the active-window heuristic, the WM_CLASS guard, and the manual-bind queue.

No display required.

### Layer 3 — End-to-end smoke against `Xvfb`

One test:
1. Boots a headless X server with `Xvfb :99`.
2. Spawns a fake "terminal" window with `WM_CLASS = gnome-terminal-server`.
3. Starts the real daemon pointed at a temp events dir.
4. Drops a synthetic `SessionStart` event with the fake terminal active.
5. Asserts an overlay window appears around the fake terminal with green border.
6. Drops a `Stop` event, asserts the border becomes red.
7. Moves the fake terminal, asserts the overlay follows within one frame.
8. Destroys the fake terminal, asserts the overlay disappears.

Slow-ish (~1 second) but exercises every real component end-to-end.

### Manual smoke test

Once before release: open two real Claude Code windows under gnome-terminal, run a tool that prompts for permission in one and finishes its turn in the other, verify both turn red within ~50ms and back to green when work resumes.

## Out of scope

- Wayland support
- Other terminal emulators
- Sound / desktop notifications
- Multi-state (3+) status visualization
- Per-screen layout configuration UI (positions are read from X11 automatically)
- Persistence of session state across daemon restarts (rebuilt lazily from event backlog and live X11 state)

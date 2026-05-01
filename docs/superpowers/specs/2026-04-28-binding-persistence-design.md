# Binding persistence across daemon restarts

**Date:** 2026-04-28
**Status:** Draft — pending review
**Owner:** Abhishek Shinde

## Problem

When the claude-alerts daemon restarts (manual restart, crash, machine wake-up), all in-memory state is lost. Sessions only re-bind to terminal windows on `UserPromptSubmit` (`daemon.py:53-56`), because that event is the only signal that proves the user is focused on the Claude terminal — you cannot submit a prompt from another window.

Consequence: after a restart, every Claude Code window that's mid-conversation (Claude is working, or sitting at a permission prompt, or paused at `Stop` waiting for the user to read the response) loses its border, and the border doesn't return until the user types a brand-new prompt in that window. For a user with several long-running Claude sessions across multiple terminals, this means a noticeable period of "no visual feedback anywhere" after every restart, and friction tapping each window individually to wake the binding back up.

This is not a regression from the v0.1.3 hook fix — it's a longstanding consequence of the bind-on-`UserPromptSubmit` design that the user only just hit because they had to restart the daemon to pick up the hook fix.

## Goals

- After a daemon restart, bindings for live windows are restored automatically; the user does not need to type in each terminal.
- Restored bindings paint the correct color immediately — i.e. session status (`WORKING`/`WAITING`), `last_event`, and `background_active` survive the restart along with the window IDs.
- Stale entries (window destroyed while daemon was down, X server restarted, etc.) are detected and dropped on load — no zombie overlays at addresses that no longer exist.
- No new hook events from Claude Code, no new runtime dependencies.

## Non-goals

- Cross-machine portability of binding state. A single user, single X display, single state file.
- Migrating in-flight tool-call timing. We persist *outcomes* (was the last event PreToolUse / Stop / PermissionRequest etc.), not pending operations.
- Repairing window-ID drift across X server restarts (logout / display reset). When window IDs are reissued by a new X server, persisted IDs no longer correspond to anything; we drop them rather than try to reassociate by WM_CLASS or PID, because the user's terminal grouping is not generally inferrable.
- Persisting unbound sessions. The point of persistence is to keep the *visual* across restarts, and unbound sessions have no visual.

## Design

### 1. Storage location and shape

Single JSON file: `$XDG_STATE_HOME/claude-alerts/sessions.json` (defaults to `~/.local/state/claude-alerts/sessions.json`, sibling of `daemon.log`).

Schema:

```json
{
  "version": 1,
  "saved_at": 1777357200.123,
  "sessions": [
    {
      "session_id": "5756986d-...",
      "cwd": "/home/u/proj",
      "claude_pid": 12345,
      "status": "working",
      "last_event_at": 1777357180.5,
      "last_event": "PreToolUse",
      "background_active": false,
      "bound_window_id": 40624604,
      "client_window_id": 40624604
    }
  ]
}
```

Only sessions with `bound_window_id != None` are persisted. Unbound sessions are visual no-ops and would just decay on the next restart anyway.

`version: 1` is a forward-compatibility marker — if the on-disk format is ever extended, older daemons can refuse to load `version > 1` rather than crash with a `KeyError`.

### 2. When to write

Persistence is a side effect of `SessionStore` mutations, not a periodic flush. Two reasons: (a) we want post-crash state to be at most one mutation behind reality, and (b) the rate of mutations is bounded by hook frequency (small).

Add a `persist()` callback hook to `SessionStore`, invoked at the end of `_notify`. The daemon wires it up to a `BindingPersister` whose `save()` method writes the file atomically (`tmp + rename`).

Throttle: at most one write per 200ms. The throttle is implemented with a "dirty" flag — `save()` sets dirty=True and either writes immediately (if no recent write) or schedules a single timer-driven write. This is to avoid hammering the disk during a burst of events (e.g. several PreToolUse / PostToolUse pairs in quick succession during a TodoWrite-heavy turn). The 200ms ceiling is well below human-perceivable lag and matches the existing overlay update cadence.

Failure to write is logged at WARNING and otherwise swallowed — disk full / permissions issues should not crash the daemon.

### 3. When and how to load

On `Daemon.run`, before subscribing to root substructure events:

1. Read `sessions.json`. If absent or malformed → start with empty store, log INFO.
2. For each persisted session entry:
   - Verify the bound window still exists by calling `x11.get_visible_geometry(client_window_id or bound_window_id)`. If `None`, drop the entry and log INFO.
   - Otherwise reconstruct a `Session` with all fields and insert it directly into the store (bypassing `apply_event`, which is for hook-driven mutations).
3. After all entries are loaded, run `OverlayManager._sync_one` on each restored session so overlays appear immediately.

Bypassing `apply_event` is deliberate: the persisted record already represents the correct post-event state, and replaying through the state machine would require fabricating events with synthetic timestamps. A dedicated `SessionStore.restore()` method makes the path explicit.

### 4. State reconstruction for the overlay

`OverlayManager._sync_one` already does the right thing given a `Session` with `bound_window_id`, `client_window_id`, `status`, `last_event`, and `background_active` set. Persisting all five fields (see schema in §1) means `_sync_one` produces the correct color on the restored session without any extra logic.

The single subtle point: `_sync_one` calls `_client_geometry`, which calls `x11.get_visible_geometry`. This is the same call we make during load to verify the window exists, so the restoration path naturally fails closed if the window was destroyed mid-restart.

### 5. Pruning live state

The existing idle-evict policy (`evict_idle`, 5-minute window for unbound sessions) already keeps the in-memory store small; the on-disk file inherits that bound. Bound sessions are never evicted — same as today.

A `bound_window_id` cleared at runtime (DestroyNotify → `binder.unbind_window` → `set_bound_window(None)`) fires `_notify`, which writes a new snapshot omitting the now-unbound session. So removal flows through the same path as any other mutation; no separate "purge" step.

### 6. Atomicity and concurrent daemons

Write path: `open(tmp, "w")`, write, `os.replace(tmp, final)`. POSIX `rename` is atomic on the same filesystem, so a reader either sees the previous full file or the new full file — never a partial write.

Two daemons running at once would race. We do not currently have a lockfile and this design does not add one — running two instances was already broken (they would both create overlays for the same windows and fight over X11 events). The sessions.json race is symptomatic of that pre-existing bug, not a new problem.

### 7. Schema evolution

`version: 1` today. If we ever extend (e.g. add `cwd_hash` for cross-machine identity, or per-session config overrides), bump to 2. Loader rule: refuse to load `version > KNOWN_VERSION`, log WARNING, start empty. This means a downgrade after upgrade loses bindings once, which is acceptable.

## Data flow

```
hook fires → emit-event.sh → events/*.json
                            ↓
                    EventIngester
                            ↓
                  Daemon._on_event
                            ↓
                SessionStore.apply_event
                  ├─ updates session fields
                  ├─ _notify(session_id)
                  │     ├─ OverlayManager.on_session_changed (existing)
                  │     └─ BindingPersister.save (NEW, throttled)
                  └─ atomic write to sessions.json

Daemon startup:
  read sessions.json
  for each persisted session:
    if x11.get_visible_geometry(client_window_id) is None: drop
    else: SessionStore.restore(session)
  for each restored session: OverlayManager._sync_one(session)
```

## Components changed

| File | Change |
|------|--------|
| `claude_alerts/persistence.py` (new) | `BindingPersister` class: throttled writer, atomic `tmp + rename`, JSON schema v1, INFO/WARNING-only failure handling. |
| `claude_alerts/sessions.py` | Add `restore(session: Session)` for direct insert without going through state machine. Optional `persist_callback: Callable[[], None]` field on `SessionStore`, called at end of `_notify`. |
| `claude_alerts/daemon.py` | Construct `BindingPersister`, wire into store. On `run()`, load `sessions.json`, verify each entry, restore live ones, sync overlays before entering the event loop. |
| `claude_alerts/overlay.py` | New `sync_all()` helper that calls `_sync_one` for every session in the store — used after restore to bring all overlays up. |
| `tests/test_persistence.py` (new) | Round-trip serialization, throttled writes, atomic write under simulated crash mid-write, version-mismatch handling, malformed-file handling, dead-window pruning on load. |
| `tests/test_sessions.py` | `restore()` adds a session without firing `apply_event` machinery; `restore` does fire `_notify` so persistence picks it up; `restore` followed by `apply_event` keeps the right state. |
| `tests/test_daemon_restart.py` (new, optional) | Integration: stand up a daemon, bind a session, stop, restart with the same `sessions.json`, confirm the overlay re-appears without further events. (Likely needs Xvfb; gate similarly to `test_e2e_xvfb.py`.) |

## Testing

Unit tests cover the persister in isolation: schema, throttling, atomicity, version skew, malformed input.

State-machine tests cover the new `restore` entry point.

Manual verification after deploy:
1. Bind several sessions, restart daemon, confirm all bound borders return immediately, with the right color (green for working, red for waiting, etc.).
2. Bind a session, close the terminal, kill the daemon before `DestroyNotify` propagates (`pkill -9 claude-alerts`), restart — confirm the dead session is dropped and its overlay does not appear.
3. Restart the X server (logout / login) — confirm the daemon starts cleanly with no zombie overlays, even though the file has stale window IDs.
4. Disk full at write time — confirm daemon logs WARNING and stays alive.

## Known limitations

- A bound session whose window ID is reused by an unrelated app between daemon stop and start would paint the wrong window. Mitigation: the WM_CLASS check during `try_bind` does not run on restore — we trust persisted bindings. In practice, X11 window IDs are not reused often enough on a single login session to make this a frequent issue, and an X server restart drops them all anyway.
- The 200ms write throttle means a daemon SIGKILLed within that window may persist state slightly behind reality. Acceptable: at most one event of drift, recoverable on the next mutation.
- The persistence file grows with the number of bound sessions but shrinks immediately when sessions unbind. No GC needed.

## Migration

No state migration: the file does not exist before this change. First-time start on a daemon with this code reads nothing and writes its first snapshot on the first hook event.

Bump version to `0.2.0` on release — this is the first user-visible feature beyond bug fixes.

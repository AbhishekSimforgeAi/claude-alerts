# Overlay correctness during idle pauses

**Date:** 2026-04-16
**Status:** Approved
**Owner:** Abhishek Shinde

## Problem

Two observable issues with the running overlay daemon, both rooted in the daemon mistaking "Claude is paused between turns" for "Claude is done."

1. **Border turns red while a background task is alive.** When Claude finishes its turn but has launched an autonomous wake-up mechanism (`Monitor`, `CronCreate`, `RemoteTrigger`, `ScheduleWakeup`), the `Stop` hook fires and the daemon flips the session to `WAITING` (red). Visually this looks like Claude needs the user, but Claude is actually waiting on a background event that will resume the turn without user input.

2. **Border disappears entirely after ~5 minutes of idle.** `Daemon.run` calls `store.evict_idle(now, max_age_s=IDLE_MAX_AGE_S)` every 30 seconds (`claude_alerts/daemon.py:24-25,89-92`), and any session whose last hook event is older than 300s is removed. Eviction notifies the overlay manager, which destroys the overlay window. So a long-idle but otherwise live `claude` session loses its border, and the user has no visual indicator that the terminal is still under daemon management.

Both bugs share a single root cause: the daemon treats hook silence as "session ended." Hook silence actually means "Claude is between turns" — possibly waiting for a background event (case 1), possibly just sitting idle while the user reads (case 2).

## Goals

- After `Stop`, keep the border green if the session has at least one autonomous wake-up mechanism alive.
- Keep the border on a bound terminal until the terminal window is destroyed or `SessionEnd` fires — never time it out.
- No new hook events from Claude Code, no new runtime dependencies.
- Notifications (permission prompts) still flip the border red, even with a background task alive.

## Non-goals

- Precise lifecycle tracking of background tasks. Once a background task is recorded, it stays recorded until the next `UserPromptSubmit` (user took over) or session removal — even if the underlying task quietly completes. False-positive green is acceptable; false-negative red is the bug.
- Tracking by task ID. We track a single boolean per session, not a set.
- Parsing tool inputs (e.g. `Monitor`'s `timeout`, `ScheduleWakeup`'s `delaySeconds`) for TTL-based expiration.
- A new color or pulse for "background-active." The user's stated mental model is two colors; the new behavior just suppresses the red transition.
- Changing the eviction policy for *unbound* sessions. They still evict at 300s — they have no overlay to preserve.

## Design

### 1. Track `background_active` per session

Add a boolean field to `Session`:

```python
@dataclass
class Session:
    session_id: str
    cwd: str
    claude_pid: int
    status: Status
    last_event_at: float
    bound_window_id: Optional[int] = None
    client_window_id: Optional[int] = None
    background_active: bool = False
```

Set to `True` on `PostToolUse` whose `tool_name` is in:

```python
BACKGROUND_TASK_TOOLS = frozenset({
    "Monitor", "CronCreate", "RemoteTrigger", "ScheduleWakeup",
})
```

`PostToolUse` (rather than `PreToolUse`) is chosen so the flag only flips on successful tool completion — a tool that errors out before creating a background mechanism shouldn't leave the flag set.

Set to `False` on:
- `UserPromptSubmit` — the user took over; whatever was going to wake Claude is now superseded.
- `SessionEnd` — the session is removed regardless.
- Idle eviction of an unbound session — same.

`Stop` and `Notification` do **not** clear the flag.

### 2. Color rule (preview — finalized in section 3)

The intent is: paint green when `status == WORKING`, **or** when `status == WAITING` and the session has an autonomous wake-up alive — *unless* the WAITING was caused by a `Notification` (permission prompt), in which case red wins.

The current model can't express the last clause: both `Stop` and `Notification` map to the same `Status.WAITING`, so the overlay can't tell them apart. Section 3 introduces a `last_event` field on `Session` to resolve this and gives the final `color_for` implementation.

### 3. Distinguish Stop-WAITING from Notification-WAITING

`Status` today is two values. To honor the rule "Notification always wins → red, even with background_active," the daemon needs to know which event last drove the session into `WAITING`. Two options:

**Option chosen — track last event on the session:**

```python
@dataclass
class Session:
    ...
    last_event: str = ""   # last hook event name applied
```

`apply_event` sets `last_event = evt.event` on every event. `OverlayManager.color_for` becomes:

```python
def color_for(self, session: Session) -> int:
    if session.status == Status.WORKING:
        return self._working_pixel
    # status == WAITING
    if session.background_active and session.last_event != "Notification":
        return self._working_pixel
    return self._waiting_pixel
```

This keeps `Status` as a clean two-value enum (WORKING / WAITING means literally what the spec says: is the user expected to act?) while letting the overlay layer apply the green-during-pause rule.

Rejected alternative: a third `Status` value (e.g. `BACKGROUND_PAUSED`). It would proliferate state through `_EVENT_TO_STATUS` — every event handler would need to think about whether to land in WORKING vs BACKGROUND_PAUSED. The flag-on-the-side approach is local to a single field and a single overlay function.

### 4. Hook plumbing — pass `tool_name` through

`scripts/hooks/emit-event.sh` currently strips the Claude Code hook payload to five fields and discards `tool_name`. Add a sixth, optional field:

```bash
TOOL_NAME="$(printf '%s' "$PAYLOAD" | jq -r '.tool_name // empty')"

jq -cn \
    --arg event "$EVENT" \
    --arg session_id "$SESSION_ID" \
    --arg cwd "$CWD" \
    --arg tool_name "$TOOL_NAME" \
    --argjson claude_pid "$$" \
    --argjson timestamp "$TS" \
    '{event:$event, session_id:$session_id, cwd:$cwd, claude_pid:$claude_pid, timestamp:$timestamp}
     + (if $tool_name == "" then {} else {tool_name:$tool_name} end)' \
    > "$TMP"
```

`ClaudeEvent` gains an optional `tool_name: Optional[str] = None`. `parse_event_file` reads it when present, validates string-ness, and otherwise defaults to `None`.

Only `PreToolUse` and `PostToolUse` Claude Code hook payloads carry `tool_name`. For other events the field is empty / absent and the daemon ignores it.

### 5. Eviction — never evict bound sessions

`SessionStore.evict_idle` is currently:

```python
def evict_idle(self, now: float, max_age_s: float) -> list[str]:
    evicted = [
        sid for sid, s in self._sessions.items()
        if now - s.last_event_at > max_age_s
    ]
    ...
```

Add a `bound_window_id is None` filter:

```python
def evict_idle(self, now: float, max_age_s: float) -> list[str]:
    evicted = [
        sid for sid, s in self._sessions.items()
        if s.bound_window_id is None
        and now - s.last_event_at > max_age_s
    ]
    ...
```

Rationale: an unbound session is one we never produced an overlay for, so eviction is purely housekeeping. A bound session has an overlay tied to a real X11 window — that window is the source of truth for "still alive." When the window is destroyed, `DestroyNotify` already fires `binder.unbind_window`, which clears `bound_window_id`; the next sweep is then free to evict the session normally if it's also stale. `SessionEnd` removes the session immediately. There is no remaining case where a bound, evicted session is the right outcome.

Edge case: a bound session whose terminal is closed without `SessionEnd` and without `DestroyNotify` (e.g. the X server disconnecting briefly) would persist forever. This matches the existing fallback path — the daemon must already tolerate stale window IDs from other code paths — and is cheaper than the alternative of losing the border on every long pause.

## Data flow

```
Claude Code hook → emit-event.sh (writes JSON incl. tool_name)
                ↓
        EventIngester (file-watcher thread)
                ↓
        Daemon._on_event (main thread)
                ↓
        SessionStore.apply_event
          ├─ updates status per _EVENT_TO_STATUS
          ├─ updates background_active per BACKGROUND_TASK_TOOLS rules
          ├─ updates last_event = evt.event
          └─ notifies subscribers
                ↓
        OverlayManager.on_session_changed
                ↓
        OverlayManager._sync_one
                ↓
        color_for(session) — returns green if WORKING, or
                              (WAITING ∧ background_active ∧ last_event ≠ Notification)
```

## Components changed

| File | Change |
|------|--------|
| `claude_alerts/sessions.py` | Add `background_active` and `last_event` fields. Add `BACKGROUND_TASK_TOOLS` set. Update `apply_event` to set/clear the flag and stamp `last_event`. Filter `evict_idle` to skip bound sessions. |
| `claude_alerts/events.py` | Add optional `tool_name: Optional[str] = None` to `ClaudeEvent`. Update `parse_event_file` to read it when present and validate string type. |
| `claude_alerts/overlay.py` | `OverlayManager.color_for` takes a `Session` (not just `Status`) and applies the new rule. `_sync_one` passes `session` instead of `session.status`. |
| `scripts/hooks/emit-event.sh` | Extract `tool_name` from the hook payload and include it in the emitted JSON when non-empty. |
| `tests/test_sessions.py` | New tests: PostToolUse for each background tool sets the flag; UserPromptSubmit clears it; SessionEnd removes the session; bound sessions are not evicted on idle; unbound sessions still evict. |
| `tests/test_events.py` | New test: `parse_event_file` reads `tool_name` when present, defaults to None when absent, rejects non-string. |
| `tests/test_overlay.py` (or wherever color logic is tested) | New tests: color is green when status=WORKING; green when status=WAITING ∧ background_active ∧ last_event=Stop; red when status=WAITING ∧ background_active ∧ last_event=Notification; red when status=WAITING ∧ ¬background_active. |

## Testing

Unit tests for the state machine, event parser, and color rule cover all the cases in the table above. The existing e2e Xvfb test (`tests/test_e2e_xvfb.py`) does not need to grow — these are state/color logic, not display logic.

Manual verification after deploy:
1. Run `Monitor` in a session; let `Stop` fire; confirm border stays green.
2. Type a `UserPromptSubmit`; confirm border stays green during work, briefly green-after-Stop only if a new background task was launched, otherwise red.
3. Trigger a permission prompt while `background_active`; confirm border goes red.
4. Leave a session idle for >5 minutes; confirm border still present.

## Known limitations

- A background task that completes naturally (e.g. `Monitor` hits `until` condition or times out at its declared `timeout`) without a `TaskStop` will leave `background_active=True` until the user types a prompt. The border stays green for that period. This is the cost of Approach A from brainstorming; mitigated by the fact that the user typing anything resets it.
- A bound session whose terminal is closed without `SessionEnd` *and* without `DestroyNotify` propagating to the daemon will persist forever. Pre-existing fallback path; this design does not make it worse.

## Migration

No state migration required. The new `background_active` and `last_event` fields default to `False` and `""` for any in-flight session at daemon restart. The `tool_name` field is optional in the wire format — old hook scripts on disk continue to work unchanged (they just produce events without `tool_name`, which matches the `None` default).

Bump version to `0.1.2` on release.

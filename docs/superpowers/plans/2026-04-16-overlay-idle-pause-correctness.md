# Overlay correctness during idle pauses — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the overlay border stay green when Claude has an autonomous wake-up alive after `Stop`, and stop bound sessions from being evicted on hook silence — implementing the spec at `docs/superpowers/specs/2026-04-16-overlay-idle-pause-correctness-design.md`.

**Architecture:** Plumb `tool_name` through the hook event JSON, add two fields to `Session` (`background_active: bool`, `last_event: str`), filter `evict_idle` to skip bound sessions, and change `OverlayManager.color_for` to take a `Session` and apply a new color rule that lets background-active sessions paint green even when `Status.WAITING` — except when the WAITING was caused by a `Notification`.

**Tech Stack:** Python 3.10, pytest, bash + jq (hook script).

**Branch:** Recommend a new branch (e.g. `feat/overlay-idle-pause`) from `main`. Optionally use a worktree (see superpowers:using-git-worktrees) since this isolates against any in-flight v0.1.2 work.

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `claude_alerts/events.py` | Modify | Add optional `tool_name` to `ClaudeEvent`; read & validate it in `parse_event_file`. |
| `scripts/hooks/emit-event.sh` | Modify | Extract `tool_name` from the hook payload and include it (when non-empty) in the emitted JSON. |
| `claude_alerts/sessions.py` | Modify | Add `last_event` and `background_active` fields to `Session`; add `BACKGROUND_TASK_TOOLS`; update `apply_event` to stamp `last_event` and toggle `background_active`; filter `evict_idle` to skip bound sessions. |
| `claude_alerts/overlay.py` | Modify | `OverlayManager.color_for` accepts a `Session` and applies the new rule. `_sync_one` passes the session in. |
| `tests/test_events.py` | Modify | New tests: `tool_name` parsed when present, defaults to `None` when absent, rejects non-string. |
| `tests/test_hook_script.py` | Modify | New tests: `tool_name` field included when payload has it; absent when payload doesn't. |
| `tests/test_sessions.py` | Modify | New tests for `last_event`, `background_active` set/clear lifecycle, and bound-session eviction filter. |
| `tests/test_overlay_smoke.py` | Modify | New tests for the `color_for(session)` rule (all four cases). |
| `pyproject.toml` | Modify | Version bump `0.1.1` → `0.1.2`. |

---

### Task 1: Extend `ClaudeEvent` with optional `tool_name`

**Files:**
- Modify: `claude_alerts/events.py`
- Test: `tests/test_events.py`

- [ ] **Step 1: Write failing tests for `tool_name`**

Append to `tests/test_events.py`:

```python
def test_parses_tool_name_when_present(tmp_path):
    p = write_event(
        tmp_path,
        {
            "event": "PostToolUse",
            "session_id": "abc",
            "cwd": "/p",
            "claude_pid": 1,
            "timestamp": 1.0,
            "tool_name": "Monitor",
        },
    )
    evt = parse_event_file(p)
    assert evt.tool_name == "Monitor"


def test_tool_name_defaults_to_none_when_absent(tmp_path):
    p = write_event(
        tmp_path,
        {
            "event": "Stop",
            "session_id": "abc",
            "cwd": "/p",
            "claude_pid": 1,
            "timestamp": 1.0,
        },
    )
    evt = parse_event_file(p)
    assert evt.tool_name is None


def test_rejects_non_string_tool_name(tmp_path):
    p = write_event(
        tmp_path,
        {
            "event": "PostToolUse",
            "session_id": "abc",
            "cwd": "/p",
            "claude_pid": 1,
            "timestamp": 1.0,
            "tool_name": 42,
        },
    )
    with pytest.raises(EventParseError, match="tool_name"):
        parse_event_file(p)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_events.py -v`
Expected: the three new tests fail (`AttributeError: 'ClaudeEvent' object has no attribute 'tool_name'` for the first two, no validation raised for the third).

- [ ] **Step 3: Add `tool_name` to `ClaudeEvent` and parser**

In `claude_alerts/events.py`, change the dataclass and parser:

```python
from typing import Optional

@dataclass(frozen=True)
class ClaudeEvent:
    event: str
    session_id: str
    cwd: str
    claude_pid: int
    timestamp: float
    tool_name: Optional[str] = None
```

In `parse_event_file`, after the existing timestamp validation and before the `event in VALID_EVENTS` check, add:

```python
    tool_name = raw.get("tool_name")
    if tool_name is not None and not isinstance(tool_name, str):
        raise EventParseError(
            f"field 'tool_name' must be str or absent, got {type(tool_name).__name__}"
        )
```

And include it in the returned `ClaudeEvent(...)`:

```python
    return ClaudeEvent(
        event=raw["event"],
        session_id=raw["session_id"],
        cwd=raw["cwd"],
        claude_pid=pid,
        timestamp=ts,
        tool_name=tool_name,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_events.py -v`
Expected: all event tests pass (existing + 3 new).

- [ ] **Step 5: Commit**

```bash
git add claude_alerts/events.py tests/test_events.py
git commit -m "$(cat <<'EOF'
feat(events): plumb optional tool_name through ClaudeEvent

PreToolUse and PostToolUse hook payloads carry tool_name; downstream
session-state logic needs it to recognize background-task creators
(Monitor, CronCreate, RemoteTrigger, ScheduleWakeup).
EOF
)"
```

---

### Task 2: Pass `tool_name` through `emit-event.sh`

**Files:**
- Modify: `scripts/hooks/emit-event.sh`
- Test: `tests/test_hook_script.py`

- [ ] **Step 1: Write failing tests for `tool_name` plumbing**

Append to `tests/test_hook_script.py`:

```python
@pytest.mark.skipif(not has_jq(), reason="jq is required by the hook script")
def test_hook_script_includes_tool_name_when_present(tmp_path):
    env = os.environ.copy()
    env["CLAUDE_ALERTS_EVENTS_DIR"] = str(tmp_path)
    payload = json.dumps({
        "session_id": "abc", "cwd": "/p", "tool_name": "Monitor",
    })
    subprocess.run(
        ["bash", str(HOOK_SCRIPT), "PostToolUse"],
        input=payload, text=True, env=env, check=True,
    )
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["tool_name"] == "Monitor"


@pytest.mark.skipif(not has_jq(), reason="jq is required by the hook script")
def test_hook_script_omits_tool_name_when_absent(tmp_path):
    env = os.environ.copy()
    env["CLAUDE_ALERTS_EVENTS_DIR"] = str(tmp_path)
    payload = json.dumps({"session_id": "abc", "cwd": "/p"})
    subprocess.run(
        ["bash", str(HOOK_SCRIPT), "Stop"],
        input=payload, text=True, env=env, check=True,
    )
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert "tool_name" not in data
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_hook_script.py -v`
Expected: `test_hook_script_includes_tool_name_when_present` fails (no `tool_name` key in output).

- [ ] **Step 3: Update `emit-event.sh` to extract and emit `tool_name`**

Replace `scripts/hooks/emit-event.sh` with:

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
TOOL_NAME="$(printf '%s' "$PAYLOAD" | jq -r '.tool_name // empty')"
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
    --arg tool_name "$TOOL_NAME" \
    --argjson claude_pid "$$" \
    --argjson timestamp "$TS" \
    '{event:$event, session_id:$session_id, cwd:$cwd, claude_pid:$claude_pid, timestamp:$timestamp}
     + (if $tool_name == "" then {} else {tool_name:$tool_name} end)' \
    > "$TMP"
mv "$TMP" "$FINAL"
exit 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_hook_script.py -v`
Expected: all hook script tests pass (existing 2 + 2 new).

- [ ] **Step 5: Commit**

```bash
git add scripts/hooks/emit-event.sh tests/test_hook_script.py
git commit -m "$(cat <<'EOF'
feat(hooks): pass tool_name through to emit-event.sh output

PreToolUse and PostToolUse hook payloads carry tool_name. Include it
in the emitted JSON so the daemon can distinguish background-task
creators (Monitor / CronCreate / RemoteTrigger / ScheduleWakeup) from
ordinary tool calls. Field is omitted entirely when the payload has
none, to keep wire format compact for events that don't need it.
EOF
)"
```

---

### Task 3: Stamp `last_event` on `Session`

**Files:**
- Modify: `claude_alerts/sessions.py`
- Test: `tests/test_sessions.py`

- [ ] **Step 1: Write failing test for `last_event`**

Append to `tests/test_sessions.py`:

```python
def test_apply_event_stamps_last_event():
    """Session.last_event records the name of the most recently applied event,
    so the overlay can distinguish Stop-WAITING from Notification-WAITING."""
    store = SessionStore()
    store.apply_event(evt("SessionStart"))
    assert store.get("s1").last_event == "SessionStart"
    store.apply_event(evt("UserPromptSubmit", t=2.0))
    assert store.get("s1").last_event == "UserPromptSubmit"
    store.apply_event(evt("PreToolUse", t=3.0))
    assert store.get("s1").last_event == "PreToolUse"
    store.apply_event(evt("Stop", t=4.0))
    assert store.get("s1").last_event == "Stop"
    store.apply_event(evt("Notification", t=5.0))
    assert store.get("s1").last_event == "Notification"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_sessions.py::test_apply_event_stamps_last_event -v`
Expected: FAIL — `AttributeError: 'Session' object has no attribute 'last_event'`.

- [ ] **Step 3: Add `last_event` field and stamp it in `apply_event`**

In `claude_alerts/sessions.py`, update the `Session` dataclass:

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
    last_event: str = ""
```

In `SessionStore.apply_event`, set `last_event` on both branches (new session and existing session). Replace the entire `apply_event` method so it reads:

```python
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
                last_event=evt.event,
            )
            self._sessions[evt.session_id] = session
            changed = True
        else:
            if session.status != new_status:
                session.status = new_status
                changed = True
            session.last_event_at = evt.timestamp
            session.last_event = evt.event

        if changed:
            self._notify(evt.session_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_sessions.py -v`
Expected: all session tests pass (existing + 1 new).

- [ ] **Step 5: Commit**

```bash
git add claude_alerts/sessions.py tests/test_sessions.py
git commit -m "$(cat <<'EOF'
feat(sessions): record last_event on Session

Needed so the overlay layer can distinguish Stop-WAITING from
Notification-WAITING — the existing two-value Status enum can't, but
the rule "Notification always wins → red" depends on knowing which
event caused the WAITING state.
EOF
)"
```

---

### Task 4: Add `background_active` flag and lifecycle

**Files:**
- Modify: `claude_alerts/sessions.py`
- Test: `tests/test_sessions.py`

- [ ] **Step 1: Write failing tests for `background_active` lifecycle**

Append to `tests/test_sessions.py`:

```python
def _post(tool_name, t=2.0, session_id="s1"):
    return ClaudeEvent(
        event="PostToolUse",
        session_id=session_id,
        cwd="/p",
        claude_pid=1,
        timestamp=t,
        tool_name=tool_name,
    )


def test_background_active_defaults_false():
    store = SessionStore()
    store.apply_event(evt("SessionStart"))
    assert store.get("s1").background_active is False


def test_post_tool_use_monitor_sets_background_active():
    store = SessionStore()
    store.apply_event(evt("SessionStart"))
    store.apply_event(_post("Monitor", t=2.0))
    assert store.get("s1").background_active is True


def test_post_tool_use_croncreate_sets_background_active():
    store = SessionStore()
    store.apply_event(evt("SessionStart"))
    store.apply_event(_post("CronCreate", t=2.0))
    assert store.get("s1").background_active is True


def test_post_tool_use_remotetrigger_sets_background_active():
    store = SessionStore()
    store.apply_event(evt("SessionStart"))
    store.apply_event(_post("RemoteTrigger", t=2.0))
    assert store.get("s1").background_active is True


def test_post_tool_use_schedulewakeup_sets_background_active():
    store = SessionStore()
    store.apply_event(evt("SessionStart"))
    store.apply_event(_post("ScheduleWakeup", t=2.0))
    assert store.get("s1").background_active is True


def test_post_tool_use_other_tool_does_not_set_background_active():
    """Read/Edit/Bash etc. must not flip the background flag."""
    store = SessionStore()
    store.apply_event(evt("SessionStart"))
    store.apply_event(_post("Read", t=2.0))
    assert store.get("s1").background_active is False


def test_pre_tool_use_does_not_set_background_active():
    """Only PostToolUse counts — PreToolUse fires for tools that may error out."""
    store = SessionStore()
    store.apply_event(evt("SessionStart"))
    pre = ClaudeEvent(
        event="PreToolUse", session_id="s1", cwd="/p", claude_pid=1,
        timestamp=2.0, tool_name="Monitor",
    )
    store.apply_event(pre)
    assert store.get("s1").background_active is False


def test_user_prompt_submit_clears_background_active():
    """User typing a new prompt supersedes any pending wake-up."""
    store = SessionStore()
    store.apply_event(evt("SessionStart"))
    store.apply_event(_post("Monitor", t=2.0))
    assert store.get("s1").background_active is True
    store.apply_event(evt("UserPromptSubmit", t=3.0))
    assert store.get("s1").background_active is False


def test_stop_does_not_clear_background_active():
    """Stop is exactly the case where we want to keep the flag — Claude
    paused and will be resumed by the background mechanism."""
    store = SessionStore()
    store.apply_event(evt("SessionStart"))
    store.apply_event(_post("Monitor", t=2.0))
    store.apply_event(evt("Stop", t=3.0))
    assert store.get("s1").background_active is True


def test_notification_does_not_clear_background_active():
    """Notification means user attention is needed, but the background
    task is still alive. The overlay layer applies the override; the
    flag itself stays set."""
    store = SessionStore()
    store.apply_event(evt("SessionStart"))
    store.apply_event(_post("Monitor", t=2.0))
    store.apply_event(evt("Notification", t=3.0))
    assert store.get("s1").background_active is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_sessions.py -v`
Expected: the new tests fail (`AttributeError: ... no attribute 'background_active'` and similar).

- [ ] **Step 3: Add `background_active` field, `BACKGROUND_TASK_TOOLS`, and lifecycle hooks**

In `claude_alerts/sessions.py`, update the `Session` dataclass:

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
    last_event: str = ""
    background_active: bool = False
```

Add the tool set near `_EVENT_TO_STATUS`:

```python
# Tools whose successful invocation means Claude has armed an autonomous
# wake-up: when this turn ends, something else will resume Claude without
# the user typing. The overlay uses this to keep the border green during
# the pause between Stop and the wake-up firing.
BACKGROUND_TASK_TOOLS = frozenset({
    "Monitor",
    "CronCreate",
    "RemoteTrigger",
    "ScheduleWakeup",
})
```

In `SessionStore.apply_event`, insert the background-active lifecycle logic immediately before the final `if changed: self._notify(evt.session_id)` block. Replace the last two lines of the method (the existing `if changed:` block) with:

```python
        # Background-active lifecycle. Set on successful PostToolUse for
        # any wake-up creator; cleared on UserPromptSubmit (user took over).
        # Stop / Notification leave it alone — the overlay layer applies
        # the Notification-overrides-green rule itself.
        if evt.event == "PostToolUse" and evt.tool_name in BACKGROUND_TASK_TOOLS:
            if not session.background_active:
                session.background_active = True
                changed = True
        elif evt.event == "UserPromptSubmit":
            if session.background_active:
                session.background_active = False
                changed = True

        if changed:
            self._notify(evt.session_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_sessions.py -v`
Expected: all session tests pass (existing + 10 new from this task).

- [ ] **Step 5: Commit**

```bash
git add claude_alerts/sessions.py tests/test_sessions.py
git commit -m "$(cat <<'EOF'
feat(sessions): track background_active for autonomous wake-ups

PostToolUse for Monitor / CronCreate / RemoteTrigger / ScheduleWakeup
sets a per-session background_active flag. UserPromptSubmit clears it
(user took over). Stop and Notification leave it alone — the override
"Notification → red even with background_active" is applied at the
overlay layer using last_event, not by clearing the flag here.

This is the state-machine half of fixing the red-after-Stop bug; the
overlay color rule wires it to a visible color in a follow-up commit.
EOF
)"
```

---

### Task 5: Skip eviction for bound sessions

**Files:**
- Modify: `claude_alerts/sessions.py`
- Test: `tests/test_sessions.py`

- [ ] **Step 1: Write failing test for the eviction filter**

Append to `tests/test_sessions.py`:

```python
def test_evict_idle_skips_bound_sessions():
    """A bound session has an overlay tied to a real X11 window; the
    window is the source of truth for liveness, not hook silence."""
    store = SessionStore()
    store.apply_event(evt("SessionStart", t=100.0))
    store.set_bound_window("s1", 0xABCD)
    # Far past the idle threshold.
    evicted = store.evict_idle(now=1000.0, max_age_s=300.0)
    assert evicted == []
    assert store.get("s1") is not None
    assert store.get("s1").bound_window_id == 0xABCD


def test_evict_idle_still_evicts_unbound_sessions():
    """Regression guard for the existing unbound-eviction behaviour."""
    store = SessionStore()
    store.apply_event(evt("SessionStart", t=100.0))
    # No set_bound_window → still unbound.
    evicted = store.evict_idle(now=1000.0, max_age_s=300.0)
    assert evicted == ["s1"]
    assert store.get("s1") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_sessions.py::test_evict_idle_skips_bound_sessions -v`
Expected: FAIL — `assert ['s1'] == []` (the bound session gets evicted under current behaviour).

- [ ] **Step 3: Filter `evict_idle` by `bound_window_id`**

In `claude_alerts/sessions.py`, replace `evict_idle`:

```python
    def evict_idle(self, now: float, max_age_s: float) -> list[str]:
        """Remove unbound sessions whose last event is older than max_age_s.

        Bound sessions are NEVER evicted on idle: their overlay is tied to
        a live X11 window, and that window's destruction is signalled by
        DestroyNotify (handled by the binder), not by hook silence. A long
        pause between turns must not destroy the border.

        Returns evicted ids.
        """
        evicted = [
            sid for sid, s in self._sessions.items()
            if s.bound_window_id is None
            and now - s.last_event_at > max_age_s
        ]
        for sid in evicted:
            del self._sessions[sid]
            self._notify(sid)
        return evicted
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_sessions.py -v`
Expected: all session tests pass.

- [ ] **Step 5: Commit**

```bash
git add claude_alerts/sessions.py tests/test_sessions.py
git commit -m "$(cat <<'EOF'
fix(sessions): never idle-evict a bound session

The 300s idle sweep was destroying the overlay on a bound, still-live
terminal whenever Claude paused for more than five minutes between
events (e.g. waiting for a long Monitor to fire). The X11 window is
the source of truth for liveness — DestroyNotify and SessionEnd handle
true session ends. Idle eviction is now restricted to unbound
sessions, which have no overlay to lose.
EOF
)"
```

---

### Task 6: Apply the new color rule in `OverlayManager`

**Files:**
- Modify: `claude_alerts/overlay.py`
- Test: `tests/test_overlay_smoke.py`

- [ ] **Step 1: Write failing tests for the four color cases**

Append to `tests/test_overlay_smoke.py`. (Reuse the existing `_make_manager` and `_start_session` helpers.)

```python
def _green_pixel():
    from claude_alerts.overlay import _rgb_to_pixel, hex_to_rgb
    return _rgb_to_pixel(hex_to_rgb(Config().color_working))


def _red_pixel():
    from claude_alerts.overlay import _rgb_to_pixel, hex_to_rgb
    return _rgb_to_pixel(hex_to_rgb(Config().color_waiting))


def test_color_for_working_is_green(monkeypatch):
    mgr, store, _ = _make_manager(monkeypatch)
    _start_session(store)
    store.apply_event(ClaudeEvent(
        event="UserPromptSubmit", session_id="s1", cwd="/p",
        claude_pid=1, timestamp=2.0,
    ))
    s = store.get("s1")
    assert mgr.color_for(s) == _green_pixel()


def test_color_for_stop_with_background_active_is_green(monkeypatch):
    """The headline fix: Stop after Monitor stays green."""
    mgr, store, _ = _make_manager(monkeypatch)
    _start_session(store)
    store.apply_event(ClaudeEvent(
        event="PostToolUse", session_id="s1", cwd="/p", claude_pid=1,
        timestamp=2.0, tool_name="Monitor",
    ))
    store.apply_event(ClaudeEvent(
        event="Stop", session_id="s1", cwd="/p", claude_pid=1, timestamp=3.0,
    ))
    s = store.get("s1")
    assert s.status == Status.WAITING
    assert s.background_active is True
    assert mgr.color_for(s) == _green_pixel()


def test_color_for_notification_with_background_active_is_red(monkeypatch):
    """Permission prompts still demand attention even with a Monitor alive."""
    mgr, store, _ = _make_manager(monkeypatch)
    _start_session(store)
    store.apply_event(ClaudeEvent(
        event="PostToolUse", session_id="s1", cwd="/p", claude_pid=1,
        timestamp=2.0, tool_name="Monitor",
    ))
    store.apply_event(ClaudeEvent(
        event="Notification", session_id="s1", cwd="/p", claude_pid=1, timestamp=3.0,
    ))
    s = store.get("s1")
    assert s.background_active is True
    assert s.last_event == "Notification"
    assert mgr.color_for(s) == _red_pixel()


def test_color_for_stop_without_background_active_is_red(monkeypatch):
    """Plain Stop with no background task: red, as before."""
    mgr, store, _ = _make_manager(monkeypatch)
    _start_session(store)
    store.apply_event(ClaudeEvent(
        event="UserPromptSubmit", session_id="s1", cwd="/p",
        claude_pid=1, timestamp=2.0,
    ))
    store.apply_event(ClaudeEvent(
        event="Stop", session_id="s1", cwd="/p", claude_pid=1, timestamp=3.0,
    ))
    s = store.get("s1")
    assert s.background_active is False
    assert mgr.color_for(s) == _red_pixel()
```

Also import `Status` at the top of `tests/test_overlay_smoke.py` (alongside the existing `SessionStore` import):

```python
from claude_alerts.sessions import SessionStore, Status
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_overlay_smoke.py -v`
Expected: the four new tests fail. The first will fail with a `TypeError` once `color_for` no longer accepts a `Status`; the others will fail because the green-during-pause behaviour isn't implemented.

If the first test currently passes because the old `color_for(status)` signature still accepts a `Session`-where-Status-was-expected, that's by accident; the new signature in step 3 will break it intentionally and the test will guard the new contract.

- [ ] **Step 3: Update `OverlayManager.color_for` and `_sync_one`**

In `claude_alerts/overlay.py`, change `color_for` to take a `Session`:

```python
    def color_for(self, session: Session) -> int:
        """Pick the overlay color for a session.

        Green when Claude is actively WORKING, or when the session has an
        autonomous wake-up alive (background_active) and the most recent
        event was NOT a Notification. Notifications always paint red,
        because they mean the user must act, even if a background task is
        also alive.
        """
        if session.status == Status.WORKING:
            return self._working_pixel
        # status == WAITING from here.
        if session.background_active and session.last_event != "Notification":
            return self._working_pixel
        return self._waiting_pixel
```

And update `_sync_one` to pass the session in (replace the existing `color_pixel = self.color_for(session.status)` line):

```python
        color_pixel = self.color_for(session)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_overlay_smoke.py tests/test_sessions.py tests/test_events.py tests/test_hook_script.py -v`
Expected: all pass.

- [ ] **Step 5: Run the full unit suite**

Run: `.venv/bin/pytest tests/ -v --ignore=tests/test_e2e_xvfb.py`
Expected: all pass (the e2e Xvfb test is excluded — verified manually after deploy).

- [ ] **Step 6: Commit**

```bash
git add claude_alerts/overlay.py tests/test_overlay_smoke.py
git commit -m "$(cat <<'EOF'
feat(overlay): keep border green during background-active pauses

OverlayManager.color_for now takes a Session and applies the new rule:
green when WORKING, also green when WAITING ∧ background_active and
the last event wasn't a Notification. Notifications still override to
red so permission prompts get the user's attention.

Closes the headline bug — after Stop with a Monitor alive, the border
no longer flips to red while Claude is just paused waiting for the
Monitor to fire.
EOF
)"
```

---

### Task 7: Bump version to 0.1.2

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Bump version**

In `pyproject.toml`, change line 7:

```
version = "0.1.1"
```

to:

```
version = "0.1.2"
```

- [ ] **Step 2: Commit**

```bash
git add pyproject.toml
git commit -m "chore: bump version to 0.1.2"
```

---

### Task 8: Manual verification

These are the post-deploy smoke checks called for in the spec. Run them after restarting the daemon (`pkill -f claude_alerts; .venv/bin/python -m claude_alerts &`, or via the user's systemd unit).

- [ ] **Step 1: Background-active stays green**

In a `claude` session in a bound terminal, run a tool call that uses `Monitor` (e.g. start any background task and watch it). When Claude finishes its turn (`Stop` fires) and shows the optional feedback prompt, confirm the border stays green.

- [ ] **Step 2: Plain Stop still goes red**

In a fresh `claude` session, ask a question that doesn't spawn a background task. After the response, confirm the border turns red.

- [ ] **Step 3: Notification overrides background-active**

In the same session as Step 1 (with a `Monitor` still alive), trigger a permission prompt (e.g. ask Claude to run a command requiring approval). Confirm the border goes red while the prompt is shown, and returns to green after acceptance.

- [ ] **Step 4: Bound session survives long idle**

In any bound `claude` session, leave the terminal idle (no new prompts, no events) for >6 minutes. Confirm the border is still present.

---

### Task 9: Merge and tag (when verification passes)

- [ ] **Step 1: Push the branch**

```bash
git push -u origin <branch-name>
```

- [ ] **Step 2: Merge to main**

Choose either a no-ff merge locally:

```bash
git checkout main
git merge <branch-name> --no-ff -m "Merge <branch-name> for v0.1.2 release"
```

Or, preferred, open a PR with `gh pr create` and merge through the normal review flow.

- [ ] **Step 3: Tag v0.1.2**

```bash
git tag -a v0.1.2 -m "v0.1.2 — keep border green during background-active pauses; never idle-evict bound sessions"
git push origin v0.1.2
```

- [ ] **Step 4: Verify**

```bash
git log --oneline -10
git tag -l
```

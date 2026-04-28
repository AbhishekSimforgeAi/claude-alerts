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


def test_pre_tool_use_keeps_working():
    """PreToolUse fires before any tool call — Claude is still working."""
    store = SessionStore()
    store.apply_event(evt("SessionStart"))
    store.apply_event(evt("UserPromptSubmit", t=1.5))
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


def test_permission_request_sets_waiting():
    """PermissionRequest fires for sandbox prompts (e.g. network access).
    The user must act before Claude can continue, so status is WAITING."""
    store = SessionStore()
    store.apply_event(evt("SessionStart"))
    store.apply_event(evt("PreToolUse", t=2.0))
    store.apply_event(evt("PermissionRequest", t=3.0))
    s = store.get("s1")
    assert s.status == Status.WAITING
    assert s.last_event == "PermissionRequest"


def test_elicitation_sets_waiting():
    """Elicitation fires when an MCP server asks for user input mid-tool-call.
    The user must act before the tool can continue, so status is WAITING."""
    store = SessionStore()
    store.apply_event(evt("SessionStart"))
    store.apply_event(evt("PreToolUse", t=2.0))
    store.apply_event(evt("Elicitation", t=3.0))
    s = store.get("s1")
    assert s.status == Status.WAITING
    assert s.last_event == "Elicitation"


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
    store.apply_event(evt("UserPromptSubmit", t=2.0))  # waiting -> working
    store.apply_event(evt("UserPromptSubmit", t=3.0))  # working -> working: NO change
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


def test_evict_idle_fires_on_change():
    store = SessionStore()
    seen = []
    store.on_change(lambda sid: seen.append(sid))
    store.apply_event(evt("SessionStart", t=100.0))
    seen.clear()  # ignore the create-time notification
    store.evict_idle(now=1000.0, max_age_s=300.0)
    assert seen == ["s1"]


def test_session_end_unknown_session_is_noop():
    store = SessionStore()
    seen = []
    store.on_change(lambda sid: seen.append(sid))
    # SessionEnd for a session that never existed should not raise or notify.
    store.apply_event(evt("SessionEnd", session_id="ghost"))
    assert seen == []
    assert store.get("ghost") is None


def test_set_bound_window_assigns_and_notifies():
    store = SessionStore()
    seen = []
    store.on_change(lambda sid: seen.append(sid))
    store.apply_event(evt("SessionStart"))
    seen.clear()
    store.set_bound_window("s1", 0xABCD)
    assert store.get("s1").bound_window_id == 0xABCD
    assert seen == ["s1"]


def test_set_bound_window_noop_when_unchanged():
    store = SessionStore()
    seen = []
    store.on_change(lambda sid: seen.append(sid))
    store.apply_event(evt("SessionStart"))
    store.set_bound_window("s1", 0x99)
    seen.clear()
    store.set_bound_window("s1", 0x99)  # same value
    assert seen == []


def test_set_bound_window_unknown_session_is_noop():
    store = SessionStore()
    seen = []
    store.on_change(lambda sid: seen.append(sid))
    store.set_bound_window("ghost", 0x42)
    assert seen == []


def test_set_bound_window_stores_client_window_id():
    """The store must remember both the frame (bound) and client window ids,
    so the overlay can size itself to the actual terminal content area while
    the daemon still matches ConfigureNotify by frame id."""
    store = SessionStore()
    store.apply_event(evt("SessionStart"))
    store.set_bound_window("s1", 0xFF22, client_window_id=0xCC11)
    s = store.get("s1")
    assert s.bound_window_id == 0xFF22
    assert s.client_window_id == 0xCC11


def test_set_bound_window_clears_client_when_unbinding():
    """Clearing the bound window (window destroyed) must also clear client_window_id."""
    store = SessionStore()
    store.apply_event(evt("SessionStart"))
    store.set_bound_window("s1", 0xFF22, client_window_id=0xCC11)
    store.set_bound_window("s1", None)
    s = store.get("s1")
    assert s.bound_window_id is None
    assert s.client_window_id is None


def test_one_buggy_listener_does_not_break_others():
    store = SessionStore()
    seen = []
    def boom(sid):
        raise RuntimeError("boom")
    store.on_change(boom)
    store.on_change(lambda sid: seen.append(sid))
    # Apply an event; first listener raises, second should still receive.
    store.apply_event(evt("SessionStart"))
    assert seen == ["s1"]


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


# ---------------------------------------------------------------------------
# background_active lifecycle tests
# ---------------------------------------------------------------------------

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


def test_permission_request_does_not_clear_background_active():
    """Same as Notification: PermissionRequest means the user must act,
    but the background task is still alive. The flag stays set; the
    overlay layer applies the override."""
    store = SessionStore()
    store.apply_event(evt("SessionStart"))
    store.apply_event(_post("Monitor", t=2.0))
    store.apply_event(evt("PermissionRequest", t=3.0))
    assert store.get("s1").background_active is True


def test_elicitation_does_not_clear_background_active():
    """Elicitation also leaves background_active alone — the overlay layer
    applies the user-action-overrides-green rule."""
    store = SessionStore()
    store.apply_event(evt("SessionStart"))
    store.apply_event(_post("Monitor", t=2.0))
    store.apply_event(evt("Elicitation", t=3.0))
    assert store.get("s1").background_active is True


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


# ---------------------------------------------------------------------------
# restore() — used by the persistence layer to reconstruct state at startup
# ---------------------------------------------------------------------------


def test_restore_inserts_session_and_notifies():
    store = SessionStore()
    seen = []
    store.on_change(lambda sid: seen.append(sid))
    s = Session(
        session_id="abc",
        cwd="/p",
        claude_pid=1,
        status=Status.WAITING,
        last_event_at=10.0,
        bound_window_id=0xFF,
        client_window_id=0xFF,
        last_event="Stop",
        background_active=False,
    )
    store.restore(s)
    got = store.get("abc")
    assert got is s
    assert seen == ["abc"]


def test_restore_bypasses_state_machine():
    """restore() is for reconstructing post-event state; it must not run
    apply_event-style transitions like background_active clearing."""
    store = SessionStore()
    s = Session(
        session_id="abc",
        cwd="/p",
        claude_pid=1,
        status=Status.WAITING,
        last_event_at=10.0,
        bound_window_id=0xFF,
        client_window_id=0xFF,
        last_event="Stop",
        background_active=True,  # would be cleared by UserPromptSubmit, not Stop
    )
    store.restore(s)
    got = store.get("abc")
    assert got.background_active is True
    assert got.last_event == "Stop"
    assert got.status == Status.WAITING

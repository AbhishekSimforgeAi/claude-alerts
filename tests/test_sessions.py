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

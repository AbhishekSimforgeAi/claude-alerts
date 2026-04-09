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


def test_try_bind_fires_on_change():
    """Binding should fire the SessionStore on_change callback so the renderer can react."""
    store = SessionStore()
    x = FakeX11()
    x.active_window_id = 0xEE
    x.wm_classes[0xEE] = "gnome-terminal-server"
    binder = Binder(store, x)
    store.apply_event(evt("SessionStart"))
    seen = []
    store.on_change(lambda sid: seen.append(sid))
    binder.try_bind("s1")
    assert seen == ["s1"]


def test_unbind_window_clears_binding_for_destroyed_window():
    store = SessionStore()
    x = FakeX11()
    x.active_window_id = 0xFF
    x.wm_classes[0xFF] = "gnome-terminal-server"
    binder = Binder(store, x)
    store.apply_event(evt("SessionStart"))
    binder.try_bind("s1")
    assert store.get("s1").bound_window_id == 0xFF
    binder.unbind_window(0xFF)
    assert store.get("s1").bound_window_id is None


def test_try_bind_handles_x11_exception_on_active_window():
    """If x11.get_active_window_id raises, the session is queued for manual bind, not crashed."""
    class BrokenX11:
        def get_active_window_id(self):
            raise RuntimeError("display connection lost")
        def get_wm_class(self, wid):
            return ""
    store = SessionStore()
    binder = Binder(store, BrokenX11())
    store.apply_event(evt("SessionStart"))
    binder.try_bind("s1")
    assert store.get("s1").bound_window_id is None
    assert binder.pending_manual_binds() == ["s1"]


def test_try_bind_handles_x11_exception_on_wm_class():
    """If x11.get_wm_class raises, the session is queued, not crashed."""
    class PartiallyBrokenX11:
        def get_active_window_id(self):
            return 0xAB
        def get_wm_class(self, wid):
            raise RuntimeError("BadWindow")
    store = SessionStore()
    binder = Binder(store, PartiallyBrokenX11())
    store.apply_event(evt("SessionStart"))
    binder.try_bind("s1")
    assert store.get("s1").bound_window_id is None
    assert binder.pending_manual_binds() == ["s1"]

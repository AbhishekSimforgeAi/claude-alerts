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

    def get_frame_window_id(self, window_id):
        # Identity by default — the wid IS the frame.
        return window_id


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


def test_try_bind_uses_frame_window_id():
    """The binder must store the frame window id, not the client window id."""
    class FrameAwareX11:
        def __init__(self):
            self.active_window_id = 0xCC11
            self.wm_classes = {0xCC11: "gnome-terminal-server"}
            # frame map: client -> frame
            self.frames = {0xCC11: 0xFF22}
        def get_active_window_id(self):
            return self.active_window_id
        def get_wm_class(self, wid):
            return self.wm_classes.get(wid, "")
        def get_frame_window_id(self, wid):
            return self.frames.get(wid)

    store = SessionStore()
    binder = Binder(store, FrameAwareX11())
    store.apply_event(evt("SessionStart"))
    binder.try_bind("s1")
    # Should be the FRAME id, not the client id
    assert store.get("s1").bound_window_id == 0xFF22


def test_try_bind_stores_client_window_id_alongside_frame():
    """The binder must record the original (client) window id so the overlay
    can size itself to the terminal content area, while bound_window_id stays
    as the frame id for substructure event matching."""
    class FrameAwareX11:
        def __init__(self):
            self.active_window_id = 0xCC11
            self.wm_classes = {0xCC11: "gnome-terminal-server"}
            self.frames = {0xCC11: 0xFF22}
        def get_active_window_id(self):
            return self.active_window_id
        def get_wm_class(self, wid):
            return self.wm_classes.get(wid, "")
        def get_frame_window_id(self, wid):
            return self.frames.get(wid)

    store = SessionStore()
    binder = Binder(store, FrameAwareX11())
    store.apply_event(evt("SessionStart"))
    binder.try_bind("s1")
    s = store.get("s1")
    assert s.bound_window_id == 0xFF22
    assert s.client_window_id == 0xCC11


def test_complete_manual_bind_stores_client_window_id():
    """Manual binding should also record the client id (the input window id)
    in addition to resolving the frame for event matching."""
    class FrameAwareX11:
        def get_frame_window_id(self, wid):
            # input is the client; frame is something else.
            return 0xFF22 if wid == 0xCC11 else None

    store = SessionStore()
    binder = Binder(store, FrameAwareX11())
    store.apply_event(evt("SessionStart"))
    binder.complete_manual_bind("s1", 0xCC11)
    s = store.get("s1")
    assert s.bound_window_id == 0xFF22
    assert s.client_window_id == 0xCC11


def test_try_bind_queues_when_frame_resolution_fails():
    """If frame resolution returns None, the session should fall back to manual bind."""
    class NoFrameX11:
        def get_active_window_id(self):
            return 0xAB
        def get_wm_class(self, wid):
            return "gnome-terminal-server"
        def get_frame_window_id(self, wid):
            return None

    store = SessionStore()
    binder = Binder(store, NoFrameX11())
    store.apply_event(evt("SessionStart"))
    binder.try_bind("s1")
    assert store.get("s1").bound_window_id is None
    assert binder.pending_manual_binds() == ["s1"]


def test_forget_session_drops_from_pending():
    """When a session is removed (SessionEnd / eviction), the queue must
    drop its id so a long-running daemon doesn't leak entries."""
    class NonTerminalX11:
        def get_active_window_id(self):
            return 0xAB
        def get_wm_class(self, wid):
            return "google-chrome"

    store = SessionStore()
    binder = Binder(store, NonTerminalX11())
    store.apply_event(evt("SessionStart", session_id="s1"))
    store.apply_event(evt("SessionStart", session_id="s2"))
    binder.try_bind("s1")
    binder.try_bind("s2")
    assert sorted(binder.pending_manual_binds()) == ["s1", "s2"]
    binder.forget_session("s1")
    assert binder.pending_manual_binds() == ["s2"]
    binder.forget_session("never-was-pending")  # silent no-op
    assert binder.pending_manual_binds() == ["s2"]


def test_pending_manual_binds_reaps_dead_sessions():
    """If a session is removed without forget_session being called, the next
    pending_manual_binds() read self-heals — defensive for code paths that
    forget to call forget_session."""
    class NonTerminalX11:
        def get_active_window_id(self):
            return 0xAB
        def get_wm_class(self, wid):
            return "google-chrome"

    store = SessionStore()
    binder = Binder(store, NonTerminalX11())
    store.apply_event(evt("SessionStart", session_id="s1"))
    binder.try_bind("s1")
    # Remove the session at the store level without telling the binder.
    store.apply_event(ClaudeEvent(
        event="SessionEnd", session_id="s1", cwd="/p", claude_pid=1, timestamp=2.0,
    ))
    assert binder.pending_manual_binds() == []

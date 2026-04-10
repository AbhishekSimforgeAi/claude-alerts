"""Import-only smoke test plus targeted unit tests with X11 stubbed.
Real visual behaviour is verified in test_e2e_xvfb.py."""

from claude_alerts.config import Config
from claude_alerts.events import ClaudeEvent
from claude_alerts.sessions import SessionStore
from claude_alerts.x11 import Geometry


def test_overlay_module_imports():
    from claude_alerts import overlay
    assert hasattr(overlay, "OverlayManager")


def test_hex_to_rgb_pure_helper():
    from claude_alerts.overlay import hex_to_rgb
    assert hex_to_rgb("#ff0000") == (0xff, 0x00, 0x00)
    assert hex_to_rgb("#22c55e") == (0x22, 0xc5, 0x5e)
    assert hex_to_rgb("ff0000") == (0xff, 0x00, 0x00)  # leading # is optional


def test_rgb_to_pixel_packing():
    from claude_alerts.overlay import _rgb_to_pixel
    assert _rgb_to_pixel((0xff, 0x00, 0x00)) == 0xff0000
    assert _rgb_to_pixel((0x00, 0xff, 0x00)) == 0x00ff00
    assert _rgb_to_pixel((0x00, 0x00, 0xff)) == 0x0000ff
    assert _rgb_to_pixel((0x22, 0xc5, 0x5e)) == 0x22c55e


# --- Geometry-source tests with _OverlayWindow stubbed --------------------

# Raw client geometry (includes GTK shadow padding for CSD apps).
CLIENT_RAW_GEO = Geometry(x=100, y=200, width=820, height=610)
# Visible decorated window after _GTK_FRAME_EXTENTS=(26,26,23,49) is applied.
CLIENT_VISIBLE_GEO = Geometry(x=126, y=223, width=768, height=538)
FRAME_GEO = Geometry(x=90, y=180, width=840, height=650)

CLIENT_WID = 0xCC11
FRAME_WID = 0xFF22


class _RecordingX11:
    """Fake X11Client returning distinct geometries for client vs frame ids."""
    def __init__(self):
        self.geometry_queries: list[int] = []
        self.visible_queries: list[int] = []

    def get_geometry(self, wid):
        self.geometry_queries.append(wid)
        if wid == CLIENT_WID:
            return CLIENT_RAW_GEO
        if wid == FRAME_WID:
            return FRAME_GEO
        return None

    def get_visible_geometry(self, wid):
        self.visible_queries.append(wid)
        if wid == CLIENT_WID:
            return CLIENT_VISIBLE_GEO
        if wid == FRAME_WID:
            return FRAME_GEO
        return None

    def flush(self):
        pass


class _FakeOverlayWindow:
    """Stand-in for the real X11-touching _OverlayWindow."""
    instances: list["_FakeOverlayWindow"] = []

    def __init__(self, x11, geo, color_pixel, thickness, transient_for_window_id=None):
        self.geo = geo
        self.color_pixel = color_pixel
        self.thickness = thickness
        self.transient_for_window_id = transient_for_window_id
        self.update_calls: list[Geometry] = []
        self.destroyed = False
        _FakeOverlayWindow.instances.append(self)

    def update_geometry(self, geo):
        self.geo = geo
        self.update_calls.append(geo)

    def set_color(self, color_pixel):
        self.color_pixel = color_pixel

    def set_visible(self, visible):
        self.visible = visible

    def destroy(self):
        self.destroyed = True


# --- Real-constructor fixture ------------------------------------------------
# These fakes drive the REAL _OverlayWindow.__init__ to verify the X11
# property writes (override_redirect, transient_for, EWMH/Motif hints).


class _FakeWindow:
    def __init__(self):
        self.property_writes: list[tuple] = []
        self.shape_calls: list = []
        self.mapped: bool = False

    def change_property(self, prop_atom, type_atom, fmt, data, mode=0):
        self.property_writes.append((prop_atom, type_atom, fmt, list(data)))

    def shape_rectangles(self, *args, **kwargs):
        self.shape_calls.append((args, kwargs))

    def map(self):
        self.mapped = True

    def unmap(self):
        self.mapped = False

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
        self._NET_WM_WINDOW_TYPE = 1001
        self._NET_WM_WINDOW_TYPE_UTILITY = 1002
        self._NET_WM_STATE = 1003
        self._NET_WM_STATE_SKIP_TASKBAR = 1004
        self._NET_WM_STATE_SKIP_PAGER = 1005
        self._MOTIF_WM_HINTS = 1006

    def flush(self):
        pass


def _build_real_overlay_window(transient_for=0xFF22):
    from claude_alerts.overlay import _OverlayWindow
    fake_x11 = _FakeX11ForCtor()
    geo = Geometry(x=10, y=20, width=400, height=300)
    ow = _OverlayWindow(
        fake_x11, geo, color_pixel=0xff0000, thickness=4,
        transient_for_window_id=transient_for,
    )
    return ow, fake_x11.screen.root, fake_x11.screen.root.created_window


def test_overlay_window_is_not_override_redirect():
    _, root, _ = _build_real_overlay_window()
    assert root.last_create_kwargs.get("override_redirect", 0) == 0


def test_overlay_window_does_not_request_visibility_events():
    from Xlib import X as Xconst
    _, root, _ = _build_real_overlay_window()
    mask = root.last_create_kwargs.get("event_mask", 0)
    assert mask & Xconst.VisibilityChangeMask == 0


def test_overlay_sets_transient_for_terminal_frame():
    from Xlib import Xatom
    _, _, fake_win = _build_real_overlay_window(transient_for=0xFF22)
    transient_writes = [
        w for w in fake_win.property_writes
        if w[0] == Xatom.WM_TRANSIENT_FOR
    ]
    assert len(transient_writes) == 1
    _, type_atom, _, data = transient_writes[0]
    assert type_atom == Xatom.WINDOW
    assert data == [0xFF22]


def test_overlay_sets_utility_window_type():
    _, _, fake_win = _build_real_overlay_window()
    type_writes = [
        w for w in fake_win.property_writes
        if w[0] == 1001  # _NET_WM_WINDOW_TYPE
    ]
    assert len(type_writes) == 1
    _, _, _, data = type_writes[0]
    assert data == [1002]  # _NET_WM_WINDOW_TYPE_UTILITY


def test_overlay_sets_skip_taskbar_and_pager():
    _, _, fake_win = _build_real_overlay_window()
    state_writes = [
        w for w in fake_win.property_writes
        if w[0] == 1003  # _NET_WM_STATE
    ]
    assert len(state_writes) == 1
    _, _, _, data = state_writes[0]
    assert 1004 in data  # SKIP_TASKBAR
    assert 1005 in data  # SKIP_PAGER


def test_overlay_sets_motif_no_decorations():
    _, _, fake_win = _build_real_overlay_window()
    motif_writes = [
        w for w in fake_win.property_writes
        if w[0] == 1006  # _MOTIF_WM_HINTS
    ]
    assert len(motif_writes) == 1
    _, _, _, data = motif_writes[0]
    assert len(data) == 5
    assert data[0] == 2   # flags = decorations bit
    assert data[2] == 0   # decorations = none


def test_overlay_manager_has_no_raise_all_method():
    from claude_alerts.overlay import OverlayManager
    assert not hasattr(OverlayManager, "raise_all")


# --- Geometry-source tests with _OverlayWindow stubbed --------------------


def _make_manager(monkeypatch):
    from claude_alerts import overlay as overlay_mod
    _FakeOverlayWindow.instances = []
    monkeypatch.setattr(overlay_mod, "_OverlayWindow", _FakeOverlayWindow)
    store = SessionStore()
    x11 = _RecordingX11()
    mgr = overlay_mod.OverlayManager(x11, store, Config())
    return mgr, store, x11


def _start_session(store):
    store.apply_event(ClaudeEvent(
        event="SessionStart", session_id="s1", cwd="/p", claude_pid=1, timestamp=1.0,
    ))


def test_overlay_creates_window_at_visible_client_geometry(monkeypatch):
    """The overlay must use the *visible* client geometry (with _GTK_FRAME_EXTENTS
    subtracted), not the raw client geometry which on CSD apps includes the
    drop shadow. This is the whole point of 'border is part of the window,
    not padded': the colored edges should sit at the visible terminal edges."""
    mgr, store, x11 = _make_manager(monkeypatch)
    _start_session(store)
    store.set_bound_window("s1", FRAME_WID, client_window_id=CLIENT_WID)

    assert len(_FakeOverlayWindow.instances) == 1
    overlay_win = _FakeOverlayWindow.instances[0]
    assert overlay_win.geo == CLIENT_VISIBLE_GEO
    assert CLIENT_WID in x11.visible_queries
    assert FRAME_WID not in x11.visible_queries


def test_on_window_configure_refetches_visible_client_geometry(monkeypatch):
    """ConfigureNotify on root substructure delivers events for the FRAME window.
    When the frame moves, we must re-query the visible client geometry rather
    than reuse the frame's geo from the event."""
    mgr, store, x11 = _make_manager(monkeypatch)
    _start_session(store)
    store.set_bound_window("s1", FRAME_WID, client_window_id=CLIENT_WID)
    overlay_win = _FakeOverlayWindow.instances[0]
    overlay_win.update_calls.clear()
    x11.visible_queries.clear()

    new_frame_geo = Geometry(x=300, y=400, width=820, height=610)
    mgr.on_window_configure(FRAME_WID, new_frame_geo)

    assert overlay_win.update_calls == [CLIENT_VISIBLE_GEO]
    assert x11.visible_queries == [CLIENT_WID]


def test_refresh_all_geometry_uses_visible_client_geometry(monkeypatch):
    """refresh_all_geometry should also use the visible client geometry."""
    mgr, store, x11 = _make_manager(monkeypatch)
    _start_session(store)
    store.set_bound_window("s1", FRAME_WID, client_window_id=CLIENT_WID)
    x11.visible_queries.clear()

    mgr.refresh_all_geometry()

    assert x11.visible_queries == [CLIENT_WID]


def test_overlay_destroyed_on_unbind(monkeypatch):
    """Sanity check that unbinding still tears down the overlay (no regression
    from the new client_window_id field)."""
    mgr, store, x11 = _make_manager(monkeypatch)
    _start_session(store)
    store.set_bound_window("s1", FRAME_WID, client_window_id=CLIENT_WID)
    overlay_win = _FakeOverlayWindow.instances[0]
    store.set_bound_window("s1", None)
    assert overlay_win.destroyed
    assert not mgr.has_overlay("s1")


def test_waiting_overlay_blinks(monkeypatch):
    """WAITING overlays should toggle visibility on tick_blink."""
    import time as _time
    mgr, store, x11 = _make_manager(monkeypatch)
    _start_session(store)
    store.set_bound_window("s1", FRAME_WID, client_window_id=CLIENT_WID)
    # Session starts as WAITING (SessionStart maps to WAITING).
    overlay_win = _FakeOverlayWindow.instances[0]

    # Force enough time to pass for a blink tick.
    mgr._last_blink = _time.monotonic() - 1.0
    mgr.tick_blink()
    assert overlay_win.visible is False

    mgr._last_blink = _time.monotonic() - 1.0
    mgr.tick_blink()
    assert overlay_win.visible is True


def test_working_overlay_stays_visible_on_blink(monkeypatch):
    """WORKING overlays must not blink — they stay visible regardless."""
    import time as _time
    mgr, store, x11 = _make_manager(monkeypatch)
    _start_session(store)
    store.set_bound_window("s1", FRAME_WID, client_window_id=CLIENT_WID)
    # Transition to WORKING.
    store.apply_event(ClaudeEvent(
        event="UserPromptSubmit", session_id="s1", cwd="/p", claude_pid=1, timestamp=2.0,
    ))
    overlay_win = _FakeOverlayWindow.instances[0]

    mgr._last_blink = _time.monotonic() - 1.0
    mgr.tick_blink()
    # WORKING overlay should not have set_visible called to False.
    assert not hasattr(overlay_win, "visible") or overlay_win.visible is True

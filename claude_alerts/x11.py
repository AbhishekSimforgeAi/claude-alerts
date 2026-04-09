"""X11 connection helpers — a thin wrapper around python-xlib for the operations we need."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Optional

from Xlib import X, display
from Xlib import error as xerr

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Geometry:
    x: int
    y: int
    width: int
    height: int


def wm_class_string(wm_class: Optional[Iterable[str]]) -> str:
    """Convert a WM_CLASS tuple (instance, class) into the instance string. Empty if absent."""
    if not wm_class:
        return ""
    items = list(wm_class)
    if not items:
        return ""
    return str(items[0])


class X11Client:
    """Owns the connection to the X server. One per daemon process."""

    def __init__(self) -> None:
        self.display = display.Display()
        self.screen = self.display.screen()
        self.root = self.screen.root
        self._NET_ACTIVE_WINDOW = self.display.intern_atom("_NET_ACTIVE_WINDOW")
        self._NET_CLIENT_LIST = self.display.intern_atom("_NET_CLIENT_LIST")
        self._NET_WM_STATE = self.display.intern_atom("_NET_WM_STATE")
        self._NET_WM_STATE_ABOVE = self.display.intern_atom("_NET_WM_STATE_ABOVE")

    def fileno(self) -> int:
        return self.display.fileno()

    def flush(self) -> None:
        self.display.flush()

    def get_active_window_id(self) -> Optional[int]:
        prop = self.root.get_full_property(self._NET_ACTIVE_WINDOW, X.AnyPropertyType)
        if not prop or not prop.value:
            return None
        wid = int(prop.value[0])
        return wid or None

    def get_wm_class(self, window_id: int) -> str:
        try:
            win = self.display.create_resource_object("window", window_id)
            cls = win.get_wm_class()
            return wm_class_string(cls)
        except (xerr.XError, ConnectionError) as e:
            log.debug("get_wm_class(%#x) failed: %s", window_id, e)
            return ""

    def get_geometry(self, window_id: int) -> Optional[Geometry]:
        try:
            win = self.display.create_resource_object("window", window_id)
            geo = win.get_geometry()
            # Translate to root coordinates
            coords = win.translate_coords(self.root, 0, 0)
            return Geometry(
                x=-coords.x, y=-coords.y, width=geo.width, height=geo.height,
            )
        except (xerr.XError, ConnectionError) as e:
            log.debug("get_geometry(%#x) failed: %s", window_id, e)
            return None

    def list_top_level_windows(self) -> list[int]:
        prop = self.root.get_full_property(self._NET_CLIENT_LIST, X.AnyPropertyType)
        if not prop:
            return []
        return [int(w) for w in prop.value]

    def subscribe_root_substructure(self) -> None:
        """Receive ConfigureNotify and DestroyNotify for all top-level windows."""
        self.root.change_attributes(event_mask=X.SubstructureNotifyMask)
        self.display.flush()

    def get_frame_window_id(self, client_window_id: int) -> Optional[int]:
        """Walk up the window tree to find the topmost ancestor that is a direct child of root.

        On reparenting window managers (GNOME Shell / Mutter, KWin, etc.), client windows
        are nested inside frame windows owned by the WM. _NET_ACTIVE_WINDOW returns the client
        window ID, but SubstructureNotifyMask on root delivers events for the frame window.
        To make ConfigureNotify and DestroyNotify match what the binder stored, the binder
        must record the frame window ID, not the client ID. This helper resolves it.

        Returns the frame window ID, or None if the input is invalid or already root.
        """
        try:
            win = self.display.create_resource_object("window", client_window_id)
            root_id = self.root.id
            for _ in range(32):  # generous bound to prevent infinite loops on broken WMs
                tree = win.query_tree()
                parent = tree.parent
                if parent is None:
                    return None
                if parent.id == root_id:
                    return win.id
                win = parent
            return None
        except (xerr.XError, ConnectionError) as e:
            log.debug("get_frame_window_id(%#x) failed: %s", client_window_id, e)
            return None

    def pending_events(self) -> int:
        return self.display.pending_events()

    def next_event(self):
        return self.display.next_event()

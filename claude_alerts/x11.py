"""X11 connection helpers — a thin wrapper around python-xlib for the operations we need."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from Xlib import X, display
from Xlib.protocol import event as xevent


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
        except Exception:
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
        except Exception:
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

    def pending_events(self) -> int:
        return self.display.pending_events()

    def next_event(self):
        return self.display.next_event()

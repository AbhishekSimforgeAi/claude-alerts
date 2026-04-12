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


def inset_by_frame_extents(
    geo: "Geometry", extents: tuple[int, int, int, int]
) -> "Geometry":
    """Shrink a window's geometry by its _GTK_FRAME_EXTENTS shadow padding.

    CSD apps (gnome-terminal, GNOME Console, etc.) draw their own decorations
    inside the client window and reserve outer padding for the drop shadow.
    The shadow area is part of the X11 client window's geometry but is
    visually transparent — drawing a border at the client edge leaves a gap
    around the visible window. Subtracting these extents gives the visible
    decorated window edges.

    extents is the (left, right, top, bottom) CARDINAL[4] from the property.
    """
    left, right, top, bottom = extents
    return Geometry(
        x=geo.x + left,
        y=geo.y + top,
        width=max(1, geo.width - left - right),
        height=max(1, geo.height - top - bottom),
    )


class X11Client:
    """Owns the connection to the X server. One per daemon process."""

    def __init__(self) -> None:
        self.display = display.Display()
        self.screen = self.display.screen()
        self.root = self.screen.root
        self._NET_ACTIVE_WINDOW = self.display.intern_atom("_NET_ACTIVE_WINDOW")
        self._NET_CLIENT_LIST = self.display.intern_atom("_NET_CLIENT_LIST")
        self._NET_WM_STATE = self.display.intern_atom("_NET_WM_STATE")
        self._GTK_FRAME_EXTENTS = self.display.intern_atom("_GTK_FRAME_EXTENTS")
        self._NET_WM_WINDOW_TYPE = self.display.intern_atom("_NET_WM_WINDOW_TYPE")
        self._NET_WM_WINDOW_TYPE_UTILITY = self.display.intern_atom("_NET_WM_WINDOW_TYPE_UTILITY")
        self._NET_WM_STATE_SKIP_TASKBAR = self.display.intern_atom("_NET_WM_STATE_SKIP_TASKBAR")
        self._NET_WM_STATE_SKIP_PAGER = self.display.intern_atom("_NET_WM_STATE_SKIP_PAGER")
        self._MOTIF_WM_HINTS = self.display.intern_atom("_MOTIF_WM_HINTS")

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

    def get_gtk_frame_extents(self, window_id: int) -> tuple[int, int, int, int]:
        """Read _GTK_FRAME_EXTENTS (left, right, top, bottom) for a CSD client.

        Returns (0, 0, 0, 0) if the property is unset, malformed, or the read
        fails — which is the right answer for non-CSD clients (xterm, etc.).
        """
        try:
            win = self.display.create_resource_object("window", window_id)
            prop = win.get_full_property(self._GTK_FRAME_EXTENTS, X.AnyPropertyType)
            if prop is not None and len(prop.value) >= 4:
                vals = list(prop.value)[:4]
                return (int(vals[0]), int(vals[1]), int(vals[2]), int(vals[3]))
        except (xerr.XError, ConnectionError) as e:
            log.debug("get_gtk_frame_extents(%#x) failed: %s", window_id, e)
        return (0, 0, 0, 0)

    def get_visible_geometry(self, window_id: int) -> Optional[Geometry]:
        """Geometry of the visible portion of a window.

        For CSD apps this excludes the _GTK_FRAME_EXTENTS drop-shadow padding;
        for plain SSD or borderless apps it returns the same value as
        get_geometry. This is what overlays should size themselves to.
        """
        geo = self.get_geometry(window_id)
        if geo is None:
            return None
        return inset_by_frame_extents(geo, self.get_gtk_frame_extents(window_id))

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

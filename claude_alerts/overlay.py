"""X11 click-through border overlays for bound terminal windows."""
from __future__ import annotations

import logging

from Xlib import X, Xatom
from Xlib.ext import shape

from claude_alerts.config import Config
from claude_alerts.sessions import Session, SessionStore, Status
from claude_alerts.x11 import Geometry, X11Client

log = logging.getLogger(__name__)


def hex_to_rgb(s: str) -> tuple[int, int, int]:
    s = s.lstrip("#")
    if len(s) != 6:
        raise ValueError(f"bad hex color: {s}")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def _rgb_to_pixel(rgb: tuple[int, int, int]) -> int:
    r, g, b = rgb
    return (r << 16) | (g << 8) | b


class _OverlayWindow:
    """One overlay window tracking one terminal."""

    def __init__(
        self,
        x11: X11Client,
        target_geo: Geometry,
        color_pixel: int,
        thickness: int,
        transient_for_window_id: int,
    ) -> None:
        self.x11 = x11
        self.thickness = thickness
        self.color_pixel = color_pixel
        self.win = x11.screen.root.create_window(
            target_geo.x, target_geo.y, target_geo.width, target_geo.height, 0,
            X.CopyFromParent, X.InputOutput, X.CopyFromParent,
            background_pixel=color_pixel,
            event_mask=X.ExposureMask,
        )
        self._apply_shape(target_geo)
        # Set WM hints so the overlay stacks with its terminal, has no
        # decorations, and stays out of taskbar / alt-tab.
        try:
            self.win.change_property(
                Xatom.WM_TRANSIENT_FOR, Xatom.WINDOW, 32,
                [transient_for_window_id],
            )
            self.win.change_property(
                x11._NET_WM_WINDOW_TYPE, Xatom.ATOM, 32,
                [x11._NET_WM_WINDOW_TYPE_UTILITY],
            )
            self.win.change_property(
                x11._NET_WM_STATE, Xatom.ATOM, 32,
                [x11._NET_WM_STATE_SKIP_TASKBAR, x11._NET_WM_STATE_SKIP_PAGER],
            )
            # _MOTIF_WM_HINTS: flags=2 (decorations bit), functions=0, decorations=0,
            # input_mode=0, status=0 — suppress titlebar/border.
            self.win.change_property(
                x11._MOTIF_WM_HINTS, x11._MOTIF_WM_HINTS, 32,
                [2, 0, 0, 0, 0],
            )
        except Exception as e:
            log.debug("failed to set WM hints on overlay: %s", e)
        self.win.map()
        x11.flush()

    def _apply_shape(self, geo: Geometry) -> None:
        """Restrict the overlay's input region to just the border edges so clicks pass through the middle."""
        t = self.thickness
        # Clamp thickness to half the smaller dimension so we never produce negative-y rects.
        t = min(t, max(1, geo.width // 2), max(1, geo.height // 2))
        edges = [
            (0, 0, geo.width, t),                              # top
            (0, geo.height - t, geo.width, t),                 # bottom
            (0, 0, t, geo.height),                             # left
            (geo.width - t, 0, t, geo.height),                 # right
        ]
        # Set both bounding and input shapes to the four edge rectangles.
        # Bounding: restricts the visible (and hit-test) region to just the borders.
        # Input: restricts where clicks are received — clicks on the transparent middle pass through.
        self.win.shape_rectangles(
            shape.SO.Set, shape.SK.Bounding, X.Unsorted, 0, 0, edges,
        )
        self.win.shape_rectangles(
            shape.SO.Set, shape.SK.Input, X.Unsorted, 0, 0, edges,
        )

    def update_geometry(self, geo: Geometry) -> None:
        self.win.configure(x=geo.x, y=geo.y, width=geo.width, height=geo.height)
        self._apply_shape(geo)
        self.x11.flush()

    def set_color(self, color_pixel: int) -> None:
        self.color_pixel = color_pixel
        self.win.change_attributes(background_pixel=color_pixel)
        self.win.clear_area(0, 0, 0, 0, True)
        self.x11.flush()

    def destroy(self) -> None:
        try:
            self.win.destroy()
            self.x11.flush()
        except Exception as e:
            log.debug("overlay window destroy failed (already gone?): %s", e)


class OverlayManager:
    """One per daemon. Maintains an _OverlayWindow per bound session."""

    def __init__(self, x11: X11Client, store: SessionStore, config: Config) -> None:
        self.x11 = x11
        self.store = store
        self.config = config
        self._overlays: dict[str, _OverlayWindow] = {}
        self._working_pixel = _rgb_to_pixel(hex_to_rgb(config.color_working))
        self._waiting_pixel = _rgb_to_pixel(hex_to_rgb(config.color_waiting))
        store.on_change(self.on_session_changed)

    def color_for(self, status: Status) -> int:
        return self._working_pixel if status == Status.WORKING else self._waiting_pixel

    def on_session_changed(self, session_id: str) -> None:
        session = self.store.get(session_id)
        if session is None:
            self._destroy(session_id)
            return
        self._sync_one(session)

    def on_window_configure(self, window_id: int, geo: Geometry) -> None:
        # ConfigureNotify on root substructure delivers events for the FRAME
        # (the top-level child of root), but the overlay is sized to the inner
        # client window so the border hugs the actual terminal content. When the
        # frame moves, the client moves with it but its root coordinates change,
        # so we re-fetch the client's geometry rather than reusing the frame geo.
        for s in self.store.all():
            if s.bound_window_id == window_id and s.session_id in self._overlays:
                client_geo = self._client_geometry(s)
                if client_geo is not None:
                    self._overlays[s.session_id].update_geometry(client_geo)

    def refresh_all_geometry(self) -> None:
        for s in self.store.all():
            if s.bound_window_id and s.session_id in self._overlays:
                geo = self._client_geometry(s)
                if geo is not None:
                    self._overlays[s.session_id].update_geometry(geo)

    def _client_geometry(self, session: Session) -> Geometry | None:
        """Visible geometry of the bound window.

        Uses the client window id (the EWMH active window) and applies
        _GTK_FRAME_EXTENTS so CSD apps' drop-shadow padding is excluded.
        Falls back to the frame id when client_window_id is unset (e.g.
        legacy sessions or non-reparenting environments).
        """
        target = session.client_window_id or session.bound_window_id
        if target is None:
            return None
        return self.x11.get_visible_geometry(target)

    def has_overlay(self, session_id: str) -> bool:
        return session_id in self._overlays

    def _sync_one(self, session: Session) -> None:
        if session.bound_window_id is None:
            self._destroy(session.session_id)
            return
        geo = self._client_geometry(session)
        if geo is None:
            self._destroy(session.session_id)
            return
        existing = self._overlays.get(session.session_id)
        color_pixel = self.color_for(session.status)
        if existing is None:
            self._overlays[session.session_id] = _OverlayWindow(
                self.x11, geo, color_pixel, self.config.border_thickness_px,
                transient_for_window_id=session.bound_window_id,
            )
        else:
            existing.update_geometry(geo)
            existing.set_color(color_pixel)

    def _destroy(self, session_id: str) -> None:
        ov = self._overlays.pop(session_id, None)
        if ov is not None:
            ov.destroy()

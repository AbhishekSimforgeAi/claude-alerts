"""Pure colour utilities — no X11, no Xlib, easy to test in isolation.

Two responsibilities:

- ``dim_hex(hex_str, ratio)`` derives a dimmer shade from a focused colour.
- ``pick_color_pixel(...)`` is the single source of truth for which pixel a
  bound overlay should paint, given session state and focus. The overlay
  manager wraps this with the actual session-to-state translation.
"""
from __future__ import annotations

from claude_alerts.sessions import Status

# Events whose arrival means the user must take action before Claude can
# continue — the overlay paints the WAITING colour even when a background
# task is alive. Imported here (instead of from sessions) to keep this
# module's policy self-contained.
_USER_ACTION_EVENTS = frozenset({
    "Notification",
    "PermissionRequest",
    "Elicitation",
})


def dim_hex(hex_str: str, ratio: float) -> str:
    """Return a dimmer hex colour derived from ``hex_str``.

    Each RGB channel is multiplied by ``ratio`` clamped to ``[0, 1]``. The
    output is always in lowercase ``#rrggbb`` form. Raises ``ValueError`` on
    a malformed input string.
    """
    s = hex_str.lstrip("#")
    if len(s) != 6:
        raise ValueError(f"bad hex color: {hex_str!r}")
    try:
        r = int(s[0:2], 16)
        g = int(s[2:4], 16)
        b = int(s[4:6], 16)
    except ValueError as e:
        raise ValueError(f"bad hex color: {hex_str!r}") from e

    clamped = max(0.0, min(1.0, ratio))
    r = int(r * clamped)
    g = int(g * clamped)
    b = int(b * clamped)
    return f"#{r:02x}{g:02x}{b:02x}"


def pick_color_pixel(
    *,
    status: Status,
    last_event: str,
    background_active: bool,
    is_focused: bool,
    working_pixel: int,
    waiting_pixel: int,
    working_dim_pixel: int,
    waiting_dim_pixel: int,
) -> int:
    """Decide which X pixel a bound overlay should paint.

    The status / background_active / last_event policy is exactly the one
    that lived on ``OverlayManager.color_for`` before #11. Focus is layered
    on top: focused windows paint the emissive pixel, unfocused ones paint
    the dim variant.
    """
    if status == Status.WORKING:
        return working_pixel if is_focused else working_dim_pixel

    # status == WAITING from here.
    if background_active and last_event not in _USER_ACTION_EVENTS:
        return working_pixel if is_focused else working_dim_pixel
    return waiting_pixel if is_focused else waiting_dim_pixel

"""Pure-function tests for colour dimming and the focus-aware policy picker."""
from __future__ import annotations

import pytest

from claude_alerts.colors import dim_hex, pick_color_pixel
from claude_alerts.sessions import Status


# -------------------------- dim_hex --------------------------


def test_dim_hex_ratio_one_is_identity():
    assert dim_hex("#ff0000", 1.0) == "#ff0000"
    assert dim_hex("#22c55e", 1.0) == "#22c55e"


def test_dim_hex_ratio_zero_is_black():
    assert dim_hex("#ff0000", 0.0) == "#000000"
    assert dim_hex("#22c55e", 0.0) == "#000000"


def test_dim_hex_half_halves_each_channel():
    # int-truncates toward zero per channel:
    # 0xff = 255 * 0.5 = 127.5 -> 127 = 0x7f
    # 0x80 = 128 * 0.5 = 64.0  -> 64  = 0x40
    # 0x40 = 64  * 0.5 = 32.0  -> 32  = 0x20
    assert dim_hex("#ff0000", 0.5) == "#7f0000"
    assert dim_hex("#80ff40", 0.5) == "#407f20"


def test_dim_hex_quarter_for_default_dim_ratio():
    # 0x22 * 0.25 = 8.5 -> 8 = 0x08
    # 0xc5 * 0.25 = 49.25 -> 49 = 0x31
    # 0x5e * 0.25 = 23.5 -> 23 = 0x17
    assert dim_hex("#22c55e", 0.25) == "#083117"


def test_dim_hex_accepts_no_leading_hash():
    assert dim_hex("ff0000", 1.0) == "#ff0000"


def test_dim_hex_clamps_ratio_above_one():
    # Out-of-range ratios clamp into [0, 1] rather than producing > 0xff channels.
    assert dim_hex("#808080", 5.0) == "#808080"


def test_dim_hex_clamps_negative_ratio():
    assert dim_hex("#808080", -0.5) == "#000000"


def test_dim_hex_rejects_bad_hex():
    with pytest.raises(ValueError):
        dim_hex("#zzz000", 0.5)
    with pytest.raises(ValueError):
        dim_hex("#abcde", 0.5)  # 5 chars
    with pytest.raises(ValueError):
        dim_hex("", 0.5)


# -------------------------- pick_color_pixel --------------------------
#
# The policy mirrors the pre-#11 OverlayManager.color_for logic, plus a
# focus dimension. The four pixel arguments are arbitrary integer sentinels
# for testing — the function is just a multi-way switch returning one of them.

W = 0xAA0001       # working
WD = 0xAA0002      # working_dim
R = 0xBB0001       # waiting
RD = 0xBB0002      # waiting_dim


def _pick(*, status, last_event="", background_active=False, is_focused=True):
    return pick_color_pixel(
        status=status,
        last_event=last_event,
        background_active=background_active,
        is_focused=is_focused,
        working_pixel=W,
        waiting_pixel=R,
        working_dim_pixel=WD,
        waiting_dim_pixel=RD,
    )


def test_pick_working_focused_is_working():
    assert _pick(status=Status.WORKING, last_event="UserPromptSubmit") == W


def test_pick_working_unfocused_is_working_dim():
    assert _pick(status=Status.WORKING, last_event="UserPromptSubmit", is_focused=False) == WD


def test_pick_waiting_focused_is_waiting():
    assert _pick(status=Status.WAITING, last_event="Stop") == R


def test_pick_waiting_unfocused_is_waiting_dim():
    assert _pick(status=Status.WAITING, last_event="Stop", is_focused=False) == RD


def test_pick_waiting_with_background_active_is_working_when_focused():
    """Stop after Monitor: stays green (working) while focused."""
    assert _pick(
        status=Status.WAITING,
        last_event="Stop",
        background_active=True,
    ) == W


def test_pick_waiting_with_background_active_is_working_dim_when_unfocused():
    assert _pick(
        status=Status.WAITING,
        last_event="Stop",
        background_active=True,
        is_focused=False,
    ) == WD


def test_pick_notification_with_background_active_is_waiting_when_focused():
    """User-action event overrides green-during-pause."""
    assert _pick(
        status=Status.WAITING,
        last_event="Notification",
        background_active=True,
    ) == R


def test_pick_notification_with_background_active_is_waiting_dim_when_unfocused():
    assert _pick(
        status=Status.WAITING,
        last_event="Notification",
        background_active=True,
        is_focused=False,
    ) == RD


def test_pick_permission_request_with_background_active_is_waiting_when_focused():
    assert _pick(
        status=Status.WAITING,
        last_event="PermissionRequest",
        background_active=True,
    ) == R


def test_pick_elicitation_with_background_active_is_waiting_when_focused():
    assert _pick(
        status=Status.WAITING,
        last_event="Elicitation",
        background_active=True,
    ) == R

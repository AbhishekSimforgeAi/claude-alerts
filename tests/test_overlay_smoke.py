"""Import-only smoke test. Real behaviour is verified in test_e2e_xvfb.py."""

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

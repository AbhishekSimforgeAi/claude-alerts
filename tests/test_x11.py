from claude_alerts.x11 import Geometry, inset_by_frame_extents, wm_class_string


def test_wm_class_string_basic():
    assert wm_class_string(("gnome-terminal-server", "Gnome-terminal")) == "gnome-terminal-server"


def test_wm_class_string_handles_none():
    assert wm_class_string(None) == ""


def test_wm_class_string_handles_empty():
    assert wm_class_string(()) == ""


def test_module_exposes_expected_api():
    from claude_alerts import x11
    for name in ("X11Client", "wm_class_string", "Geometry"):
        assert hasattr(x11, name), f"missing {name}"


def test_inset_by_frame_extents_subtracts_csd_shadow():
    """_GTK_FRAME_EXTENTS reports (left, right, top, bottom) shadow padding that
    the CSD client window extends INTO. To get the visible window we shrink the
    geometry by those extents and shift the origin inward."""
    geo = Geometry(x=100, y=200, width=820, height=610)
    visible = inset_by_frame_extents(geo, (26, 26, 23, 49))
    assert visible == Geometry(x=126, y=223, width=768, height=538)


def test_inset_by_frame_extents_zero_extents_is_identity():
    geo = Geometry(x=10, y=20, width=400, height=300)
    assert inset_by_frame_extents(geo, (0, 0, 0, 0)) == geo


def test_inset_by_frame_extents_clamps_to_at_least_one_pixel():
    """Pathological extents shouldn't produce zero/negative dimensions."""
    geo = Geometry(x=0, y=0, width=10, height=10)
    out = inset_by_frame_extents(geo, (50, 50, 50, 50))
    assert out.width >= 1
    assert out.height >= 1

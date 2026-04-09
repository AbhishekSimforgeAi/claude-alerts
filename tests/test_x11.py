from claude_alerts.x11 import wm_class_string


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

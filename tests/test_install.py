import json
import sys
from pathlib import Path

import pytest

import importlib.util
spec = importlib.util.spec_from_file_location(
    "install_hooks",
    Path(__file__).resolve().parents[1] / "scripts" / "install-hooks.py",
)
install_hooks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install_hooks)


HOOK_PATH = "/abs/path/to/emit-event.sh"
HOOK_EVENTS = (
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "Stop", "Notification", "SessionEnd",
)


def test_creates_settings_when_absent(tmp_path):
    settings = tmp_path / "settings.json"
    install_hooks.merge_hooks_into(settings, HOOK_PATH)
    data = json.loads(settings.read_text())
    assert "hooks" in data
    for evt in HOOK_EVENTS:
        assert evt in data["hooks"]
        assert any(HOOK_PATH in str(item) for item in data["hooks"][evt])


def test_preserves_unrelated_settings(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"theme": "dark", "model": "claude-opus-4-6"}))
    install_hooks.merge_hooks_into(settings, HOOK_PATH)
    data = json.loads(settings.read_text())
    assert data["theme"] == "dark"
    assert data["model"] == "claude-opus-4-6"
    assert "hooks" in data


def test_idempotent_no_duplicates(tmp_path):
    settings = tmp_path / "settings.json"
    install_hooks.merge_hooks_into(settings, HOOK_PATH)
    install_hooks.merge_hooks_into(settings, HOOK_PATH)
    data = json.loads(settings.read_text())
    for evt in HOOK_EVENTS:
        matches = [i for i in data["hooks"][evt] if HOOK_PATH in str(i)]
        assert len(matches) == 1, f"event {evt} got {len(matches)} matches"


def test_preserves_other_users_hooks(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "PreToolUse": [
                {"hooks": [{"type": "command", "command": "/other/hook.sh"}]}
            ]
        }
    }))
    install_hooks.merge_hooks_into(settings, HOOK_PATH)
    data = json.loads(settings.read_text())
    pretool = data["hooks"]["PreToolUse"]
    assert any("/other/hook.sh" in json.dumps(item) for item in pretool)
    assert any(HOOK_PATH in json.dumps(item) for item in pretool)


def test_handles_hooks_as_list(tmp_path):
    """If existing settings has 'hooks': [], we should not crash; we should reset to dict."""
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": [], "theme": "dark"}))
    install_hooks.merge_hooks_into(settings, HOOK_PATH)
    data = json.loads(settings.read_text())
    assert data["theme"] == "dark"
    assert isinstance(data["hooks"], dict)
    for evt in HOOK_EVENTS:
        assert evt in data["hooks"]


def test_handles_event_as_string(tmp_path):
    """If a hook event value is a string instead of a list, we should reset that event to a list."""
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {"PreToolUse": "this-should-be-a-list"}
    }))
    install_hooks.merge_hooks_into(settings, HOOK_PATH)
    data = json.loads(settings.read_text())
    assert isinstance(data["hooks"]["PreToolUse"], list)
    # Our hook is now in the list
    assert any(HOOK_PATH in json.dumps(item) for item in data["hooks"]["PreToolUse"])


def test_idempotency_does_not_false_positive_on_path_prefix(tmp_path):
    """A different hook whose command happens to share a prefix with hook_path must not block install."""
    settings = tmp_path / "settings.json"
    other_command = HOOK_PATH + "-something-else.sh PreToolUse"
    settings.write_text(json.dumps({
        "hooks": {
            "PreToolUse": [
                {"hooks": [{"type": "command", "command": other_command}]}
            ]
        }
    }))
    install_hooks.merge_hooks_into(settings, HOOK_PATH)
    data = json.loads(settings.read_text())
    pretool = data["hooks"]["PreToolUse"]
    # Both the prefix-sharing hook AND our hook should be present
    assert any(other_command in json.dumps(item) for item in pretool)
    target_command = f"{HOOK_PATH} PreToolUse"
    assert any(
        any(h.get("command") == target_command for h in item.get("hooks", []))
        for item in pretool
    )


def test_preserves_file_permissions(tmp_path):
    """If settings.json was 0600, it should remain 0600 after merge."""
    import stat
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"existing": "data"}))
    settings.chmod(0o600)
    install_hooks.merge_hooks_into(settings, HOOK_PATH)
    mode = stat.S_IMODE(settings.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"

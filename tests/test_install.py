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

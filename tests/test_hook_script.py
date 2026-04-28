import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

HOOK_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "emit-event.sh"


def has_jq():
    return shutil.which("jq") is not None


@pytest.mark.skipif(not has_jq(), reason="jq is required by the hook script")
def test_hook_script_writes_event_file(tmp_path):
    env = os.environ.copy()
    env["CLAUDE_ALERTS_EVENTS_DIR"] = str(tmp_path)
    payload = json.dumps({"session_id": "abc123", "cwd": "/proj"})
    subprocess.run(
        ["bash", str(HOOK_SCRIPT), "PreToolUse"],
        input=payload, text=True, env=env, check=True,
    )
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["event"] == "PreToolUse"
    assert data["session_id"] == "abc123"
    assert data["cwd"] == "/proj"
    assert "claude_pid" in data
    assert "timestamp" in data


@pytest.mark.skipif(not has_jq(), reason="jq is required by the hook script")
def test_hook_script_atomic_write(tmp_path):
    """No .tmp files left lying around."""
    env = os.environ.copy()
    env["CLAUDE_ALERTS_EVENTS_DIR"] = str(tmp_path)
    subprocess.run(
        ["bash", str(HOOK_SCRIPT), "Stop"],
        input='{"session_id":"x","cwd":"/y"}', text=True, env=env, check=True,
    )
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.skipif(not has_jq(), reason="jq is required by the hook script")
def test_hook_script_includes_tool_name_when_present(tmp_path):
    env = os.environ.copy()
    env["CLAUDE_ALERTS_EVENTS_DIR"] = str(tmp_path)
    payload = json.dumps({
        "session_id": "abc", "cwd": "/p", "tool_name": "Monitor",
    })
    subprocess.run(
        ["bash", str(HOOK_SCRIPT), "PostToolUse"],
        input=payload, text=True, env=env, check=True,
    )
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["tool_name"] == "Monitor"


@pytest.mark.skipif(not has_jq(), reason="jq is required by the hook script")
def test_hook_script_omits_tool_name_when_absent(tmp_path):
    env = os.environ.copy()
    env["CLAUDE_ALERTS_EVENTS_DIR"] = str(tmp_path)
    payload = json.dumps({"session_id": "abc", "cwd": "/p"})
    subprocess.run(
        ["bash", str(HOOK_SCRIPT), "Stop"],
        input=payload, text=True, env=env, check=True,
    )
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert "tool_name" not in data


@pytest.mark.skipif(not has_jq(), reason="jq is required by the hook script")
def test_hook_script_sanitizes_session_id_in_filename(tmp_path):
    """A hostile session_id with path-traversal characters must NOT escape
    the events directory. The JSON payload still carries the original
    session_id verbatim — only the filename is sanitized."""
    env = os.environ.copy()
    env["CLAUDE_ALERTS_EVENTS_DIR"] = str(tmp_path)
    payload = json.dumps({"session_id": "../../../etc/evil", "cwd": "/p"})
    subprocess.run(
        ["bash", str(HOOK_SCRIPT), "Stop"],
        input=payload, text=True, env=env, check=True,
    )
    # No file escaped the events dir.
    files = list(tmp_path.glob("**/*.json"))
    assert len(files) == 1
    assert files[0].parent == tmp_path  # still in tmp_path, not anywhere else
    # Filename has no '/' — '..' inside a filename is harmless (still a
    # single literal directory entry), but '/' would actually traverse.
    name = files[0].name
    assert "/" not in name
    # The JSON body still has the literal string (jq's --arg escaped it safely).
    data = json.loads(files[0].read_text())
    assert data["session_id"] == "../../../etc/evil"

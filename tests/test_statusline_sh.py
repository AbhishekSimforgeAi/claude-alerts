"""Subprocess tests for scripts/hooks/statusline.sh — contexts sidecar behavior."""
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "statusline.sh"


def has_jq():
    return shutil.which("jq") is not None


def _run(payload: dict, tmp_path: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["CLAUDE_ALERTS_STATE_DIR"] = str(tmp_path)
    env.pop("CLAUDE_ALERTS_WRAPPED_STATUSLINE", None)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        input=json.dumps(payload), text=True, env=env,
        capture_output=True, check=True,
    )


def _full_payload(session_id: str = "abc-123") -> dict:
    return {
        "session_id": session_id,
        "model": {"display_name": "Sonnet"},
        "workspace": {"current_dir": "/tmp/proj"},
        "rate_limits": {"five_hour": {"used_percentage": 1.0, "resets_at": 0}},
        "context_window": {
            "total_input_tokens": 100,
            "total_output_tokens": 50,
            "context_window_size": 200000,
            "used_percentage": 5.0,
            "remaining_percentage": 95.0,
            "current_usage": {
                "input_tokens": 8000,
                "output_tokens": 1000,
                "cache_creation_input_tokens": 4000,
                "cache_read_input_tokens": 2000,
            },
        },
    }


@pytest.mark.skipif(not has_jq(), reason="jq is required by the hook script")
def test_writes_contexts_sidecar(tmp_path):
    _run(_full_payload("abc-123"), tmp_path)
    sidecar = tmp_path / "contexts" / "abc-123.json"
    assert sidecar.exists()
    data = json.loads(sidecar.read_text())
    assert data["session_id"] == "abc-123"
    assert data["context_window"]["context_window_size"] == 200000
    assert data["context_window"]["current_usage"]["input_tokens"] == 8000
    # Mode 0600.
    assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600


@pytest.mark.skipif(not has_jq(), reason="jq is required")
def test_no_context_window_does_not_write_sidecar(tmp_path):
    payload = _full_payload()
    del payload["context_window"]
    _run(payload, tmp_path)
    assert not (tmp_path / "contexts").exists() or not list((tmp_path / "contexts").glob("*.json"))
    # Rate-limits sidecar still gets written.
    assert (tmp_path / "rate_limits.json").exists()


@pytest.mark.skipif(not has_jq(), reason="jq is required")
def test_rejects_session_id_with_path_traversal(tmp_path):
    _run(_full_payload("../../etc/passwd"), tmp_path)
    # Nothing written outside the contexts dir.
    assert not (tmp_path.parent / "etc").exists()
    contexts_dir = tmp_path / "contexts"
    if contexts_dir.exists():
        assert list(contexts_dir.glob("*.json")) == []


@pytest.mark.skipif(not has_jq(), reason="jq is required")
def test_rejects_session_id_with_control_chars(tmp_path):
    _run(_full_payload("abc\nevil"), tmp_path)
    contexts_dir = tmp_path / "contexts"
    if contexts_dir.exists():
        assert list(contexts_dir.glob("*.json")) == []


@pytest.mark.skipif(not has_jq(), reason="jq is required")
def test_atomic_write_no_tmp_files_left(tmp_path):
    _run(_full_payload(), tmp_path)
    contexts_dir = tmp_path / "contexts"
    assert list(contexts_dir.glob("*.tmp")) == []

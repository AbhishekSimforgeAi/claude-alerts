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


def _run(payload: dict, tmp_path: Path, *, tty_file: Path | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["CLAUDE_ALERTS_STATE_DIR"] = str(tmp_path)
    # Redirect the OSC title write away from the real /dev/tty so tests
    # don't change the terminal title of whoever is running pytest.
    env["CLAUDE_ALERTS_TTY"] = str(tty_file) if tty_file is not None else str(tmp_path / "tty")
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
def test_empty_git_branch_does_not_corrupt_parse(tmp_path):
    """Regression test for the U+0001 separator switch.

    With the previous tab separator, an empty git.branch value caused
    bash `read` to collapse the whitespace and shift later fields,
    producing the wrong session_id (or none). Verifies the contexts
    sidecar still lands at the correct path when the payload has no
    git block.
    """
    payload = _full_payload("regression-test-sid")
    # Note: _full_payload already omits the .git key.
    _run(payload, tmp_path)
    # The sidecar must land under the unsanitized session_id we passed,
    # not under "" or some shifted MODEL value.
    sidecar = tmp_path / "contexts" / "regression-test-sid.json"
    assert sidecar.exists(), \
        f"sidecar not at expected path; contents of {tmp_path / 'contexts'}: " \
        f"{list((tmp_path / 'contexts').glob('*'))}"
    data = json.loads(sidecar.read_text())
    assert data["session_id"] == "regression-test-sid"


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


@pytest.mark.skipif(not has_jq(), reason="jq is required")
def test_writes_osc_title_with_ctx(tmp_path):
    """OSC 2 sequence written to /dev/tty when context_window is present.

    used = 8000 + 4000 + 2000 = 14000 -> "14k"
    total = 200000                    -> "200k"
    pct   = 5.0                       -> "5%"
    cwd basename = "proj"
    """
    tty_file = tmp_path / "tty"
    _run(_full_payload(), tmp_path, tty_file=tty_file)
    assert tty_file.read_bytes() == b"\x1b]2;proj: 5% (14k/200k)\x07"


@pytest.mark.skipif(not has_jq(), reason="jq is required")
def test_writes_osc_title_without_ctx(tmp_path):
    """Without context_window the title is just the folder basename — no
    colon, no percent, no parentheses."""
    payload = _full_payload()
    del payload["context_window"]
    tty_file = tmp_path / "tty"
    _run(payload, tmp_path, tty_file=tty_file)
    assert tty_file.read_bytes() == b"\x1b]2;proj\x07"


@pytest.mark.skipif(not has_jq(), reason="jq is required")
def test_writes_osc_title_partial_ctx_falls_back_to_basename(tmp_path):
    """Brand-new session (context_window present but current_usage null)
    falls back to just the folder basename."""
    payload = _full_payload()
    payload["context_window"] = {"context_window_size": 200000}
    tty_file = tmp_path / "tty"
    _run(payload, tmp_path, tty_file=tty_file)
    assert tty_file.read_bytes() == b"\x1b]2;proj\x07"


@pytest.mark.skipif(not has_jq(), reason="jq is required")
def test_writes_osc_title_sub_one_percent(tmp_path):
    """Sub-1% non-zero usage renders as '<1%' rather than '0%'."""
    payload = _full_payload()
    payload["context_window"]["used_percentage"] = 0.5
    tty_file = tmp_path / "tty"
    _run(payload, tmp_path, tty_file=tty_file)
    contents = tty_file.read_bytes()
    assert contents.startswith(b"\x1b]2;proj: <1% (")
    assert contents.endswith(b")\x07")
    assert b"0%" not in contents


@pytest.mark.skipif(not has_jq(), reason="jq is required")
def test_osc_bytes_not_in_stdout(tmp_path):
    """OSC bytes never appear in stdout (the visible status-line text)."""
    result = _run(_full_payload(), tmp_path)
    assert "\x1b" not in result.stdout
    assert "\x07" not in result.stdout
    # Sanity: the existing visible status line is still emitted on stdout.
    assert "Sonnet" in result.stdout

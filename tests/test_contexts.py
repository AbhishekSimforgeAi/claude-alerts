"""Tests for per-session context-window sidecar parsing."""
import json
from pathlib import Path

from claude_alerts import contexts
from claude_alerts.contexts import ContextUsage


def _write_sidecar(base: Path, sid: str, payload: dict) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    p = base / f"{sid}.json"
    p.write_text(json.dumps(payload))
    return p


def _full_payload(saved_at: float = 1.0) -> dict:
    return {
        "saved_at": saved_at,
        "session_id": "abc",
        "context_window": {
            "total_input_tokens": 15234,
            "total_output_tokens": 4521,
            "context_window_size": 200000,
            "used_percentage": 7.9,
            "remaining_percentage": 92.1,
            "current_usage": {
                "input_tokens": 8500,
                "output_tokens": 1200,
                "cache_creation_input_tokens": 5000,
                "cache_read_input_tokens": 2000,
            },
        },
    }


def test_load_missing_file_returns_none(tmp_path):
    assert contexts.load("abc", tmp_path) is None


def test_load_full_payload(tmp_path):
    _write_sidecar(tmp_path, "abc", _full_payload(saved_at=42.0))
    cu = contexts.load("abc", tmp_path)
    assert cu is not None
    assert cu.saved_at == 42.0
    assert cu.used_percentage == 7.9
    # input + cache_creation + cache_read = 8500 + 5000 + 2000 = 15500
    # output_tokens (1200) is intentionally excluded.
    assert cu.used_tokens == 15500
    assert cu.total_tokens == 200000


def test_load_malformed_json_returns_none(tmp_path):
    p = tmp_path / "abc.json"
    p.write_text("{not json")
    assert contexts.load("abc", tmp_path) is None


def test_load_top_level_not_object_returns_none(tmp_path):
    p = tmp_path / "abc.json"
    p.write_text("[]")
    assert contexts.load("abc", tmp_path) is None


def test_load_missing_context_window_returns_none(tmp_path):
    _write_sidecar(tmp_path, "abc", {"saved_at": 1.0, "session_id": "abc"})
    assert contexts.load("abc", tmp_path) is None


def test_load_null_current_usage_yields_none_used_tokens(tmp_path):
    payload = _full_payload()
    payload["context_window"]["current_usage"] = None
    _write_sidecar(tmp_path, "abc", payload)
    cu = contexts.load("abc", tmp_path)
    assert cu is not None
    assert cu.used_tokens is None
    assert cu.used_percentage == 7.9
    assert cu.total_tokens == 200000


def test_load_zero_context_window_size_yields_none_total(tmp_path):
    payload = _full_payload()
    payload["context_window"]["context_window_size"] = 0
    _write_sidecar(tmp_path, "abc", payload)
    cu = contexts.load("abc", tmp_path)
    assert cu is not None
    assert cu.total_tokens is None


def test_load_negative_context_window_size_yields_none_total(tmp_path):
    payload = _full_payload()
    payload["context_window"]["context_window_size"] = -1
    _write_sidecar(tmp_path, "abc", payload)
    cu = contexts.load("abc", tmp_path)
    assert cu is not None
    assert cu.total_tokens is None


def test_load_missing_context_window_size_yields_none_total(tmp_path):
    payload = _full_payload()
    del payload["context_window"]["context_window_size"]
    _write_sidecar(tmp_path, "abc", payload)
    cu = contexts.load("abc", tmp_path)
    assert cu is not None
    assert cu.total_tokens is None


def test_load_null_used_percentage_yields_none(tmp_path):
    payload = _full_payload()
    payload["context_window"]["used_percentage"] = None
    _write_sidecar(tmp_path, "abc", payload)
    cu = contexts.load("abc", tmp_path)
    assert cu is not None
    assert cu.used_percentage is None


def test_load_excludes_output_tokens_from_used(tmp_path):
    """Mirrors how Claude Code computes used_percentage: input-only."""
    payload = _full_payload()
    payload["context_window"]["current_usage"]["output_tokens"] = 9999999
    _write_sidecar(tmp_path, "abc", payload)
    cu = contexts.load("abc", tmp_path)
    assert cu.used_tokens == 8500 + 5000 + 2000


def test_delete_removes_sidecar(tmp_path):
    _write_sidecar(tmp_path, "abc", _full_payload())
    contexts.delete("abc", tmp_path)
    assert not (tmp_path / "abc.json").exists()


def test_delete_is_idempotent_on_missing(tmp_path):
    # No exception when the file does not exist.
    contexts.delete("ghost", tmp_path)


def test_sweep_keeps_active_sessions_only(tmp_path):
    _write_sidecar(tmp_path, "alive", _full_payload())
    _write_sidecar(tmp_path, "dead",  _full_payload())
    removed = contexts.sweep({"alive"}, tmp_path)
    assert removed == 1
    assert (tmp_path / "alive.json").exists()
    assert not (tmp_path / "dead.json").exists()


def test_sweep_no_directory_returns_zero(tmp_path):
    assert contexts.sweep({"alive"}, tmp_path / "missing") == 0


def test_sweep_ignores_non_json_files(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "junk.txt").write_text("not ours")
    assert contexts.sweep(set(), tmp_path) == 0
    assert (tmp_path / "junk.txt").exists()

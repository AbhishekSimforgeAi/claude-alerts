"""Tests for rate-limit sidecar parsing."""
import json
from pathlib import Path

from claude_alerts import limits
from claude_alerts.limits import Limit, RateLimits


def _write_sidecar(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "rate_limits.json"
    p.write_text(json.dumps(payload))
    return p


def test_load_missing_file_returns_none(tmp_path):
    assert limits.load(tmp_path / "absent.json") is None


def test_load_full_payload(tmp_path):
    path = _write_sidecar(tmp_path, {
        "saved_at": 1777373944.0,
        "rate_limits": {
            "five_hour":        {"used_percentage": 42.5, "resets_at": 1777368000},
            "seven_day":        {"used_percentage": 18.3, "resets_at": 1777886400},
            "seven_day_opus":   {"used_percentage": 56.0, "resets_at": 1777886400},
            "seven_day_sonnet": {"used_percentage": 12.0, "resets_at": 1777886400},
        },
    })
    rl = limits.load(path)
    assert rl is not None
    assert rl.saved_at == 1777373944.0
    assert rl.five_hour == Limit(used_percentage=42.5, resets_at=1777368000)
    assert rl.seven_day_opus == Limit(used_percentage=56.0, resets_at=1777886400)
    assert rl.any_present()


def test_load_partial_payload(tmp_path):
    """Claude Code only emits keys it has data for. Missing windows must
    parse as None, not raise."""
    path = _write_sidecar(tmp_path, {
        "saved_at": 1.0,
        "rate_limits": {
            "five_hour": {"used_percentage": 50.0, "resets_at": 100},
        },
    })
    rl = limits.load(path)
    assert rl.five_hour is not None
    assert rl.seven_day is None
    assert rl.seven_day_opus is None
    assert rl.seven_day_sonnet is None


def test_load_malformed_json_returns_none(tmp_path):
    path = tmp_path / "rate_limits.json"
    path.write_text("{not json")
    assert limits.load(path) is None


def test_load_top_level_not_object_returns_none(tmp_path):
    path = tmp_path / "rate_limits.json"
    path.write_text("[]")
    assert limits.load(path) is None


def test_load_missing_rate_limits_returns_none(tmp_path):
    path = _write_sidecar(tmp_path, {"saved_at": 1.0})
    assert limits.load(path) is None


def test_load_skips_individually_malformed_windows(tmp_path):
    """A bogus shape on one window must not poison sibling windows."""
    path = _write_sidecar(tmp_path, {
        "saved_at": 1.0,
        "rate_limits": {
            "five_hour": {"used_percentage": "bogus", "resets_at": 1},
            "seven_day": {"used_percentage": 30.0, "resets_at": 200},
        },
    })
    rl = limits.load(path)
    assert rl.five_hour is None
    assert rl.seven_day is not None
    assert rl.seven_day.used_percentage == 30.0


def test_any_present_false_when_all_none():
    rl = RateLimits()
    assert rl.any_present() is False


def test_any_present_true_with_one_window():
    rl = RateLimits(five_hour=Limit(used_percentage=10.0, resets_at=1))
    assert rl.any_present() is True

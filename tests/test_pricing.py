"""Tests for cost computation and context-window lookup."""
from claude_alerts import pricing
from claude_alerts.pricing import context_window_for, cost_usd


def test_known_model_cost_math():
    """Each rate category contributes independently — verify the formula
    against a known input. Opus 4.7 input is $15/Mtok, output is $75/Mtok."""
    cost = cost_usd("claude-opus-4-7", 1_000_000, 0, 0, 1_000_000)
    assert cost == 15.0 + 75.0


def test_cache_read_and_write_separate_rates():
    """Cache reads are 10× cheaper than fresh input on Opus; cache writes
    are 25% more expensive."""
    cost = cost_usd("claude-opus-4-7", 0, 1_000_000, 1_000_000, 0)
    assert cost == 1.5 + 18.75


def test_unknown_model_returns_none():
    pricing._warned_unknown_models.clear()
    assert cost_usd("claude-mythical-99", 1000, 0, 0, 1000) is None


def test_unknown_model_warning_is_rate_limited(caplog):
    pricing._warned_unknown_models.clear()
    cost_usd("brand-new-model", 1, 0, 0, 0)
    cost_usd("brand-new-model", 1, 0, 0, 0)
    cost_usd("brand-new-model", 1, 0, 0, 0)
    warnings = [r for r in caplog.records if "unknown model" in r.message]
    assert len(warnings) == 1


def test_context_window_lookup():
    assert context_window_for("claude-opus-4-7") == 200_000
    assert context_window_for("claude-opus-4-7[1m]") == 1_000_000
    assert context_window_for("claude-sonnet-4-6") == 200_000


def test_context_window_unknown_returns_none():
    assert context_window_for("unknown-model") is None


def test_zero_tokens_zero_cost():
    assert cost_usd("claude-opus-4-7", 0, 0, 0, 0) == 0.0

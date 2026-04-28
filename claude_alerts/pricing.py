"""Static Anthropic pricing and context-window data for the dashboard.

Pricing drifts. The values below are pulled from
https://docs.claude.com/en/docs/about-claude/pricing
TODO(pricing): refresh quarterly. Last updated 2026-04-28.

Cost is computed per assistant message from `message.usage` in the JSONL
transcript: input_tokens at the input rate, cache_read_input_tokens at the
cache-read rate, cache_creation_input_tokens at the cache-write rate, and
output_tokens at the output rate.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Rate:
    """USD per million tokens for each input/output category."""
    input: float
    cache_read: float
    cache_write: float
    output: float


# Keys are model ids as they appear in JSONL `message.model`. The "[1m]"
# variant is the same model in a 1M-context configuration; pricing is
# identical, only the context window differs.
MODEL_PRICING_USD_PER_MTOK: dict[str, Rate] = {
    "claude-opus-4-7":           Rate(15.00,  1.50, 18.75, 75.00),
    "claude-opus-4-7[1m]":       Rate(15.00,  1.50, 18.75, 75.00),
    "claude-sonnet-4-6":         Rate( 3.00,  0.30,  3.75, 15.00),
    "claude-haiku-4-5":          Rate( 0.80,  0.08,  1.00,  4.00),
    "claude-haiku-4-5-20251001": Rate( 0.80,  0.08,  1.00,  4.00),
}


CONTEXT_WINDOW: dict[str, int] = {
    "claude-opus-4-7":           200_000,
    "claude-opus-4-7[1m]":     1_000_000,
    "claude-sonnet-4-6":         200_000,
    "claude-haiku-4-5":          200_000,
    "claude-haiku-4-5-20251001": 200_000,
}


_warned_unknown_models: set[str] = set()


def _warn_unknown(model: str) -> None:
    if model not in _warned_unknown_models:
        _warned_unknown_models.add(model)
        log.warning(
            "unknown model id %r; cost shown as ? — update claude_alerts/pricing.py",
            model,
        )


def cost_usd(
    model: str,
    input_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    output_tokens: int,
) -> Optional[float]:
    """Compute USD cost for one assistant message. Returns None for unknown models."""
    rate = MODEL_PRICING_USD_PER_MTOK.get(model)
    if rate is None:
        _warn_unknown(model)
        return None
    return (
        input_tokens       * rate.input       / 1_000_000
        + cache_read_tokens  * rate.cache_read  / 1_000_000
        + cache_write_tokens * rate.cache_write / 1_000_000
        + output_tokens      * rate.output      / 1_000_000
    )


def context_window_for(model: str) -> Optional[int]:
    """Return the model's context window in tokens, or None if unknown."""
    return CONTEXT_WINDOW.get(model)

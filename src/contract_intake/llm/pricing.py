"""Token pricing, so cost is computed rather than guessed.

Rates are USD per million tokens, list price on the first-party Anthropic API.
Cache multipliers are relative to the input rate: a 5-minute cache write costs
1.25x, a cache read 0.1x. Those two numbers are the whole reason prompt caching
pays off from the second document onwards (see docs/COST_MODEL.md).
"""

from __future__ import annotations

from dataclasses import dataclass

CACHE_WRITE_MULTIPLIER = 1.25  # 5-minute TTL
CACHE_READ_MULTIPLIER = 0.10


@dataclass(frozen=True, slots=True)
class ModelRates:
    input_per_mtok: float
    output_per_mtok: float
    adaptive_thinking: bool = True
    """Whether the model accepts `thinking: {type: "adaptive"}` and `effort`.

    These arrived with the 4.6 generation. Sending either to an older model is a
    400, not a silently-ignored field -- which matters here, because the model is
    a per-stage setting and mixing generations is the point of having it.
    """


RATES: dict[str, ModelRates] = {
    "claude-opus-5": ModelRates(5.00, 25.00),
    "claude-opus-4-8": ModelRates(5.00, 25.00),
    "claude-sonnet-5": ModelRates(3.00, 15.00),
    "claude-haiku-4-5": ModelRates(1.00, 5.00, adaptive_thinking=False),
}


def supports_adaptive_thinking(model: str) -> bool:
    return rates_for(model).adaptive_thinking


class UnknownModelError(KeyError):
    """Raised when a model has no rate entry.

    Deliberately fatal. A silently-unpriced call makes the ledger under-report,
    and an under-reporting ledger is worse than none at all.
    """


def rates_for(model: str) -> ModelRates:
    try:
        return RATES[model]
    except KeyError as exc:
        raise UnknownModelError(
            f"no pricing entry for {model!r}; add it to llm/pricing.py"
        ) from exc


@dataclass(frozen=True, slots=True)
class Usage:
    """Token usage for one call, split by billing category."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_input(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def cache_hit_ratio(self) -> float:
        return self.cache_read_tokens / self.total_input if self.total_input else 0.0


def cost_usd(model: str, usage: Usage) -> float:
    r = rates_for(model)
    per_token_in = r.input_per_mtok / 1_000_000
    per_token_out = r.output_per_mtok / 1_000_000
    return (
        usage.input_tokens * per_token_in
        + usage.cache_write_tokens * per_token_in * CACHE_WRITE_MULTIPLIER
        + usage.cache_read_tokens * per_token_in * CACHE_READ_MULTIPLIER
        + usage.output_tokens * per_token_out
    )

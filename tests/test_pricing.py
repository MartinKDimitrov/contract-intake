from __future__ import annotations

import pytest

from contract_intake.llm.pricing import UnknownModelError, Usage, cost_usd, rates_for


def test_plain_input_output_cost() -> None:
    # 1M input + 1M output on Opus 5 == $5 + $25.
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost_usd("claude-opus-5", usage) == pytest.approx(30.0)


def test_cache_read_is_a_tenth_of_input() -> None:
    read = cost_usd("claude-opus-5", Usage(cache_read_tokens=1_000_000))
    plain = cost_usd("claude-opus-5", Usage(input_tokens=1_000_000))
    assert read == pytest.approx(plain * 0.10)


def test_cache_write_carries_a_premium() -> None:
    write = cost_usd("claude-opus-5", Usage(cache_write_tokens=1_000_000))
    plain = cost_usd("claude-opus-5", Usage(input_tokens=1_000_000))
    assert write == pytest.approx(plain * 1.25)


def test_caching_pays_off_from_the_second_document() -> None:
    """One write plus one read must beat two uncached sends of the same prefix."""
    prefix = 1_000_000
    cached = cost_usd("claude-opus-5", Usage(cache_write_tokens=prefix)) + cost_usd(
        "claude-opus-5", Usage(cache_read_tokens=prefix)
    )
    uncached = 2 * cost_usd("claude-opus-5", Usage(input_tokens=prefix))
    assert cached < uncached


def test_cache_hit_ratio() -> None:
    usage = Usage(input_tokens=250, cache_read_tokens=750)
    assert usage.total_input == 1000
    assert usage.cache_hit_ratio == pytest.approx(0.75)


def test_unpriced_model_is_fatal_not_silent() -> None:
    with pytest.raises(UnknownModelError):
        rates_for("claude-imaginary-9")


def test_known_models_are_priced() -> None:
    for model in ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"):
        assert rates_for(model).input_per_mtok > 0

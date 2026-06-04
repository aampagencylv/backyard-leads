"""Tests for static cost estimation (pure-function path).

The atomic reservation against the DB is exercised by the staging Postgres
verification script — these tests cover the estimate_cost_usd math against
the static price table, which has no DB dependencies.
"""
from app.engagement_engine.cost import (
    estimate_cost_usd,
    STATIC_PRICE_TABLE_USD_PER_M_TOKENS,
)


def test_known_anthropic_pricing():
    # Opus: $15/M input, $75/M output
    # 1000 in + 500 out = $0.015 + $0.0375 = $0.0525
    cost = estimate_cost_usd("anthropic", "claude-opus-4-7", 1000, 500)
    assert cost == round(0.015 + 0.0375, 5)


def test_known_haiku_pricing():
    # Haiku: $1/M input, $5/M output
    cost = estimate_cost_usd("anthropic", "claude-haiku-4-5", 2000, 800)
    assert cost == round(0.002 + 0.004, 5)


def test_openrouter_deepseek_pricing():
    # DeepSeek V3: $0.27/M in, $1.10/M out
    cost = estimate_cost_usd(
        "openrouter", "deepseek/deepseek-v3", 5000, 1000,
    )
    expected = round((5000 / 1e6) * 0.27 + (1000 / 1e6) * 1.10, 5)
    assert cost == expected


def test_unknown_model_falls_back_to_provider_wildcard():
    # Ollama '*' wildcard ($0.01/M in/out) should kick in for an unknown
    # model under ollama.
    cost = estimate_cost_usd("ollama", "any-local-model", 1_000_000, 1_000_000)
    assert cost == round(0.01 + 0.01, 5)


def test_unknown_provider_falls_back_to_safe_default():
    """An unknown provider should default to Sonnet-level pricing so we
    never under-account."""
    cost = estimate_cost_usd("brand_new_provider", "weird-model", 1000, 500)
    # Falls back to (3, 15) — same as Sonnet
    expected = round((1000 / 1e6) * 3.0 + (500 / 1e6) * 15.0, 5)
    assert cost == expected


def test_zero_tokens_is_zero_cost():
    assert estimate_cost_usd("anthropic", "claude-opus-4-7", 0, 0) == 0.0


def test_aamp_default_treated_like_anthropic():
    # AAMP-default (proxying through Anthropic) priced same as direct Anthropic.
    cost_aamp = estimate_cost_usd("aamp_default", "claude-opus-4-7", 1000, 500)
    cost_anthropic = estimate_cost_usd("anthropic", "claude-opus-4-7", 1000, 500)
    assert cost_aamp == cost_anthropic


def test_price_table_has_expected_providers():
    """Sanity: every provider mentioned in the v3 design is in the table."""
    providers = {key[0] for key in STATIC_PRICE_TABLE_USD_PER_M_TOKENS}
    for required in ("anthropic", "openai", "openrouter",
                     "google_gemini", "ollama", "vllm", "aamp_default"):
        assert required in providers, f"missing pricing for {required}"

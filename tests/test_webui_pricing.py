"""Cost math for the web UI must match the engine's real token pricing."""

from tradingagents.webui.pricing import (
    cost_breakdown,
    cost_from_usage,
    normalize_model,
)


def test_normalize_model_maps_concrete_ids_to_price_keys():
    assert normalize_model("gpt-4o-mini-2024-07-18") == "gpt-4o-mini"
    assert normalize_model("gpt-4.1-mini") == "gpt-4.1-mini"
    assert normalize_model("some-unknown-model") == "some-unknown-model"


def test_cost_from_usage_prices_fresh_and_cached_separately():
    usage = {
        "gpt-4o-mini": {
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "input_token_details": {"cache_read": 400_000},
        }
    }
    # fresh in = 600k * 0.15, cached = 400k * 0.075, out = 1M * 0.60
    expected = (600_000 * 0.15 + 400_000 * 0.075 + 1_000_000 * 0.60) / 1_000_000
    assert cost_from_usage(usage) == round(expected, 10) or abs(
        cost_from_usage(usage) - expected
    ) < 1e-9


def test_cost_from_usage_sums_multiple_models():
    usage = {
        "gpt-4o-mini": {"input_tokens": 1_000_000, "output_tokens": 0},
        "gpt-4.1-mini": {"input_tokens": 1_000_000, "output_tokens": 0},
    }
    assert abs(cost_from_usage(usage) - (0.15 + 0.40)) < 1e-9


def test_cost_breakdown_flags_unpriced_models():
    usage = {
        "gpt-4o-mini": {"input_tokens": 1_000_000, "output_tokens": 0},
        "mystery-model": {"input_tokens": 5_000, "output_tokens": 5_000},
    }
    b = cost_breakdown(usage)
    assert b["complete"] is False
    assert "mystery-model" in b["unpriced_models"]
    assert b["per_model"]["mystery-model"]["priced"] is False
    # unknown model contributes $0, never an invented number
    assert abs(b["usd"] - 0.15) < 1e-9
    assert b["tokens_in"] == 1_005_000
    assert b["tokens_out"] == 5_000


def test_cost_breakdown_empty_is_zero_and_complete():
    b = cost_breakdown({})
    assert b == {
        "usd": 0.0,
        "tokens_in": 0,
        "tokens_out": 0,
        "per_model": {},
        "unpriced_models": [],
        "complete": True,
    }


def test_realistic_run_cost_is_in_expected_cents_range():
    # A run measured ~US$0.026; a plausible token mix should land near it.
    usage = {"gpt-4o-mini": {"input_tokens": 120_000, "output_tokens": 8_000},
             "gpt-4.1-mini": {"input_tokens": 15_000, "output_tokens": 4_000}}
    usd = cost_from_usage(usage)
    assert 0.005 < usd < 0.20

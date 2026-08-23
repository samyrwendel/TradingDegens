"""Token-usage → US$ cost, the single source of truth for the web UI.

The engine already tracks token usage through LangChain's
``UsageMetadataCallbackHandler`` (same handler ``run_portfolio.py`` uses). This
module turns that per-model usage dict into a dollar figure so every analysis
can show its cost — the brief calls hidden cost "dívida".

Prices are USD per 1M tokens (OpenAI list pricing, the config models the fork
runs on). An unknown model contributes $0 to the total and is surfaced in
``unpriced_models`` so the UI can say "custo parcial" instead of silently
undercounting — never invent a number.
"""

from __future__ import annotations

from typing import Any

# USD per 1M tokens. Keep the config models (gpt-4o-mini / gpt-4.1-mini) and a
# couple of forward defaults so a model bump doesn't silently drop cost.
PRICES: dict[str, dict[str, float]] = {
    "gpt-4o-mini":  {"in": 0.15, "cached": 0.075, "out": 0.60},
    "gpt-4.1-mini": {"in": 0.40, "cached": 0.10,  "out": 1.60},
    "gpt-4.1":      {"in": 2.00, "cached": 0.50,  "out": 8.00},
    "gpt-4o":       {"in": 2.50, "cached": 1.25,  "out": 10.00},
}


def normalize_model(name: str) -> str:
    """Map a concrete model id (``gpt-4o-mini-2024-07-18``) to a price key."""
    n = (name or "").lower()
    for key in PRICES:
        if key in n:
            return key
    return name


def _model_cost(model: str, usage: dict[str, Any]) -> tuple[float, bool]:
    """Return ``(usd, priced)`` for one model's usage entry."""
    price = PRICES.get(normalize_model(model))
    if not price:
        return 0.0, False
    in_tok = usage.get("input_tokens", 0) or 0
    out_tok = usage.get("output_tokens", 0) or 0
    details = usage.get("input_token_details") or {}
    cached = details.get("cache_read", 0) or 0
    fresh_in = max(in_tok - cached, 0)
    usd = (
        fresh_in * price["in"]
        + cached * price["cached"]
        + out_tok * price["out"]
    ) / 1_000_000
    return usd, True


def cost_from_usage(usage_metadata: dict[str, Any]) -> float:
    """Total USD cost across every model in a ``UsageMetadataCallbackHandler`` dict."""
    total = 0.0
    for model, usage in (usage_metadata or {}).items():
        usd, _priced = _model_cost(model, usage or {})
        total += usd
    return total


def cost_breakdown(usage_metadata: dict[str, Any]) -> dict[str, Any]:
    """Detailed cost view for the UI.

    Returns total USD, a per-model breakdown, aggregate token counts, and the
    list of models we had no price for (so the UI can flag a partial total
    rather than pretend the number is complete).
    """
    per_model: dict[str, dict[str, Any]] = {}
    unpriced: list[str] = []
    total = 0.0
    tokens_in = 0
    tokens_out = 0
    for model, usage in (usage_metadata or {}).items():
        usage = usage or {}
        usd, priced = _model_cost(model, usage)
        total += usd
        tokens_in += usage.get("input_tokens", 0) or 0
        tokens_out += usage.get("output_tokens", 0) or 0
        per_model[model] = {
            "usd": round(usd, 6),
            "input_tokens": usage.get("input_tokens", 0) or 0,
            "output_tokens": usage.get("output_tokens", 0) or 0,
            "priced": priced,
        }
        if not priced:
            unpriced.append(model)
    return {
        "usd": round(total, 6),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "per_model": per_model,
        "unpriced_models": unpriced,
        "complete": not unpriced,
    }

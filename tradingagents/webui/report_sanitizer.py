"""Scrub internal error/component strings from the published report (spec item 6d).

A degraded source used to leak its raw sentinel straight into the PDF — e.g. ZEC
rendered ``DATA_UNAVAILABLE: optional onchain_data ... [ta_datacache cached failure:
NoMarketDataError] No market data for 'ZEC-USD' ...`` three times. The user should
see a friendly "dados indisponíveis" line; the internal exception/component name
belongs in the server log, never in the report.

This runs deterministically over the assembled ``result`` module texts, mapping the
known sentinels to a friendly pt-BR line and stripping bracketed internal detail and
bare exception-class names. Fail-open: never raises.
"""
from __future__ import annotations

import re
from typing import Any

# Module report texts that reach the published PDF.
_TEXT_KEYS = (
    "market_report", "news_report", "sentiment_report", "fundamentals_report",
    "erick_report", "derivatives_report", "research_manager", "investment_plan",
    "trader_plan", "risk_decision", "bull", "bear",
)

# Friendly pt-BR names for the optional-enrichment categories.
_CATEGORY_PT = {
    "onchain_data": "on-chain",
    "onchain": "on-chain",
    "prediction_markets": "mercados de previsão",
    "sentiment": "sentimento",
    "derivatives": "derivativos",
    "fear_greed": "índice de medo e ganância",
    "etf_flow": "fluxo de ETF",
    "insider": "transações de insiders",
}

# Internal exception/component names that must never appear in the report.
_INTERNAL_NAMES = (
    "NoMarketDataError", "IntradayUnavailableError", "ta_datacache",
    "RuntimeError", "KeyError", "ValueError", "ConnectionError", "TimeoutError",
    "HTTPError", "JSONDecodeError", "Traceback",
)


def _friendly_category(cat: str) -> str:
    key = (cat or "").strip().strip("`'\"").lower()
    return _CATEGORY_PT.get(key, key.replace("_", " ") or "esse dado")


def sanitize_report_text(text: str | None) -> str:
    """Return ``text`` with internal sentinels/component names mapped to a friendly
    line. Idempotent; safe on already-clean text."""
    if not text or not isinstance(text, str):
        return text or ""
    out = text

    # 1) "DATA_UNAVAILABLE: optional <category> could not be retrieved ..." (to EOL).
    out = re.sub(
        r"DATA_UNAVAILABLE:\s*optional\s+`?([\w-]+)`?[^\n]*",
        lambda m: f"_Dados de {_friendly_category(m.group(1))} indisponíveis para este "
                  "ativo — a análise segue sem eles._",
        out,
    )
    # 2) "NO_DATA_AVAILABLE: No usable market data for '<sym>' ..." (to EOL).
    out = re.sub(
        r"NO_DATA_AVAILABLE:[^\n]*",
        "_Dados de mercado indisponíveis para este símbolo — a análise segue sem eles._",
        out,
    )
    # 3) Bracketed internal detail: "[ta_datacache cached failure: NoMarketDataError] ...".
    out = re.sub(r"\[ta_datacache cached failure:[^\]]*\]\s*", "", out)
    # 4) Bare "(SomeError)" parentheticals right after an "indisponível"-style word.
    out = re.sub(
        r"\s*\((?:" + "|".join(map(re.escape, _INTERNAL_NAMES)) + r")\)",
        "",
        out,
    )
    # 5) Any lingering bare internal name left mid-sentence → drop it (rare tail case).
    for name in _INTERNAL_NAMES:
        out = re.sub(rf"\b{re.escape(name)}\b:?\s*", "", out)
    # Tidy doubled spaces created by removals (not newlines).
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out


def has_internal_leak(text: str | None) -> bool:
    """True when ``text`` still contains an internal sentinel/component name — the
    predicate the item-6d test asserts is False after sanitising."""
    if not text or not isinstance(text, str):
        return False
    if "DATA_UNAVAILABLE" in text or "NO_DATA_AVAILABLE" in text:
        return True
    return any(name in text for name in _INTERNAL_NAMES)


def sanitize_result(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """Sanitise every published module text in ``result`` in place (fail-open)."""
    if not isinstance(result, dict):
        return result
    for key in _TEXT_KEYS:
        val = result.get(key)
        if isinstance(val, str) and val:
            try:
                result[key] = sanitize_report_text(val)
            except Exception:  # noqa: BLE001 — sanitising must never break a run
                continue
    return result

"""Guarantee a crypto market report always carries the derivatives signal.

For a crypto asset, funding / open interest / liquidations are not flavour — they
are the part of the picture yfinance is blind to. The market analyst is given
``get_crypto_derivatives`` on crypto runs, but an LLM may still write a
price-only report. This mirrors the prediction-market and multi-timeframe
coverage guards: after a crypto analyst writes its report, if it never surfaced
the derivatives, we fetch them deterministically (through the same routed + cached
path as the tool) and append the section — data when a source answers, an
explicit "unavailable" line when one is down, never a fabricated number.
"""
from __future__ import annotations

import logging

from tradingagents.dataflows.interface import route_to_vendor

logger = logging.getLogger(__name__)

# Each derivatives signal, with the synonyms that reveal it in the report. The
# report is written in pt-BR (leading term) but keeps the English original in
# parentheses on first use, so we match either form. Grouped by concept so one
# signal mentioned bilingually still counts once, not twice.
_SIGNALS = (
    ("taxa de financiamento", "funding rate", "funding"),
    ("contratos em aberto", "open interest"),
    ("liquidaç", "liquidation"),
)


def report_covers_derivatives(report: str) -> bool:
    if not report:
        return False
    low = report.lower()
    covered = sum(any(term in low for term in signal) for signal in _SIGNALS)
    return covered >= 2


def ensure_crypto_derivatives_coverage(report: str, symbol: str, curr_date: str) -> str:
    """Return the report guaranteed to carry a crypto-derivatives section."""
    if report_covers_derivatives(report):
        return report
    try:
        section = route_to_vendor("get_crypto_derivatives", symbol, curr_date)
    except Exception as exc:  # noqa: BLE001 — never break the report over enrichment
        logger.warning("crypto derivatives coverage failed for %s: %s", symbol, exc)
        section = (
            f"## Derivativos (derivatives) — {symbol}\n\n"
            f"Derivativos — taxa de financiamento (funding), contratos em aberto "
            f"(open interest), liquidações (liquidations) — indisponíveis "
            f"({type(exc).__name__}); nenhum valor inventado."
        )
    base = (report or "").rstrip()
    return f"{base}\n\n{section}\n" if base else section + "\n"

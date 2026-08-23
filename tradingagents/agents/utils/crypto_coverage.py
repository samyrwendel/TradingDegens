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

# Substrings that show the report already discussed the derivatives signal.
_MARKERS = ("funding rate", "open interest", "liquidation")


def report_covers_derivatives(report: str) -> bool:
    if not report:
        return False
    low = report.lower()
    return sum(m in low for m in _MARKERS) >= 2


def ensure_crypto_derivatives_coverage(report: str, symbol: str, curr_date: str) -> str:
    """Return the report guaranteed to carry a crypto-derivatives section."""
    if report_covers_derivatives(report):
        return report
    try:
        section = route_to_vendor("get_crypto_derivatives", symbol, curr_date)
    except Exception as exc:  # noqa: BLE001 — never break the report over enrichment
        logger.warning("crypto derivatives coverage failed for %s: %s", symbol, exc)
        section = (
            f"## Crypto Derivatives — {symbol}\n\n"
            f"Derivatives (funding, open interest, liquidations) unavailable "
            f"({type(exc).__name__}); no values fabricated."
        )
    base = (report or "").rstrip()
    return f"{base}\n\n{section}\n" if base else section + "\n"

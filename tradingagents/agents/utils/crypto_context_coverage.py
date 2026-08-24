"""Guarantee a crypto market report carries the network-context signals.

On-chain, spot-ETF flows and the Fear & Greed index are three feeds the modeled
decision process treats as first-class for a crypto call but that yfinance is
blind to. This mirrors the derivatives / multi-timeframe / prediction-market
coverage guards: after the crypto analyst writes its report, if it never
surfaced these, we fetch them deterministically — through the same routed,
cached and date-guarded path as the tool — and append the section. Real data
when a source answers, an explicit named "unavailable" when one is down or
paid-key-only, never a fabricated number.
"""
from __future__ import annotations

import logging

from tradingagents.dataflows.interface import route_to_vendor

logger = logging.getLogger(__name__)

# The three routed methods, in report order (on-chain first — the most cited).
_METHODS = ("get_onchain_metrics", "get_etf_flows", "get_fear_greed")

# Each context signal, with the synonyms that reveal it in the report. Matched
# bilingually (pt-BR leading term + English original) so one signal counts once.
_SIGNALS = (
    ("on-chain", "hashrate", "dominância", "dominance", "halving", "stablecoin"),
    ("fluxo de etf", "etf spot", "spot-etf", "etf flow"),
    ("medo & ganância", "medo e ganância", "fear & greed", "fear and greed"),
)


def build_crypto_context(symbol: str, curr_date: str) -> str:
    """Assemble the three network-context sections via the routed+cached vendors."""
    sections = []
    for method in _METHODS:
        try:
            section = route_to_vendor(method, symbol, curr_date)
        except Exception as exc:  # noqa: BLE001 — never break the report over enrichment
            logger.warning("crypto context %s failed for %s: %s", method, symbol, exc)
            section = f"_({method}: indisponível — {type(exc).__name__})_"
        if section:
            sections.append(section.rstrip())
    return "\n\n".join(sections)


def report_covers_crypto_context(report: str) -> bool:
    if not report:
        return False
    low = report.lower()
    covered = sum(any(term in low for term in signal) for signal in _SIGNALS)
    return covered >= 2


def ensure_crypto_context_coverage(report: str, symbol: str, curr_date: str) -> str:
    """Return the report guaranteed to carry the crypto network-context block."""
    if report_covers_crypto_context(report):
        return report
    section = build_crypto_context(symbol, curr_date)
    base = (report or "").rstrip()
    return f"{base}\n\n{section}\n" if base else section + "\n"

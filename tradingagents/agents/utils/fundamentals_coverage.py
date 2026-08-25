"""Guarantee the fundamentals report carries deterministic anchors.

The fundamentals analyst reads yfinance's real-time ``info`` (a LIVE market cap /
price and its own quarterly-table arithmetic), which drifted from the date-guarded
series the rest of the engine uses and let the debate cite TTM aggregates that did
not match the table. This mirrors the other coverage guards (price structure,
correlation, earnings): after the analyst writes its report, we append a
deterministic 'Âncoras' section — one frozen reference price (+ market cap, 52-week
low/high) and the TTM cash-flow aggregates summed straight from the quarterly table
— always real, never a fabricated number.
"""
from __future__ import annotations

import logging

from tradingagents.dataflows.fundamentals_anchors import (
    build_fundamentals_anchors_section,
)

logger = logging.getLogger(__name__)


def ensure_fundamentals_anchors_coverage(
    report: str, symbol: str, curr_date: str
) -> str:
    """Return the report with the deterministic anchors section appended.

    Fail-open: on any error (or when nothing is computable) the report is returned
    unchanged, so this enrichment never breaks the fundamentals output.
    """
    try:
        section = build_fundamentals_anchors_section(symbol, curr_date)
    except Exception as exc:  # noqa: BLE001 — never break the report over enrichment
        logger.warning("fundamentals anchors coverage failed for %s: %s", symbol, exc)
        return report
    if not section:
        return report
    base = (report or "").rstrip()
    return f"{base}\n\n{section}\n" if base else section + "\n"

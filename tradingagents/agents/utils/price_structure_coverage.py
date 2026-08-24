"""Guarantee the market report always carries the price-structure / setup read.

The market analyst LLM describes indicators but never identifies price STRUCTURE
— the pullback-to-a-rising-average buy region and the 1-2-3 reversal the product
owner actually trades. This mirrors the other coverage guards (multi-timeframe,
derivatives, prediction-markets): after the analyst writes its report, we append
a deterministic 'Estrutura de preço / setups' section computed from the same
cached, date-guarded daily series — always present, never a fabricated number.

Unlike the other guards it is appended UNCONDITIONALLY: the LLM cannot produce
this structural detection itself, so there is nothing to detect-and-skip.
"""
from __future__ import annotations

import logging

from tradingagents.dataflows.price_structure import build_price_structure_section

logger = logging.getLogger(__name__)


def ensure_price_structure_coverage(
    report: str, symbol: str, curr_date: str, timeframe: str = "1d"
) -> str:
    """Return the report with the deterministic price-structure section appended.

    ``timeframe`` selects the frame the structure/1-2-3/setup is detected on, so a
    run asked for a shorter frame carries that frame's concrete setup into the
    debate (the lever that lets a 15m verdict differ from the daily one). Daily by
    default — the timeframe-agnostic path stays unchanged.
    """
    try:
        section = build_price_structure_section(symbol, curr_date, timeframe)
    except Exception as exc:  # noqa: BLE001 — never break the report over enrichment
        logger.warning("price-structure coverage failed for %s: %s", symbol, exc)
        return report
    base = (report or "").rstrip()
    return f"{base}\n\n{section}\n" if base else section + "\n"

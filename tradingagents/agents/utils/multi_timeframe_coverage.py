"""Guarantee the market report always reads both timeframes (fork addition).

``get_price_timeframes`` is offered to the market analyst, but — like the
prediction-market tool before it — an LLM will happily write a daily-only report
and never mention the weekly frame. The BE backtest showed why that is
dangerous: the daily frame alone bought the first candle of a top correction the
weekly had already started rolling over. So this makes the weekly/daily read
**non-optional**: after the analyst writes its report, if it did not surface both
timeframes, we compute the deterministic weekly+daily trend and append it with a
convergence verdict. Silence on the higher timeframe is not an acceptable answer.

The price data comes from the same cached, ``<= curr_date``-cut series the tool
uses, so this adds no look-ahead surface and (after the first fetch) no network.
"""
from __future__ import annotations

import logging

from tradingagents.dataflows.multi_timeframe import build_timeframe_summary

logger = logging.getLogger(__name__)


def report_covers_timeframes(report: str) -> bool:
    """True if the report already discusses both the weekly and daily frames."""
    if not report:
        return False
    low = report.lower()
    return "weekly" in low and "daily" in low


def ensure_multi_timeframe_coverage(report: str, symbol: str, curr_date: str) -> str:
    """Return the report guaranteed to carry a weekly+daily trend section.

    If the analyst already cited both timeframes the report is returned
    unchanged; otherwise the deterministic section is appended. A hard data
    failure (unknown symbol) degrades to an explicit note — never a fabricated
    trend.
    """
    if report_covers_timeframes(report):
        return report
    try:
        section = build_timeframe_summary(symbol, curr_date)
    except Exception as exc:  # noqa: BLE001 — never break the report over enrichment
        logger.warning("multi-timeframe coverage failed for %s: %s", symbol, exc)
        section = (
            "## Multi-Timeframe Trend\n\n"
            f"Weekly/daily trend read unavailable ({type(exc).__name__}); "
            "no timeframe values fabricated."
        )
    base = (report or "").rstrip()
    return f"{base}\n\n{section}\n" if base else section + "\n"

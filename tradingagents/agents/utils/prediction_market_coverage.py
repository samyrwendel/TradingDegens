"""Guarantee the news report always carries a prediction-markets section.

``get_prediction_markets`` (Polymarket, keyless) is offered to the news analyst,
but the LLM tends to skip it — across the fork's first 8-asset run it was called
**zero** times. This module makes the signal non-optional (brief #4): after the
analyst writes its report, if it never surfaced prediction-market probabilities,
we fetch them deterministically for a small set of standing macro topics (plus
the instrument) and append a section — with data when a market exists, or an
explicit "no market for this topic" line otherwise. Silence is not an acceptable
answer.

The fetch goes through the same routing + cache path as the tool, so within a day
the deterministic call is served from cache after the first time.
"""
from __future__ import annotations

import logging

from tradingagents.dataflows.interface import route_to_vendor

logger = logging.getLogger(__name__)

SECTION_TITLE = "## Prediction Markets"

# Standing forward-looking macro topics every report should price. Kept short to
# bound the network/token cost; the instrument is appended per run.
_DEFAULT_TOPICS = ("Fed interest rate decision", "US recession")

# Substrings that mean the vendor found no usable forward-looking market.
_NO_MARKET_MARKERS = (
    "No open prediction markets matched",
    "currently unavailable",
    "Proceed without prediction-market signal",
)


def report_mentions_prediction_markets(report: str) -> bool:
    """True if the analyst already surfaced prediction-market content."""
    if not report:
        return False
    low = report.lower()
    return "prediction market" in low or "polymarket" in low


def _topics_for(company: str | None) -> list[str]:
    topics = list(_DEFAULT_TOPICS)
    if company:
        c = company.strip()
        if c and c.lower() not in (t.lower() for t in topics):
            topics.append(c)
    return topics


def _has_market_data(vendor_output: str) -> bool:
    text = (vendor_output or "").strip()
    if not text:
        return False
    return not any(marker in text for marker in _NO_MARKET_MARKERS)


def build_prediction_market_section(company: str | None = None) -> str:
    """Fetch prediction markets for the standing topics and render a section."""
    lines = [
        SECTION_TITLE,
        "",
        "_Market-implied probabilities of forward-looking events (Polymarket), "
        "auto-attached so the report never omits this signal._",
        "",
    ]
    for topic in _topics_for(company):
        try:
            out = route_to_vendor("get_prediction_markets", topic, None)
        except Exception as exc:  # noqa: BLE001 — never break the report
            logger.warning("prediction-market coverage failed for %r: %s", topic, exc)
            lines.append(
                f"- **{topic}** — prediction-market lookup errored "
                f"({type(exc).__name__}); no market signal available for this topic."
            )
            lines.append("")
            continue
        if _has_market_data(out):
            lines.append(f"### {topic}\n{out.strip()}")
        else:
            lines.append(f"- **{topic}** — no open prediction market found for this topic.")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def ensure_prediction_market_coverage(report: str, company: str | None = None) -> str:
    """Return the report with a guaranteed prediction-markets section.

    If the analyst already surfaced prediction markets, the report is returned
    unchanged; otherwise the deterministic section is appended.
    """
    if report_mentions_prediction_markets(report):
        return report
    section = build_prediction_market_section(company)
    base = (report or "").rstrip()
    return f"{base}\n\n{section}" if base else section

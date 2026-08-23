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

SECTION_TITLE = "## Mercados de Previsão"

# Standing forward-looking macro topics every report should price. Kept short to
# bound the network/token cost; the instrument is appended per run. Each entry is
# ``(query, label)``: the English ``query`` searches Polymarket (whose markets are
# titled in English), while the pt-BR ``label`` is what the report displays, so no
# stray English topic string leaks into the output.
_DEFAULT_TOPICS = (
    ("Fed interest rate decision", "Decisão de juros do Fed"),
    ("US recession", "Recessão nos EUA"),
)

# Substrings that mean the vendor found no usable forward-looking market. Both
# the pt-BR message the vendor now emits and the older English markers are kept,
# so detection is robust across language and any legacy vendor path.
_NO_MARKET_MARKERS = (
    "Nenhum mercado de previsão aberto casou com",
    "No open prediction markets matched",
    "currently unavailable",
    "indisponível no momento",
    "Proceed without prediction-market signal",
)


def report_mentions_prediction_markets(report: str) -> bool:
    """True if the analyst already surfaced prediction-market content."""
    if not report:
        return False
    low = report.lower()
    return (
        "prediction market" in low
        or "polymarket" in low
        or "mercado de previsão" in low
        or "mercados de previsão" in low
    )


def _topics_for(company: str | None) -> list[tuple[str, str]]:
    """Return ``(query, label)`` pairs to price. The instrument is a proper noun,
    so its query and label are the same string (never translated)."""
    topics = list(_DEFAULT_TOPICS)
    if company:
        c = company.strip()
        if c and c.lower() not in (q.lower() for q, _ in topics):
            topics.append((c, c))
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
        "_Probabilidades de eventos futuros implícitas no mercado (Polymarket), "
        "anexadas automaticamente para o relatório nunca omitir esse sinal._",
        "",
    ]
    for query, label in _topics_for(company):
        try:
            out = route_to_vendor("get_prediction_markets", query, None, display=label)
        except Exception as exc:  # noqa: BLE001 — never break the report
            logger.warning("prediction-market coverage failed for %r: %s", query, exc)
            lines.append(
                f"- **{label}** — a consulta de mercado de previsão falhou "
                f"({type(exc).__name__}); sem sinal de mercado para este tópico."
            )
            lines.append("")
            continue
        if _has_market_data(out):
            lines.append(f"### {label}\n{out.strip()}")
        else:
            lines.append(f"- **{label}** — nenhum mercado de previsão aberto para este tópico.")
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

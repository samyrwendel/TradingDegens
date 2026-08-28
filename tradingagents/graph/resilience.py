"""Pipeline resilience: a failing phase degrades instead of aborting the run.

Samyr's question — "if a phase errors, do you retry once or throw the whole
analysis away?" The answer was "throw it away": a single bad indicator ('ema')
aborted the graph and lost the entire multi-agent debate. This module makes the
pipeline degrade, not crash:

* :func:`make_resilient_analyst` wraps an analyst node so a raising node is
  retried once (most failures are transient — network / rate-limit) and, if it
  still fails, returns an explicit "indisponível" report and lets the analysis
  continue. Only a genuinely essential failure (no price at all, surfaced by the
  runner) becomes a total ERROR — a single analyst dropping out never does.
* :func:`tool_error_message` is the ToolNode error handler: any tool exception is
  turned into a message the model reads and moves on from, instead of propagating
  and aborting the graph (the exact path a cache-wrapped RuntimeError took).
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage

logger = logging.getLogger(__name__)


def tool_error_message(exc: Exception) -> str:
    """ToolNode error handler: never let a tool exception abort the run.

    Returned string becomes the ToolMessage content, so the analyst LLM sees the
    failure and proceeds with whatever else it has, rather than the exception
    tearing down the whole graph.
    """
    return (
        f"Ferramenta indisponível nesta chamada ({type(exc).__name__}: {exc}). "
        "Prossiga com os demais dados; não repita esta chamada."
    )


def make_resilient_analyst(node, report_key: str, label: str, attempts: int = 2):
    """Wrap an analyst node with retry-once-then-degrade.

    ``node`` is the raw analyst callable ``state -> dict``. ``report_key`` is the
    state field it fills (e.g. ``"market_report"``); ``label`` is the human name
    used in the degraded note (e.g. ``"Market Analyst"``). ``attempts`` is the
    total number of tries (2 = one retry).

    On success returns the node's own output. If every attempt raises, returns a
    valid state update whose message carries no tool calls (so the graph routes on
    to the next node) and whose ``report_key`` is an explicit pt-BR "indisponível"
    note — the debate downstream still runs, just without this analyst.
    """
    def wrapped(state):
        last: Exception | None = None
        for i in range(1, attempts + 1):
            try:
                return node(state)
            except Exception as exc:  # noqa: BLE001 — degrade, never abort the run
                last = exc
                logger.warning(
                    "analyst %s failed on attempt %d/%d: %s", label, i, attempts, exc
                )
        # Every attempt (incl. the automatic retry) failed: degrade this source and
        # keep going. The retry already happened silently — the user only ever sees
        # a resolved-source result or this explicit "feito sem X", never a raw error.
        reason = f"{type(last).__name__}: {last}"
        note = (
            f"⚠️ Fonte **{label}** indisponível após {attempts} tentativa(s) "
            f"({reason}). A análise seguiu SEM essa fonte — trate esta leitura como "
            "ausente, não como sinal."
        )
        # Structured entry aggregated onto the run state (reducer=add) so the UI can
        # name the failed source, say the analysis was done without it, and offer a
        # "reavaliar com essa fonte" control. ``kind="missing"`` because this source
        # really is ABSENT — the debate guard emits ``kind="suspect"`` for a turn
        # that IS in the analysis with flagged text, and the banner must not tell
        # the user those two are the same thing.
        return {
            "messages": [AIMessage(content=note)],
            report_key: note,
            "degraded_sources": [
                {
                    "label": label,
                    "report_key": report_key,
                    "reason": reason,
                    "kind": "missing",
                }
            ],
        }

    return wrapped

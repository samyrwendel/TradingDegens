"""Turn-size limiting + post-generation sanity guard for debate turns (spec 3b).

Two mitigations against the long-run pt-BR corruption documented in
:mod:`tradingagents.agents.utils.text_sanity`:

* :func:`clip_report` bounds how much of each analyst report (and the running
  debate history) is injected into a single debate/risk turn. The corruption
  correlates with oversized context — the corrupt AAPL run fed ~10k-char reports
  vs ~3-4k in clean runs — so capping the per-turn input attacks the root cause.
* :func:`invoke_debate_turn` validates the generated turn and, when it comes
  back degraded, regenerates it once with a corrective nudge, keeping whichever
  generation is cleaner. A moderately-degraded turn that can't be improved is
  returned but reported, so the run marks it instead of shipping it silently.

Config keys (all read from the run config, with safe fallbacks):
``debate_report_clip_chars``, ``debate_history_clip_chars``,
``debate_sanity_check``, ``debate_sanity_regen``,
``debate_sanity_invented_rate_degrade``, ``debate_sanity_invented_rate_suspect``.
"""

from __future__ import annotations

from typing import Any

from tradingagents.agents.utils.text_sanity import SanityReport, sanity_report

_CLIP_MARKER = "\n\n[… {n} caracteres omitidos para limitar o contexto do turno …]\n\n"

# Appended to the prompt on the single regeneration attempt. Names the failure
# plainly so the model rewrites in correct pt-BR instead of repeating the garble.
REGEN_NUDGE = (
    "\n\nATENÇÃO: a tentativa anterior de resposta saiu com palavras corrompidas "
    "ou inventadas e trechos ilegíveis. Reescreva a resposta INTEIRA do zero, em "
    "português do Brasil correto e natural — sem inventar palavras, sem misturar "
    "letras dentro das palavras, sem códigos de formatação (%d, %s) e sem grudar "
    "números em palavras. Priorize clareza e legibilidade."
)


def clip_report(text: Any, max_chars: int) -> str:
    """Clip ``text`` to ``max_chars``, keeping the head and tail with a marker.

    ``max_chars <= 0`` (or falsy) disables clipping. ``None``/non-str becomes
    ``""``. Head-and-tail is deliberate: an analyst report's verdict often sits
    at the end, so a plain head truncation would drop the conclusion.
    """
    if text is None:
        return ""
    text = str(text)
    if not max_chars or max_chars <= 0 or len(text) <= max_chars:
        return text
    marker = _CLIP_MARKER.format(n=len(text) - max_chars)
    budget = max_chars - len(marker)
    if budget <= 0:  # pathologically small cap — just hard-truncate the head
        return text[:max_chars]
    head = (budget * 3) // 4
    tail = budget - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def _content(response: Any) -> str:
    """Extract text from an LLM response (AIMessage-like or bare string)."""
    if response is None:
        return ""
    content = getattr(response, "content", response)
    return content if isinstance(content, str) else str(content)


def _report(text: str, config: dict | None) -> SanityReport:
    cfg = config or {}
    kwargs = {}
    degrade = cfg.get("debate_sanity_invented_rate_degrade")
    suspect = cfg.get("debate_sanity_invented_rate_suspect")
    if degrade is not None:
        kwargs["invented_rate_degrade"] = float(degrade)
    if suspect is not None:
        kwargs["invented_rate_suspect"] = float(suspect)
    return sanity_report(text, **kwargs)


def invoke_debate_turn(
    llm: Any,
    prompt: str,
    *,
    speaker: str,
    config: dict | None = None,
) -> tuple[str, SanityReport | None]:
    """Invoke ``llm`` for one debate turn, guarding the output.

    Returns ``(content, report)``. ``report`` is ``None`` when the sanity check
    is disabled. When enabled and the first generation is ``degraded`` (and
    regeneration is on), one corrective retry runs and the cleaner of the two
    generations is returned.
    """
    cfg = config or {}
    content = _content(llm.invoke(prompt))

    if not cfg.get("debate_sanity_check", True):
        return content, None

    report = _report(content, cfg)
    if report.degraded and cfg.get("debate_sanity_regen", True):
        retry_content = _content(llm.invoke(prompt + REGEN_NUDGE))
        retry_report = _report(retry_content, cfg)
        # Adopt the retry only when it is STRICTLY cleaner (fewer structural
        # artifacts, or the same count with a lower invented rate). On a tie the
        # first generation stands — a swap with no measurable gain buys nothing.
        if retry_report.score() < report.score():
            content, report = retry_content, retry_report

    return content, report


def degraded_note(speaker: str, report: SanityReport | None) -> str:
    """One-line pt-BR note for ``degraded_sources`` when a turn was not clean."""
    if report is None or report.clean:
        return ""
    label = "regenerado/degradado" if report.degraded else "texto suspeito"
    return f"{speaker} ({label}: {report.summary()})"

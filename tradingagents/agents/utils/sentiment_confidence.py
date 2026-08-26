"""Deterministic sentiment confidence + non-informative fallback (spec items 9/6c).

The sentiment analyst pre-fetches three sources (news, StockTwits, Reddit); each
degrades to a ``<... unavailable>`` / ``<no ... found>`` placeholder rather than
raising. The LLM used to *judge* its own confidence, which drifted between runs
("Alta 8,5/10 confiança média" with 2/3 sources out on ZEC, "2,5/10 baixa" on
AAOI). Here confidence is a deterministic function of how many sources actually
returned data:

* 3 sources with data → ``high``
* 2 → ``medium``
* ≤1 → ``low`` → the read is flagged NON-INFORMATIVE so the judge does not anchor
  the decision to a "bearish 2,5/10 low-confidence" number built on nothing.

Applied by rewriting the rendered report's confidence line and prepending an
explicit flag — the judge reads the report as text, so the flag reaches it.
"""
from __future__ import annotations

# ≤ this many sources with data ⇒ low confidence ⇒ non-informative for the judge.
_NON_INFORMATIVE_MAX_SOURCES = 1


def _source_has_data(block: str | None) -> bool:
    """True when a pre-fetched source block carries real data (not a placeholder)."""
    if not block or not block.strip():
        return False
    b = block.strip()
    # A bare "<...>" placeholder token (a single angle-bracket span, no real content):
    # covers "<stocktwits unavailable: ...>", "<no StockTwits messages found ...>",
    # "<no news ...>", etc. regardless of the exact wording.
    if b.startswith("<") and b.endswith(">") and "\n" not in b:
        return False
    # Reddit lists one line per subreddit; if EVERY line is a "<no posts found>"
    # placeholder the source contributed nothing.
    lines = [ln for ln in b.splitlines() if ln.strip()]
    return not (lines and all("<no posts found" in ln.lower() for ln in lines))


def deterministic_confidence(
    news_block: str | None,
    stocktwits_block: str | None,
    reddit_block: str | None,
) -> dict[str, object]:
    """Return ``{sources_ok, confidence, non_informative}`` from source availability."""
    sources_ok = sum(
        _source_has_data(b) for b in (news_block, stocktwits_block, reddit_block)
    )
    if sources_ok >= 3:
        confidence = "high"
    elif sources_ok == 2:
        confidence = "medium"
    else:
        confidence = "low"
    return {
        "sources_ok": sources_ok,
        "confidence": confidence,
        "non_informative": sources_ok <= _NON_INFORMATIVE_MAX_SOURCES,
    }


_CONF_PT = {"low": "Baixa", "medium": "Média", "high": "Alta"}


def annotate_report(report: str, info: dict[str, object]) -> str:
    """Rewrite the confidence line to the DETERMINISTIC value and, when the read is
    non-informative, prepend an explicit flag the judge must honour.

    Idempotent-ish: the confidence line is replaced (not duplicated); the flag is
    only prepended when non-informative.
    """
    conf = str(info.get("confidence", "low"))
    n = int(info.get("sources_ok", 0))
    conf_pt = _CONF_PT.get(conf, conf.capitalize())
    conf_line = f"**Confiança:** {conf_pt} — {n}/3 fontes com dados (determinístico)"

    lines = (report or "").splitlines()
    replaced = False
    for i, ln in enumerate(lines):
        if ln.strip().startswith("**Confiança:**"):
            lines[i] = conf_line
            replaced = True
            break
    body = "\n".join(lines)
    if not replaced:
        body = f"{conf_line}\n{body}" if body else conf_line

    if info.get("non_informative"):
        flag = (
            f"> ⚠️ **Sentimento NÃO-informativo** ({n}/3 fontes com dados): o juiz "
            "deve tratar este bloco como fraco e NÃO ancorar a decisão à nota — sem "
            "dados suficientes, a direção não é confiável."
        )
        body = f"{flag}\n\n{body}" if body else flag
    return body

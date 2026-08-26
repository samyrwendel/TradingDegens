"""Deterministic sentiment confidence + non-informative fallback (spec item 9)."""
import pytest

from tradingagents.agents.utils.sentiment_confidence import (
    annotate_report,
    deterministic_confidence,
)

_REAL_NEWS = "AAOI beats on revenue; datacenter demand cited by three outlets."
_REAL_TWITS = "$AAOI 40 messages, 70% Bullish tags, notable posts on capacity."
_REAL_REDDIT = "r/stocks: 12 posts mentioning AAOI; r/wallstreetbets: 8 posts."
_PLACEHOLDER_TWITS = "<stocktwits unavailable: ConnectionError>"
_PLACEHOLDER_TWITS2 = "<no StockTwits messages found for $AAOI>"
_PLACEHOLDER_REDDIT = (
    "r/wallstreetbets: <no posts found mentioning AAOI in the past 7 days>\n"
    "r/stocks: <no posts found mentioning AAOI in the past 7 days>"
)


@pytest.mark.unit
def test_three_sources_high_confidence():
    info = deterministic_confidence(_REAL_NEWS, _REAL_TWITS, _REAL_REDDIT)
    assert info["sources_ok"] == 3
    assert info["confidence"] == "high"
    assert info["non_informative"] is False


@pytest.mark.unit
def test_two_sources_medium_confidence():
    info = deterministic_confidence(_REAL_NEWS, _PLACEHOLDER_TWITS, _REAL_REDDIT)
    assert info["sources_ok"] == 2
    assert info["confidence"] == "medium"
    assert info["non_informative"] is False


@pytest.mark.unit
def test_one_source_low_and_non_informative():
    """The ZEC/AAOI bug: 1/3 sources must be LOW confidence and non-informative, not
    an anchoring 'bearish 2,5/10 low-confidence'."""
    info = deterministic_confidence(_REAL_NEWS, _PLACEHOLDER_TWITS, _PLACEHOLDER_REDDIT)
    assert info["sources_ok"] == 1
    assert info["confidence"] == "low"
    assert info["non_informative"] is True


@pytest.mark.unit
def test_zero_sources_low_and_non_informative():
    info = deterministic_confidence("", _PLACEHOLDER_TWITS2, _PLACEHOLDER_REDDIT)
    assert info["sources_ok"] == 0
    assert info["confidence"] == "low"
    assert info["non_informative"] is True


@pytest.mark.unit
def test_annotate_rewrites_confidence_line_deterministically():
    report = (
        "**Sentimento Geral:** **De baixa — Bearish** (Nota: 2.5/10)\n"
        "**Confiança:** Média\n"
        "Narrativa: só o feed de notícias tinha dados."
    )
    info = deterministic_confidence(_REAL_NEWS, _PLACEHOLDER_TWITS, _PLACEHOLDER_REDDIT)
    out = annotate_report(report, info)
    # the LLM's "Média" is replaced by the deterministic low + source count
    assert "**Confiança:** Baixa — 1/3 fontes com dados (determinístico)" in out
    assert "**Confiança:** Média" not in out
    # and a non-informative flag is prepended for the judge
    assert "NÃO-informativo" in out
    assert out.strip().startswith(">")


@pytest.mark.unit
def test_annotate_high_confidence_no_flag():
    report = "**Sentimento Geral:** **De alta — Bullish** (Nota: 7.0/10)\n**Confiança:** Baixa\nx"
    info = deterministic_confidence(_REAL_NEWS, _REAL_TWITS, _REAL_REDDIT)
    out = annotate_report(report, info)
    assert "**Confiança:** Alta — 3/3 fontes com dados (determinístico)" in out
    assert "NÃO-informativo" not in out

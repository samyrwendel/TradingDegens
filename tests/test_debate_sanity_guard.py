"""The debate turn guard clips context and regenerates a degraded turn (3b).

These are offline: a fake LLM returns scripted content so the regeneration and
context-clipping behaviour is asserted without any network call. The lexical
speller is irrelevant here — the corrupt fixtures carry *mechanical* artifacts,
which the zero-dependency structural detector always catches.
"""

from types import SimpleNamespace

from tradingagents.agents.researchers.bull_researcher import create_bull_researcher
from tradingagents.agents.utils.debate_utils import (
    REGEN_NUDGE,
    clip_report,
    degraded_entry,
    invoke_debate_turn,
)
from tradingagents.agents.utils.text_sanity import sanity_report

CORRUPT = "o preço es'tá caindo e o bear fica dezANIMAdO com d%d% de chance"
CLEAN = (
    "o preço recuou hoje e o cenário de baixa ganhou força, com o modelo "
    "apontando maior probabilidade de continuação da queda no curto prazo"
)


class ScriptedLLM:
    """Returns queued contents in order, one per ``invoke``."""

    def __init__(self, *contents):
        self._contents = list(contents)
        self.prompts = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        content = self._contents.pop(0) if self._contents else "ok"
        return SimpleNamespace(content=content)


# --------------------------------------------------------------------------
# clip_report
# --------------------------------------------------------------------------

def test_clip_report_disabled_returns_input():
    assert clip_report("x" * 1000, 0) == "x" * 1000


def test_clip_report_keeps_head_and_tail():
    text = "HEAD" + ("m" * 5000) + "TAIL"
    clipped = clip_report(text, 400)
    assert len(clipped) <= 400 + 80  # marker overhead
    assert clipped.startswith("HEAD")
    assert clipped.endswith("TAIL")
    assert "omitidos" in clipped


def test_clip_report_handles_none():
    assert clip_report(None, 500) == ""


# --------------------------------------------------------------------------
# invoke_debate_turn — regeneration
# --------------------------------------------------------------------------

def test_regenerates_degraded_turn_and_keeps_clean_one():
    llm = ScriptedLLM(CORRUPT, CLEAN)
    content, report = invoke_debate_turn(
        llm, "base prompt", speaker="Bull Researcher", config={}
    )
    assert content == CLEAN
    assert not report.degraded
    assert len(llm.prompts) == 2
    assert REGEN_NUDGE in llm.prompts[1]  # the retry carried the corrective nudge


def test_clean_turn_does_not_regenerate():
    llm = ScriptedLLM(CLEAN)
    content, report = invoke_debate_turn(
        llm, "base prompt", speaker="Bull Researcher", config={}
    )
    assert content == CLEAN
    assert report.severity == "clean"
    assert len(llm.prompts) == 1


def test_regen_disabled_returns_first_and_flags():
    llm = ScriptedLLM(CORRUPT, CLEAN)
    content, report = invoke_debate_turn(
        llm,
        "base prompt",
        speaker="Bull Researcher",
        config={"debate_sanity_regen": False},
    )
    assert content == CORRUPT
    assert report.degraded
    assert len(llm.prompts) == 1  # no retry


def test_sanity_check_disabled_skips_validation():
    llm = ScriptedLLM(CORRUPT)
    content, report = invoke_debate_turn(
        llm,
        "base prompt",
        speaker="Bull Researcher",
        config={"debate_sanity_check": False},
    )
    assert content == CORRUPT
    assert report is None
    assert len(llm.prompts) == 1


def test_keeps_first_when_retry_no_better():
    # both generations corrupt: retry is not strictly better, first stands
    llm = ScriptedLLM(CORRUPT, "ainda es'tá corrompido e dezANIMAdO d%d%")
    content, report = invoke_debate_turn(
        llm, "base prompt", speaker="Bull Researcher", config={}
    )
    assert content == CORRUPT
    assert report.degraded


# --------------------------------------------------------------------------
# degraded_entry — the STRUCTURED entry the UI banner reads
# --------------------------------------------------------------------------

def test_degraded_entry_none_for_clean():
    assert degraded_entry("Bull Researcher", sanity_report(CLEAN),
                          report_key="investment_debate_state") is None


def test_degraded_entry_is_structured_and_names_the_speaker():
    # Regression (task 20260828-003): this used to be a bare string, so the UI
    # rendered "Análise feita SEM a fonte: fonte" with no reason listed.
    entry = degraded_entry("Bull Researcher", sanity_report(CORRUPT),
                           report_key="investment_debate_state")
    assert isinstance(entry, dict)
    assert entry["label"] == "Bull Researcher"
    assert entry["report_key"] == "investment_debate_state"
    assert entry["reason"] and "degradado" in entry["reason"]
    # a debate turn SHIPS — it is flagged text, not an absent source
    assert entry["kind"] == "suspect"


def test_degraded_entry_reason_is_pt_br_not_diagnostic_shorthand():
    entry = degraded_entry("Bull Researcher", sanity_report(CORRUPT),
                           report_key="investment_debate_state")
    assert "severity=" not in entry["reason"]
    assert "artefato" in entry["reason"]


def test_degraded_entry_handles_none():
    assert degraded_entry("Bull Researcher", None,
                          report_key="investment_debate_state") is None


# --------------------------------------------------------------------------
# Bull node integration: a degraded turn surfaces in degraded_sources
# --------------------------------------------------------------------------

def _min_state():
    return {
        "investment_debate_state": {
            "history": "",
            "bull_history": "",
            "bear_history": "",
            "current_response": "",
            "count": 0,
        },
        "market_report": "relatório de mercado",
        "sentiment_report": "relatório de sentimento",
        "news_report": "notícias",
        "fundamentals_report": "fundamentos",
        "asset_type": "stock",
        "company_of_interest": "AAPL",
        "instrument_context": "AAPL (ação)",
    }


def test_bull_node_marks_degraded_source_when_unrecoverable():
    # both generations corrupt -> node ships a turn but marks it degraded
    llm = ScriptedLLM(CORRUPT, CORRUPT)
    node = create_bull_researcher(llm)
    out = node(_min_state())
    assert out["degraded_sources"]
    entry = out["degraded_sources"][0]
    assert entry["label"] == "Bull Researcher"
    assert entry["reason"]
    assert entry["kind"] == "suspect"


def test_bull_node_clean_turn_has_no_degraded_source():
    llm = ScriptedLLM(CLEAN)
    node = create_bull_researcher(llm)
    out = node(_min_state())
    assert "degraded_sources" not in out
    assert "Analista de Alta (bull):" in out["investment_debate_state"]["current_response"]

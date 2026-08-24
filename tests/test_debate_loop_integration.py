"""The bull/bear debate must actually alternate — end to end (fork brief 23/08).

The unit test in ``test_risk_router_path_map`` pins the *router* in isolation, but
the field failure it could not catch was empirical: a run reached the judge with
``bear_history`` EMPTY and the bull having spoken twice. The pt-BR speaker label
(``"Analista de Alta (bull): ..."``) had stopped matching a legacy
``startswith("Bull")`` check, so the alternation collapsed and the verdict was
decided one-sided — silently.

These tests drive the REAL debate nodes through the REAL router and shared path
map (a minimal LangGraph wired exactly like ``graph/setup.py`` wires the debate),
with a fake LLM so they run offline. They assert the debate produced one speech
per side and — the hard rule — that ``bear_history`` is NOT empty. If anyone
reintroduces the label/router mismatch, the bull speaks twice, the bear never,
and these fail. The bug can no longer pass silently.
"""
from types import SimpleNamespace

import pytest
from langgraph.graph import END, START, StateGraph

from tradingagents.agents.managers.research_manager import assert_debate_integrity
from tradingagents.agents.researchers.bear_researcher import create_bear_researcher
from tradingagents.agents.researchers.bull_researcher import create_bull_researcher
from tradingagents.agents.utils.agent_states import AgentState
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.setup import DEBATE_PATH_MAP


class _FakeLLM:
    """Minimal stand-in for the quick-thinking LLM the researchers invoke.

    Returns non-empty content per call (the node prepends the pt-BR speaker
    label). Distinct content per call lets the assertions confirm each side ran.
    """

    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, prompt):  # noqa: ARG002 — prompt content is irrelevant here
        self.calls += 1
        return SimpleNamespace(content=f"Argumento fundamentado nº {self.calls}.")


def _build_debate_graph(llm):
    """Compile just the research-debate slice, wired like ``graph/setup.py``:

    entry -> Bull; both researchers route through ``should_continue_debate`` over
    the shared ``DEBATE_PATH_MAP``; the judge is a terminal stub (this test is
    about the alternation, not the ruling — the ruling is covered separately).
    """
    logic = ConditionalLogic(max_debate_rounds=1)
    workflow = StateGraph(AgentState)
    workflow.add_node("Bull Researcher", create_bull_researcher(llm))
    workflow.add_node("Bear Researcher", create_bear_researcher(llm))

    def _judge_stub(state):  # terminal: assert integrity, then stop
        assert_debate_integrity(state["investment_debate_state"])
        return {}

    workflow.add_node("Research Manager", _judge_stub)
    workflow.add_edge(START, "Bull Researcher")
    for node in ("Bull Researcher", "Bear Researcher"):
        workflow.add_conditional_edges(
            node, logic.should_continue_debate, DEBATE_PATH_MAP
        )
    workflow.add_edge("Research Manager", END)
    return workflow.compile()


def _initial_state(ticker: str, asset_type: str) -> dict:
    state = Propagator().create_initial_state(
        ticker, "2026-08-23", asset_type=asset_type,
        instrument_context=f"{ticker} — instrumento de teste",
    )
    # Reports the researchers read; content is immaterial to the routing.
    for key in ("market_report", "sentiment_report", "news_report",
                "fundamentals_report"):
        state[key] = "relatório de teste"
    return state


@pytest.mark.integration
@pytest.mark.parametrize("ticker,asset_type", [
    ("AAPL", "stock"),
    ("BTC-USD", "crypto"),
])
def test_debate_alternates_bear_speaks(ticker, asset_type):
    """One speech per side, and ``bear_history`` is NOT empty — the exact field
    that came back empty in the reported break."""
    graph = _build_debate_graph(_FakeLLM())
    final = graph.invoke(_initial_state(ticker, asset_type))
    debate = final["investment_debate_state"]

    assert debate["bear_history"].strip(), (
        f"[{ticker}] bear_history is EMPTY — the debate did not alternate; "
        "the judge would rule one-sided (the reported regression)."
    )
    assert debate["bull_history"].strip(), f"[{ticker}] bull_history is empty"
    # Exactly one speech from each side for a single debate round.
    assert debate["bull_history"].count("Analista de Alta") == 1
    assert debate["bear_history"].count("Analista de Baixa") == 1
    # Bull first, then bear: count reaches 2 * max_debate_rounds.
    assert debate["count"] == 2


@pytest.mark.unit
def test_guard_raises_on_one_sided_debate():
    """The judge refuses to rule when the bull argued but the bear did not."""
    with pytest.raises(RuntimeError, match="one-sided"):
        assert_debate_integrity(
            {"count": 2, "bull_history": "Analista de Alta (bull): ...",
             "bear_history": ""}
        )


@pytest.mark.unit
def test_guard_raises_when_bull_missing():
    """Symmetric: a bear-only debate is just as broken."""
    with pytest.raises(RuntimeError):
        assert_debate_integrity(
            {"count": 2, "bull_history": "",
             "bear_history": "Analista de Baixa (bear): ..."}
        )


@pytest.mark.unit
def test_guard_passes_on_healthy_debate():
    """Both sides argued — the judge proceeds."""
    assert_debate_integrity(
        {"count": 2, "bull_history": "Analista de Alta (bull): ...",
         "bear_history": "Analista de Baixa (bear): ..."}
    )


@pytest.mark.unit
def test_guard_tolerates_single_round_config():
    """``max_debate_rounds=0`` runs the bull once (count < 2) via the entry edge
    and never reaches a back-and-forth — the guard must not false-positive."""
    assert_debate_integrity(
        {"count": 1, "bull_history": "Analista de Alta (bull): ...",
         "bear_history": ""}
    )

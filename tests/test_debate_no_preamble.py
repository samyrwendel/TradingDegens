"""Sem floreio retórico nos agentes de debate (task 012).

O Samyr: os agentes cospem "Prezados colegas e, em particular, meu estimado colega
analista de baixa" — floreio inútil que gasta token e alimenta a degradação de texto
longo. A causa era a instrução "conversational style / Output conversationally".

Aqui capturamos o PROMPT real que cada nó de debate manda ao LLM (bull/bear +
aggressive/neutral/conservative) e provamos que a instrução agora (a) PROÍBE
saudação/preâmbulo explicitamente e (b) não pede mais o estilo "conversacional" que
virava saudação teatral.
"""

from types import SimpleNamespace

import pytest

from tradingagents.agents.researchers.bear_researcher import create_bear_researcher
from tradingagents.agents.researchers.bull_researcher import create_bull_researcher
from tradingagents.agents.risk_mgmt.aggressive_debator import create_aggressive_debator
from tradingagents.agents.risk_mgmt.conservative_debator import create_conservative_debator
from tradingagents.agents.risk_mgmt.neutral_debator import create_neutral_debator
from tradingagents.graph.propagation import Propagator

pytestmark = pytest.mark.unit


class _CapturingLLM:
    """Captura o primeiro prompt enviado e devolve um argumento denso e válido."""

    def __init__(self):
        self.prompt = None

    def invoke(self, prompt):
        if self.prompt is None:
            self.prompt = prompt
        return SimpleNamespace(
            content="EBITDA subiu 12% no trimestre e o guidance foi elevado; "
                    "a tese de baixa ignora o desalavancamento em curso.")


def _state():
    state = Propagator().create_initial_state(
        "AAPL", "2026-08-23", asset_type="stock",
        instrument_context="AAPL — instrumento de teste")
    for key in ("market_report", "sentiment_report", "news_report", "fundamentals_report"):
        state[key] = "relatório de teste com números"
    state["trader_investment_plan"] = "Plano do trader: comprar no recuo à média."
    return state


# frases de floreio que NÃO podem mais aparecer na instrução
_BANNED = ("conversational style", "Output conversationally", "dynamic debate")


def _capture(factory):
    llm = _CapturingLLM()
    factory(llm)(_state())
    assert llm.prompt is not None
    return llm.prompt


@pytest.mark.parametrize("factory", [
    create_bull_researcher, create_bear_researcher,
    create_aggressive_debator, create_neutral_debator, create_conservative_debator,
])
def test_debate_prompt_forbids_preamble_and_drops_conversational(factory):
    prompt = _capture(factory)
    # (a) proíbe saudação/preâmbulo explicitamente
    assert "NO greeting" in prompt, prompt[:400]
    assert "esteemed colleague" in prompt          # exemplo do que NÃO fazer
    assert "no preamble" in prompt.lower()
    # (b) o gatilho do floreio saiu
    for banned in _BANNED:
        assert banned not in prompt, f"instrução ainda pede floreio: {banned!r}"


def test_source_has_no_conversational_directive():
    # trava de regressão no fonte dos 5 agentes de debate
    import pathlib
    base = pathlib.Path(__file__).resolve().parents[1] / "tradingagents" / "agents"
    files = [
        base / "researchers" / "bull_researcher.py",
        base / "researchers" / "bear_researcher.py",
        base / "risk_mgmt" / "aggressive_debator.py",
        base / "risk_mgmt" / "neutral_debator.py",
        base / "risk_mgmt" / "conservative_debator.py",
    ]
    for f in files:
        src = f.read_text(encoding="utf-8")
        assert "conversational style" not in src, f
        assert "Output conversationally" not in src, f

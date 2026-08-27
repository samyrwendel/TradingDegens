"""Integração runner × fallback (task 027-fallback): a análise NÃO para quando o
provedor do topo falha por estado (429), cai pro próximo da cadeia, CONCLUI, e o
resultado carrega o marcador de desvio por-etapa + o banner de resumo.

Exercita de verdade: um grafo-fake dirige um :class:`FallbackRunnable` real (topo 429
→ fallback OK) com o tracker que o runner injetou, e prova pela borda pública
(``status()`` do runner) que o run terminou ``done`` com os fallbacks visíveis.
"""
import uuid

import pytest

import tradingagents.webui.runner as runner_module
from tests.test_webui_runner import FINAL_STATE, _wait
from tradingagents.llm_clients.fallback import FallbackRunnable
from tradingagents.webui.progress import (
    ProgressCallbackHandler,
    ThinkingCallbackHandler,
)
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.store import HistoryStore

pytestmark = pytest.mark.integration


class _Msg:
    def __init__(self, content):
        self.content = content


class _Prim:
    def invoke(self, input, config=None, **kwargs):
        raise RuntimeError("Error code: 429 - Too Many Requests (rate limit)")


class _Sec:
    def invoke(self, input, config=None, **kwargs):
        return _Msg("parecer do fallback")


class _FallbackGraph:
    """Grafo-fake que EXERCITA o fallback numa etapa: o membro primário (claude-cli)
    estoura 429, o motor cai pro openai e conclui. Dirige os callbacks de atribuição
    (024P1) pra a etapa 'Market Analyst' reportar o modelo REAL do fallback."""

    NODE = "Market Analyst"

    def __init__(self, callbacks, tracker):
        self.callbacks = callbacks
        self.tracker = tracker

    def propagate(self, ticker, date, asset_type="stock", timeframe="1d"):
        # Atribuição por-etapa: o membro que de fato rodou (fallback openai) reporta
        # seu provider/model pelo metadata padrão do langchain (ls_*).
        for cb in self.callbacks:
            if isinstance(cb, ThinkingCallbackHandler):
                cb.on_chat_model_start(
                    {}, [], run_id=uuid.uuid4(),
                    metadata={"langgraph_node": self.NODE,
                              "ls_provider": "openai", "ls_model_name": "gpt-5.4-mini"},
                )
            if isinstance(cb, ProgressCallbackHandler):
                for node in (self.NODE, "Portfolio Manager"):
                    cb.on_chat_model_start({}, [], run_id=uuid.uuid4(),
                                           metadata={"langgraph_node": node})
        # A troca REAL: topo 429 → fallback OK, registrada no tracker do runner.
        fr = FallbackRunnable(
            [{"provider": "claude-cli", "model": "claude-sonnet-5", "llm": _Prim()},
             {"provider": "openai", "model": "gpt-5.4-mini", "llm": _Sec()}],
            tracker=self.tracker, level="quick",
        )
        out = fr.invoke("prompt", config={"metadata": {"langgraph_node": self.NODE}})
        assert out.content == "parecer do fallback"   # não parou
        return FINAL_STATE, "Buy"


def _factory():
    def make(config, selected_analysts, callbacks):
        # O runner injeta o tracker do run no config; o grafo real o tiraria antes do
        # set_config — o fake só o LÊ pra registrar a troca de verdade.
        return _FallbackGraph(callbacks, config.get("_fallback_tracker"))
    return make


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(runner_module, "fetch_price_chart",
                        lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_actionable_plan",
                        lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_derivatives_report", lambda t, d: "")


def test_run_does_not_stop_and_marks_fallback(tmp_path):
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "claude-cli",
                     "deep_think_llm": "claude-sonnet-5", "quick_think_llm": "claude-haiku-4-5"},
        store=HistoryStore(tmp_path), graph_factory=_factory(),
    )
    # Dono (allow_server_key=True) → a cadeia de fallback está destravada.
    run_id = runner.start("AAPL", "2020-01-02",
                          overrides={"allow_server_key": True})
    snap = _wait(runner, run_id)
    assert snap["status"] == "done"                    # a análise CONCLUIU

    result = snap["result"]
    # Banner de resumo: houve troca, visível.
    fallbacks = result["fallbacks"]
    assert len(fallbacks) == 1
    h = fallbacks[0]
    assert h["from_provider"] == "claude-cli" and h["to_provider"] == "openai"
    assert h["code"] == "rate_limit" and h["node"] == "Market Analyst"

    # Selo por-etapa: a linha da etapa 'Market Analyst' carrega o desvio.
    steps = result["audit"]["models_by_step"]
    market = next(s for s in steps if s["node"] == "Market Analyst")
    assert market["provider"] == "openai"              # atribuição = modelo REAL do fallback
    assert market["fallback"]["to_provider"] == "openai"
    assert market["fallback"]["reason"]


def test_no_fallback_leaves_result_clean(tmp_path):
    """Sem troca (topo saudável) o resultado fica limpo — nada de 'fallbacks'."""
    from tests.test_webui_runner import _FakeGraph

    def factory():
        def make(config, selected, callbacks):
            return _FakeGraph(callbacks, FINAL_STATE, "Buy")
        return make

    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "openai"},
        store=HistoryStore(tmp_path), graph_factory=factory(),
    )
    run_id = runner.start("AAPL", "2020-01-02", overrides={"allow_server_key": True})
    snap = _wait(runner, run_id)
    assert snap["status"] == "done"
    assert "fallbacks" not in snap["result"]           # caminho feliz intacto

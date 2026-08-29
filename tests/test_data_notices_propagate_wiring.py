"""O cano dos avisos de dado vive no GRAFO, não no chamador.

O C4 ligou ``data_notices`` na webui (``runner._execute`` e ``_worker_setup123``) e só
lá. Quem chama ``graph.propagate()`` direto — o CLI (``main.py``), ``run_portfolio.py``,
os backtests ``backtest_be_*.py``, ``run_be_macro.py`` — nunca via nada: uma série
vencida servida no fail-open chegava MUDA ao relatório, exatamente o bug L2.

Isso pesa mais no backtest do que na tela: é ali que os limiares provisórios
(``_DECEL_BARS``, ``_EARNINGS_WINDOW_DAYS``, ``_GATILHO_TOL``) seriam calibrados, e
calibrar num caminho mudo sobre dado velho é calibrar em cima de ruído silencioso.

Por isso o reset/merge desceu pra ``TradingAgentsGraph._run_graph``: um lugar só,
que TODO caminho de execução atravessa. Quem já drenava por fora (a webui) segue
funcionando — só encontra o coletor vazio.
"""
from unittest.mock import MagicMock

import pytest

from tradingagents.dataflows import data_notices
from tradingagents.graph.trading_graph import TradingAgentsGraph

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _limpo():
    data_notices.reset()
    yield
    data_notices.reset()


def _grafo_falso(final_state, durante_a_run=None):
    """Stub com o mínimo que ``_run_graph`` toca — sem LLM, sem rede."""
    g = MagicMock()
    g.config = {}                      # checkpoint desligado
    g.debug = False
    g.memory_log.get_past_context.return_value = ""
    g.resolve_instrument_context.return_value = {}
    g.propagator.create_initial_state.return_value = {}
    g.propagator.get_graph_args.return_value = {}
    g.process_signal.return_value = "Buy"

    def _invoke(*a, **k):
        if durante_a_run:
            durante_a_run()          # o fetch de dentro de um nó registra o aviso
        return final_state

    g.graph.invoke.side_effect = _invoke
    return g


def test_aviso_da_camada_de_fetch_chega_ao_estado_final(tmp_path):
    """DENTE: sem o merge no ``_run_graph``, ``degraded_sources`` volta sem o aviso e
    o backtest segue cego pra série vencida."""
    final_state = {"final_trade_decision": "Rating: Buy"}
    g = _grafo_falso(final_state, durante_a_run=lambda: data_notices.record(
        "série OHLCV de MCD", "última barra em 2026-08-24 para a data pedida 2026-08-27"))

    estado, _sinal = TradingAgentsGraph._run_graph(g, "MCD", "2026-08-27")

    deg = estado.get("degraded_sources") or []
    assert any("MCD" in d["label"] for d in deg), deg
    assert all(d["kind"] == "suspect" for d in deg), deg


def test_aviso_preserva_as_fontes_que_ja_estavam_no_estado():
    """O aviso ENTRA na lista das fontes degradadas, não a substitui."""
    final_state = {"final_trade_decision": "Rating: Hold",
                   "degraded_sources": [{"label": "Reddit", "report_key": "",
                                         "reason": "fonte caiu", "kind": "missing"}]}
    g = _grafo_falso(final_state, durante_a_run=lambda: data_notices.record(
        "série OHLCV de MCD", "buraco de 2 dias úteis"))

    estado, _ = TradingAgentsGraph._run_graph(g, "MCD", "2026-08-27")

    labels = [d["label"] for d in estado["degraded_sources"]]
    assert "Reddit" in labels and any("MCD" in x for x in labels)


def test_uma_run_nao_herda_o_aviso_da_anterior():
    """Processo de backtest roda N tickers em sequência na MESMA thread: sem o reset
    na entrada, o aviso do MCD apareceria no relatório do próximo papel."""
    data_notices.record("série OHLCV de MCD", "sobra da run anterior")
    final_state = {"final_trade_decision": "Rating: Buy"}
    g = _grafo_falso(final_state)

    estado, _ = TradingAgentsGraph._run_graph(g, "AAPL", "2026-08-27")

    assert not (estado.get("degraded_sources") or []), estado.get("degraded_sources")


def test_run_limpa_nao_inventa_campo_de_degradacao():
    """Sem aviso nenhum, o estado sai como entrou — silêncio é o normal."""
    final_state = {"final_trade_decision": "Rating: Buy"}
    estado, _ = TradingAgentsGraph._run_graph(_grafo_falso(final_state), "AAPL", "2026-08-27")
    assert "degraded_sources" not in estado

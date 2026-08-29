"""Uma varredura por vez — o reclique não vira uma segunda rajada no provedor.

A revisão mediu 25s a 75s no ``/api/scan``, e a chamada MAIS lenta foi a segunda
consecutiva. O servidor é threaded e não sabe que o cliente desistiu: quem cansou
de esperar e reclicou disparava uma varredura nova ENQUANTO a primeira ainda batia
no yfinance — o dobro de chamadas, e throttle acumulado em cima de uma fonte que já
estava reclamando. Isso explica um outlier de 75s melhor do que "está lento".
"""
import threading
import time

import pytest

import tradingagents.webui.runner as rm
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.store import HistoryStore

pytestmark = pytest.mark.unit


@pytest.fixture()
def runner(tmp_path):
    r = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                       store=HistoryStore(tmp_path))
    r.watchlist_store.set(["AAPL", "MSFT"])
    return r


def _stub_lento(monkeypatch, chamadas, dur=0.4):
    def lento(tickers, date, *a, **k):
        chamadas.append(date)
        time.sleep(dur)
        return {"date": date, "frames": ["1d"], "resumo": {}, "ativos": []}

    monkeypatch.setattr(rm, "scan_watchlist", lento)


def test_pedidos_concorrentes_viram_UMA_varredura(runner, monkeypatch):
    chamadas: list[str] = []
    _stub_lento(monkeypatch, chamadas)
    t0 = time.time()
    ths = [threading.Thread(target=runner.scan_portfolio, args=("2026-08-29",))
           for _ in range(4)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    assert len(chamadas) == 1, ("cada reclique virou uma rajada nova no provedor", chamadas)
    assert time.time() - t0 < 1.2, "os pedidos foram serializados em vez de compartilhar"


def test_o_memo_expira_e_a_varredura_seguinte_e_de_verdade(runner, monkeypatch):
    """O memo é pra absorver reclique, não pra congelar a tela: passada a janela,
    "Escanear" escaneia mesmo."""
    chamadas: list[str] = []
    _stub_lento(monkeypatch, chamadas, dur=0.0)
    monkeypatch.setattr(rm, "_SCAN_MEMO_TTL", 0.0)
    runner.scan_portfolio("2026-08-29")
    runner.scan_portfolio("2026-08-29")
    assert len(chamadas) == 2


def test_data_diferente_nunca_reaproveita_o_memo(runner, monkeypatch):
    chamadas: list[str] = []
    _stub_lento(monkeypatch, chamadas, dur=0.0)
    runner.scan_portfolio("2026-08-29")
    runner.scan_portfolio("2026-08-28")
    assert chamadas == ["2026-08-29", "2026-08-28"]

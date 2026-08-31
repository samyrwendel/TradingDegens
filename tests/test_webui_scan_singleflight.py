"""Uma varredura por vez — o reclique não vira uma segunda rajada no provedor.

A revisão mediu 25s a 75s no ``/api/scan``, e a chamada MAIS lenta foi a segunda
consecutiva. O servidor é threaded e não sabe que o cliente desistiu: quem cansou
de esperar e reclicou disparava uma varredura nova ENQUANTO a primeira ainda batia
no yfinance — o dobro de chamadas, e throttle acumulado em cima de uma fonte que já
estava reclamando. Isso explica um outlier de 75s melhor do que "está lento".

**A partir da task 20260831-015 estes testes também são o guarda da FRONTEIRA
entre as duas camadas.** O snapshot em disco (o "último resultado conhecido", que
faz a tela abrir com informação) entrou no mesmo método, e as duas se parecem o
bastante pra alguém querer fundi-las. Elas respondem a perguntas diferentes — *já
estou varrendo?* e *o que eu sabia da última vez?* — e o que segue prova que a
chegada da segunda não afrouxou a primeira: pedidos concorrentes continuam virando
UMA varredura (e UMA gravação), e o memo continua com a janela curta que "Escanear"
precisa pra escanear de verdade.
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


# --------------------------------------- a fronteira entre as DUAS camadas ----
def test_o_snapshot_nao_afrouxou_o_singleflight(runner, monkeypatch):
    """Quatro pedidos concorrentes: UMA varredura E UMA gravação em disco.

    DENTE: o jeito errado de somar a camada 2 seria gravar por pedido (ou, pior,
    varrer por pedido pra "ter o que gravar") — o arquivo ficaria certo e o
    provedor pagaria a conta de novo, que é exatamente o que o single-flight
    veio matar.
    """
    chamadas: list[str] = []
    _stub_lento(monkeypatch, chamadas, dur=0.3)
    gravacoes: list[dict] = []
    original = runner.scan_snapshot.save
    monkeypatch.setattr(runner.scan_snapshot, "save",
                        lambda res: (gravacoes.append(res), original(res))[1])
    ths = [threading.Thread(target=runner.scan_portfolio, args=("2026-08-29",))
           for _ in range(4)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    assert len(chamadas) == 1, ("o reclique voltou a virar rajada no provedor", chamadas)
    assert len(gravacoes) == 1, ("cada pedido gravou o snapshot de novo", len(gravacoes))


def test_o_retorno_pelo_memo_nao_regrava_o_snapshot(runner, monkeypatch):
    """Dentro dos 5s o segundo pedido devolve o MESMO objeto — não há o que gravar.

    Regravar seria I/O dentro do lock sem mudar um byte do arquivo. E, como o
    ``gerado_em`` viaja no resultado, uma regravação também não rejuvenesceria o
    carimbo — o que só torna o I/O mais inútil, não mais inofensivo.
    """
    chamadas: list[str] = []
    _stub_lento(monkeypatch, chamadas, dur=0.0)
    gravacoes: list[dict] = []
    monkeypatch.setattr(runner.scan_snapshot, "save", lambda res: gravacoes.append(res))
    a = runner.scan_portfolio("2026-08-29")
    b = runner.scan_portfolio("2026-08-29")     # dentro da janela do memo
    assert a is b, "o memo devia ter devolvido o MESMO objeto"
    assert len(chamadas) == 1 and len(gravacoes) == 1


def test_o_memo_morre_no_restart_e_o_snapshot_nao(runner, tmp_path, monkeypatch):
    """A razão de existirem as duas, num teste só.

    Uma instância NOVA (o serviço recém-reiniciado) não tem memo nenhum — se
    perguntassem a ela "o que você sabia?", a camada 1 responderia nada. A
    camada 2 responde, e sem varrer.
    """
    chamadas: list[str] = []
    _stub_lento(monkeypatch, chamadas, dur=0.0)
    monkeypatch.setattr(rm, "scan_watchlist", lambda tickers, date, *a, **k: (
        chamadas.append(date) or {"date": date, "frames": ["1d"], "resumo": {},
                                  "gerado_em": "2026-08-29T14:32:00-04:00",
                                  "ativos": [{"ticker": "AAPL", "melhor": {}, "frames": []}]}))
    runner.scan_portfolio("2026-08-29")
    assert len(chamadas) == 1

    novo = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                          store=HistoryStore(tmp_path))
    assert novo._scan_memo is None, "o memo não pode sobreviver ao processo"
    salvo = novo.scan_ultimo()
    assert salvo["ativos"][0]["ticker"] == "AAPL"
    assert salvo["gerado_em"] == "2026-08-29T14:32:00-04:00"
    assert len(chamadas) == 1, "responder 'o que eu sabia' não pode varrer"

"""O relógio da revalidação automática é o do servidor — e é o MESMO do scan
(task 20260831-018).

A tela precisa saber quando o próximo candle do frame exibido fecha. Recalcular
isso em JavaScript criaria um segundo agendador com regra própria: no dia em que
os dois divergissem, ninguém saberia qual manda — e o scan e o gráfico passariam a
"fechar candle" em minutos diferentes. Então a tela PERGUNTA.

Os dentes:

* o horário sai de :func:`agenda.proxima_passada` e a cadência de
  :func:`agenda.cadencia_minutos` — o teste compara com as funções, não com uma
  constante decorada, então mudar a regra num lugar não deixa o outro para trás;
* o horário é ANCORADO NO RELÓGIO (minuto 0 da hora), não em quando o serviço
  subiu — um restart às 14h37 não pode passar a "fechar candle" aos 37;
* ação fora do pregão devolve ``revalida: False`` (o candle não anda; revalidar
  seria gasto sem informação) e cripto devolve ``True`` sempre — a MESMA regra de
  :func:`agenda.alvos_da_passada`, reusada e não recopiada;
* sem cotação de referência a sessão é ``desconhecida`` e ``revalida`` é ``True``:
  não saber não é motivo pra deixar de olhar.
"""

import json
import threading
import urllib.request
from datetime import datetime, timedelta

import pytest

from tradingagents.webui import agenda, timeutil
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.server import make_server
from tradingagents.webui.store import HistoryStore

pytestmark = pytest.mark.unit


@pytest.fixture
def runner(tmp_path):
    return AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                          store=HistoryStore(tmp_path))


@pytest.mark.parametrize("tf,minutos", [("1h", 60), ("4h", 240), ("1d", 1440)])
def test_a_cadencia_e_a_do_candle_do_frame(runner, tf, minutos):
    a = runner.agenda_proxima(tf)
    assert a["cadencia_min"] == minutos == agenda.cadencia_minutos((tf,))
    assert a["atraso_s"] == agenda.ATRASO_POS_FECHAMENTO_S


@pytest.mark.parametrize("tf", ["1h", "4h", "1d"])
def test_o_proximo_fechamento_e_o_MESMO_que_a_agenda_do_scan_calcula(runner, tf):
    """DENTE: se alguém trocar a regra de horário só num dos lados, isto cai."""
    antes = timeutil.now()
    a = runner.agenda_proxima(tf)
    depois = timeutil.now()
    proxima = datetime.fromisoformat(a["proxima"])
    janela = {agenda.proxima_passada(t, (tf,), agenda.ATRASO_POS_FECHAMENTO_S)
              for t in (antes, depois)}
    assert proxima in janela
    assert 1 <= a["em_segundos"] <= agenda.cadencia_minutos((tf,)) * 60 + 60


def test_o_horario_e_ancorado_no_relogio_nao_em_quando_o_servico_subiu(runner):
    """14h37 + 1h não é 15h37: é 15h01 (fechamento do candio + atraso)."""
    ref = timeutil.now().replace(hour=14, minute=37, second=0, microsecond=0)
    p = agenda.proxima_passada(ref, ("1h",), agenda.ATRASO_POS_FECHAMENTO_S)
    assert (p.hour, p.minute, p.second) == (15, 1, 0)


def test_cripto_revalida_sempre_e_nem_pergunta_a_sessao(runner, monkeypatch):
    """24/7: a resposta não mudaria a decisão, e a tela chama isto a cada render
    de gráfico — pagar uma cotação por render pra confirmar o óbvio é custo por nada."""
    monkeypatch.setattr("tradingagents.dataflows.live_price.fetch_live_price",
                        lambda t: pytest.fail("cripto não precisa de cotação pra saber a hora"))
    a = runner.agenda_proxima("1h", "BTC-USD", "crypto")
    assert a["revalida"] is True and a["sessao"] == "24h"


def test_a_sessao_da_ACAO_vem_do_cache_de_cotacao_do_runner(runner, monkeypatch):
    """Perguntar a hora não pode martelar a fonte: duas perguntas, uma cotação."""
    n = {"i": 0}

    def conta(_t):
        n["i"] += 1
        return {"sessao": "regular"}
    monkeypatch.setattr("tradingagents.dataflows.live_price.fetch_live_price", conta)
    runner.agenda_proxima("1h", "NVDA", "stock")
    runner.agenda_proxima("4h", "NVDA", "stock")
    assert n["i"] == 1, f"a fonte foi consultada {n['i']}x — o cache não pegou"


def test_acao_fora_do_pregao_NAO_revalida(runner, monkeypatch):
    """O candio não anda: revalidar seria chamada sem informação."""
    monkeypatch.setattr("tradingagents.dataflows.live_price.fetch_live_price",
                        lambda t: {"sessao": "fechada"})
    a = runner.agenda_proxima("1h", "NVDA", "stock")
    assert a["sessao"] == "fechada" and a["revalida"] is False


def test_acao_com_pregao_aberto_revalida(runner, monkeypatch):
    monkeypatch.setattr("tradingagents.dataflows.live_price.fetch_live_price",
                        lambda t: {"sessao": "regular"})
    a = runner.agenda_proxima("1h", "NVDA", "stock")
    assert a["sessao"] == "regular" and a["revalida"] is True


def test_sem_cotacao_a_sessao_e_desconhecida_e_ainda_assim_revalida(runner, monkeypatch):
    """Não saber não é motivo pra deixar de olhar — o default de alvos_da_passada."""
    def explode(_t):
        raise RuntimeError("fonte fora do ar")
    monkeypatch.setattr("tradingagents.dataflows.live_price.fetch_live_price", explode)
    a = runner.agenda_proxima("1h", "NVDA", "stock")
    assert a["sessao"] == "desconhecida" and a["revalida"] is True


def test_sem_ticker_nao_consulta_cotacao_nenhuma(runner, monkeypatch):
    monkeypatch.setattr("tradingagents.dataflows.live_price.fetch_live_price",
                        lambda t: pytest.fail("perguntar a hora não pode custar cotação"))
    a = runner.agenda_proxima("4h")
    assert a["revalida"] is True and a["sessao"] == "desconhecida"


def test_endpoint_responde_e_nao_varre(tmp_path, monkeypatch):
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path))
    monkeypatch.setattr("tradingagents.webui.runner.scan_watchlist",
                        lambda *a, **k: pytest.fail("perguntar a hora não pode varrer"))
    httpd = make_server("127.0.0.1", 0, runner=runner)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/api/agenda/proxima?tf=4h"
        with urllib.request.urlopen(url, timeout=10) as resp:
            a = json.loads(resp.read().decode())
        assert a["tf"] == "4h" and a["cadencia_min"] == 240
        assert datetime.fromisoformat(a["proxima"]) > datetime.fromisoformat(a["agora"])
        assert datetime.fromisoformat(a["proxima"]) - datetime.fromisoformat(a["agora"]) \
            <= timedelta(minutes=240, seconds=61)
    finally:
        httpd.shutdown()

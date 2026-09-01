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


# ============ GENERALIDADE POR CLASSE (task 20260831-020) ======================
#
# "o exemplo foi MSFT mas é pra valer pra todos os atuais e futuros". A watchlist
# REAL de 31/08 tem 16 ``stock``, 3 ``crypto`` e **1 sem ``asset_type``** (GOLD,
# "Gold Dec 26" — futuro de ouro). A amostra abaixo é essa realidade, e não uma
# lista de símbolos preferidos: o que se parametriza é a CLASSE, porque é a classe
# que muda o comportamento.
#
# **Consequência declarada, não escondida:** o produto tem hoje DUAS classes de
# agendamento — cripto e todo-o-resto. ``agenda.alvos_da_passada`` diz isso por
# escrito ("``asset_type`` ausente conta como AÇÃO — o erro seguro aqui é varrer de
# menos"), então FUTURO e ativo SEM CLASSE seguem o pregão da referência. Para o
# GOLD isso quer dizer não revalidar sozinho fora da sessão americana, mesmo que o
# futuro de ouro negocie quase 24h. É decisão anterior a esta entrega e continua
# valendo; o teste abaixo a PINA para que mudá-la seja uma escolha visível, e não
# um efeito colateral.
CLASSES_DE_ATIVO = [
    pytest.param("crypto", True, id="cripto"),
    pytest.param("stock", False, id="acao"),
    pytest.param("", False, id="sem_classe_futuro_GOLD"),
    pytest.param("commodity", False, id="classe_desconhecida"),
]


@pytest.mark.parametrize("classe,revalida_fora_do_pregao", CLASSES_DE_ATIVO)
@pytest.mark.parametrize("tf", ["1h", "4h", "1d"])
def test_pregao_fechado_por_CLASSE_e_por_frame(runner, monkeypatch, tf, classe,
                                               revalida_fora_do_pregao):
    monkeypatch.setattr("tradingagents.dataflows.live_price.fetch_live_price",
                        lambda t: {"sessao": "fechada"})
    a = runner.agenda_proxima(tf, "QUALQUER", classe)
    assert a["revalida"] is revalida_fora_do_pregao, (classe, tf, a)
    assert a["cadencia_min"] == agenda.cadencia_minutos((tf,))


@pytest.mark.parametrize("classe,_r", CLASSES_DE_ATIVO)
@pytest.mark.parametrize("tf", ["1h", "4h", "1d"])
def test_com_o_pregao_ABERTO_toda_classe_revalida(runner, monkeypatch, tf, classe, _r):
    monkeypatch.setattr("tradingagents.dataflows.live_price.fetch_live_price",
                        lambda t: {"sessao": "regular"})
    assert runner.agenda_proxima(tf, "QUALQUER", classe)["revalida"] is True


@pytest.mark.parametrize("simbolo", ["NVDA", "BTC-USD", "GC=F", "^GSPC", "XAUUSD+"])
def test_o_horario_nao_depende_do_SIMBOLO(runner, monkeypatch, simbolo):
    """A hora do fechamento do candle é do FRAME, não do papel.

    DENTE: se alguém enfiar um caso especial por símbolo aqui, dois ativos no
    mesmo frame passariam a fechar candle em minutos diferentes.
    """
    monkeypatch.setattr("tradingagents.dataflows.live_price.fetch_live_price",
                        lambda t: {"sessao": "regular"})
    horas = {runner.agenda_proxima("1h", s, "stock")["proxima"]
             for s in ["NVDA", simbolo]}
    assert len(horas) == 1, horas


def test_o_agendamento_nao_conhece_ticker_nenhum():
    """NADA de lista chumbada: a classe vem do DADO (`asset_type` do payload)."""
    import inspect

    from tradingagents.webui.runner import AnalysisRunner as _AR

    fonte = inspect.getsource(_AR.agenda_proxima)
    for nome in ("MSFT", "NVDA", "AAPL", "BTC-USD", "GOLD", "GC=F"):
        assert nome not in fonte, f"{nome} chumbado no agendamento"


# ============ A AGENDA DO SCAN (DA-141) =======================================
#
# Irmã da de cima, e a diferença é a PERGUNTA. `/api/agenda/proxima` responde pelo
# candle de um frame de um ativo — é o que a revalidação do gráfico precisa saber.
# `/api/agenda/scan` responde pela PASSADA: quando a agenda varre a watchlist e
# grava o `last_scan.json`, que é o arquivo de onde a faixa de frames do card
# (DA-133) lê. A faixa se agendava... nunca: `carregaFaixaDoScan` tinha uma única
# chamada, no boot, e o card ficava com o estado de quando a aba abriu.
#
# O dente comum aos dois: o relógio é o de :mod:`agenda`, comparado com as FUNÇÕES
# e não com uma constante decorada aqui — trocar a regra num lado sem o outro cai.


def test_a_cadencia_do_scan_e_a_do_candle_mais_rapido_que_ele_le(runner):
    """DENTE: o front não pode contar "60 minutos" por conta própria.

    A cadência é a de :func:`agenda.cadencia_minutos` sobre ``SCAN_FRAMES``. No dia
    em que o scan ganhar o 15m, a faixa passa a reler de 15 em 15 sozinha — porque
    ela obedece a este número em vez de ter o seu.
    """
    a = runner.agenda_do_scan()
    assert a["cadencia_min"] == agenda.cadencia_minutos()
    assert a["atraso_s"] == agenda.ATRASO_POS_FECHAMENTO_S


def test_a_passada_do_scan_e_a_MESMA_que_o_laco_da_agenda_executa(runner):
    """DENTE: dois relógios divergindo é o defeito que isto existe pra impedir."""
    antes = timeutil.now()
    a = runner.agenda_do_scan()
    depois = timeutil.now()
    proxima = datetime.fromisoformat(a["proxima"])
    assert proxima in {agenda.proxima_passada(t) for t in (antes, depois)}
    assert 1 <= a["em_segundos"] <= agenda.cadencia_minutos() * 60 + 60


def test_a_LEITURA_espera_a_passada_gravar(runner):
    """A passada não é instantânea: varre a watchlist e SÓ ENTÃO grava.

    DENTE: reler no instante do fechamento leria o arquivo ANTERIOR e a tela
    concluiria que nada mudou — o mesmo congelamento, uma hora mais tarde. A folga
    sai de :data:`agenda.MARGEM_LEITURA_S` (medida na passada real), e vem SOMADA
    do servidor pra o JavaScript não ter margem própria.
    """
    a = runner.agenda_do_scan()
    assert a["margem_s"] == agenda.MARGEM_LEITURA_S
    assert a["ler_em_segundos"] == a["em_segundos"] + agenda.MARGEM_LEITURA_S
    assert a["ler_em_segundos"] > a["em_segundos"]


def test_perguntar_quando_ler_NAO_varre_e_NAO_custa_cotacao(runner, monkeypatch):
    """$0: é aritmética de calendário. Uma varredura por pergunta seria o oposto
    do que a faixa promete (uma leitura de arquivo, sem LLM)."""
    monkeypatch.setattr("tradingagents.webui.runner.scan_watchlist",
                        lambda *a, **k: pytest.fail("perguntar a hora não pode varrer"))
    monkeypatch.setattr("tradingagents.dataflows.live_price.fetch_live_price",
                        lambda t: pytest.fail("perguntar a hora não pode custar cotação"))
    assert runner.agenda_do_scan()["ler_em_segundos"] > 0


def test_o_endpoint_da_agenda_do_scan_responde(tmp_path, monkeypatch):
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path))
    monkeypatch.setattr("tradingagents.webui.runner.scan_watchlist",
                        lambda *a, **k: pytest.fail("perguntar a hora não pode varrer"))
    httpd = make_server("127.0.0.1", 0, runner=runner)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/api/agenda/scan"
        with urllib.request.urlopen(url, timeout=10) as resp:
            a = json.loads(resp.read().decode())
        assert a["cadencia_min"] == agenda.cadencia_minutos()
        assert a["ler_em_segundos"] == a["em_segundos"] + agenda.MARGEM_LEITURA_S
        assert datetime.fromisoformat(a["proxima"]) > datetime.fromisoformat(a["agora"])
        assert datetime.fromisoformat(a["proxima"]) - datetime.fromisoformat(a["agora"]) \
            <= timedelta(minutes=agenda.cadencia_minutos(), seconds=61)
    finally:
        httpd.shutdown()

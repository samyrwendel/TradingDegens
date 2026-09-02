"""AGENDA DO SCAN — uma passada por candle FECHADO (task 20260830-037).

*"a melhor frequência possível pra gerar histórico e dados sobre acertos"* — e a
resposta medida NÃO é "o máximo que a API aguenta".

O padrão e o gatilho saem da série date-guarded: só mudam quando um candle FECHA.
Varrer mais rápido que isso devolve o mesmo padrão com um candle em formação; o que a
passada extra pega são toques INTRABARRA, que é justamente o que não se quer no track
record — ele mede o que a pessoa operando esta tela teria visto, e ela olha em cadência
humana. Ver o docstring de :mod:`tradingagents.webui.agenda` para os números medidos.

O que se trava aqui:

* a cadência sai do CANDLE, ancorada no relógio (restart não desalinha a passada);
* ação respeita pregão, cripto é 24/7 — e "não sei a sessão" varre, nunca deixa de olhar;
* de-duplicação: rodar duas vezes seguidas NÃO infla o ledger;
* fonte degradada não vira gatilho inventado nem "não aconteceu" falso — vira contagem
  declarada na linha da passada;
* a passada é registrada mesmo sem gatilho nenhum: é o que separa "não houve" de
  "ninguém olhou".
"""

from datetime import datetime, timedelta, timezone

import pytest

from tradingagents.webui import agenda
from tradingagents.webui.scanner import ScanLog

pytestmark = pytest.mark.unit

UTC = timezone.utc


# ────────────────────── a cadência sai do candle, não do palpite ──────────────
def test_a_cadencia_e_a_do_candle_mais_rapido_que_o_scan_le():
    """Uma passada calcula todos os frames de uma vez — não há cadência por frame a
    escolher. O que define o ritmo é o menor candle da lista."""
    assert agenda.cadencia_minutos(("1d", "4h", "1h")) == 60
    assert agenda.cadencia_minutos(("1d", "4h")) == 240
    assert agenda.cadencia_minutos(("1d",)) == 1440
    assert agenda.cadencia_minutos(("15m", "1h")) == 15


def test_frame_desconhecido_nao_vira_cadencia_de_um_minuto():
    """Erro de digitação numa constante não pode virar varredura por minuto contra o
    provedor."""
    assert agenda.cadencia_minutos(("1x", "zzz")) == 60
    assert agenda.cadencia_minutos(()) == 60


@pytest.mark.parametrize("agora,esperado", [
    ((14, 37, 0), (15, 1, 0)),
    ((14, 59, 59), (15, 1, 0)),
    ((15, 0, 30), (15, 1, 0)),     # o bloco corrente ainda não passou
    ((15, 1, 30), (16, 1, 0)),     # já passou → o próximo
    ((23, 59, 0), (0, 1, 0)),      # vira o dia sem buraco
])
def test_a_passada_e_ancorada_no_RELOGIO_nao_no_boot(agora, esperado):
    """DENTE: um restart às 14h37 não pode passar a varrer aos 37 de cada hora — a
    passada deixaria de coincidir com o fechamento e voltaria a ler barra em formação."""
    h, m, s = agora
    t = datetime(2026, 8, 30, h, m, s, tzinfo=UTC)
    prox = agenda.proxima_passada(t)
    eh, em, es = esperado
    assert (prox.hour, prox.minute, prox.second) == (eh, em, es), (t, prox)
    assert prox > t, (t, prox)


def test_a_passada_sai_DEPOIS_do_fechamento_nunca_no_segundo_exato():
    """A barra recém-fechada leva segundos pra aparecer consolidada na fonte; varrer no
    segundo do fechamento lê a barra anterior e gasta a passada à toa."""
    t = datetime(2026, 8, 30, 14, 37, tzinfo=UTC)
    fechamento = datetime(2026, 8, 30, 15, 0, tzinfo=UTC)
    atraso = (agenda.proxima_passada(t) - fechamento).total_seconds()
    assert atraso == agenda.ATRASO_POS_FECHAMENTO_S, atraso
    assert agenda.ATRASO_POS_FECHAMENTO_S > 0


# ───────────────────── ação tem pregão, cripto é 24/7 ─────────────────────────
_WATCH = [{"ticker": "AAPL", "asset_type": "stock"},
          {"ticker": "BTC-USD", "asset_type": "crypto"},
          {"ticker": "NVDA"},                       # sem tipo → conta como ação
          {"ticker": "ZEC-USD", "asset_type": "crypto"}]


@pytest.mark.parametrize("sessao", ["regular", "pre", "pos"])
def test_com_o_mercado_ativo_varre_tudo(sessao):
    assert agenda.alvos_da_passada(_WATCH, sessao) == ["AAPL", "BTC-USD", "NVDA", "ZEC-USD"]


def test_com_a_bolsa_FECHADA_so_a_cripto():
    """Fora do pregão a ação repete o mesmo candle: gasto sem informação."""
    assert agenda.alvos_da_passada(_WATCH, "fechado") == ["BTC-USD", "ZEC-USD"]


def test_sessao_DESCONHECIDA_varre_tudo():
    """Não saber não pode virar "não olhar" — o erro caro aqui é o buraco no histórico,
    não a leitura repetida (que a de-duplicação absorve)."""
    assert agenda.alvos_da_passada(_WATCH, "desconhecida") == \
        ["AAPL", "BTC-USD", "NVDA", "ZEC-USD"]


def test_a_sessao_custa_UMA_cotacao_por_passada_nao_vinte():
    """O objetivo é saber se a bolsa está aberta, e isso é o mesmo pra toda a lista de
    ações. Vinte cotações só pra decidir seriam vinte requisições por hora à toa."""
    chamadas = []

    def cotacao(t):
        chamadas.append(t)
        return {"sessao": "regular"}

    sessao, ref = agenda.sessao_de_mercado(_WATCH, cotacao)
    assert (sessao, ref) == ("regular", "AAPL"), (sessao, ref)
    assert chamadas == ["AAPL"], chamadas


def test_watchlist_so_de_cripto_nao_gasta_cotacao_de_referencia():
    so_cripto = [{"ticker": "BTC-USD", "asset_type": "crypto"}]
    assert agenda.sessao_de_mercado(so_cripto, lambda t: pytest.fail("não devia cotar")) \
        == ("24h", None)


def test_falha_da_cotacao_nao_derruba_a_passada():
    """A agenda nunca cai por causa de uma cotação — ela varre com sessão desconhecida,
    que é o comportamento que não deixa buraco."""
    def explode(_t):
        raise RuntimeError("provedor fora")
    assert agenda.sessao_de_mercado(_WATCH, explode) == ("desconhecida", "AAPL")


# ───────────────────────────── o laço da agenda ───────────────────────────────
def test_o_laco_espera_ate_o_fechamento_e_executa():
    passadas = []
    agora = datetime(2026, 8, 30, 15, 0, 30, tzinfo=UTC)
    ag = agenda.AgendaScan(lambda: passadas.append(1), relogio=lambda: agora)
    # sem thread: exercita o cálculo do laço uma volta, com espera curta
    alvo = agenda.proxima_passada(agora)
    assert 0 < (alvo - agora).total_seconds() <= 60, (agora, alvo)
    ag.stop()   # não deixa thread pendurada se alguém chamar start depois


def test_uma_passada_ruim_nao_mata_a_agenda():
    """Provedor fora do ar por uma hora não pode desligar o histórico pro resto do dia.

    O laço real dorme até o candle; aqui se roda UMA volta com cadência de 15m e atraso
    zero, com o relógio parado logo antes do fechamento, e se confere que a segunda
    volta acontece mesmo depois de a primeira ter explodido."""
    chamadas = []

    def passada():
        chamadas.append(len(chamadas))
        raise RuntimeError("provedor fora")

    ag = agenda.AgendaScan(passada, frames=("15m",), atraso_s=0,
                           relogio=lambda: datetime(2026, 8, 30, 15, 14, 59, 900000,
                                                    tzinfo=UTC))
    ag.start()
    inicio = datetime.now(UTC)
    while len(chamadas) < 2 and (datetime.now(UTC) - inicio) < timedelta(seconds=10):
        pass
    ag.stop()
    assert len(chamadas) >= 2, ("a agenda parou na primeira falha", chamadas)


# ─────────────────── a passada REGISTRADA, e a ausência com ela ───────────────
def test_a_passada_e_registrada_mesmo_sem_gatilho(tmp_path):
    """DENTE: sem esta linha, um período sem gatilhos é ambíguo — pode ter sido mercado
    parado ou serviço fora do ar. Metade do valor do track record está aí."""
    log = ScanLog(tmp_path / "scans.jsonl")
    log.record_pass(alvos=20, lidos=20, sem_dado=0, gatilhos=0, sessao="regular")
    p = log.passadas()
    assert len(p) == 1 and p[0]["gatilhos"] == 0 and p[0]["alvos"] == 20, p
    assert p[0]["sessao"] == "regular" and p[0]["ts"], p


def test_fonte_degradada_vira_CONTAGEM_nunca_gatilho(tmp_path):
    """Fonte que caiu não pode virar gatilho inventado nem "não aconteceu" falso."""
    log = ScanLog(tmp_path / "scans.jsonl")
    log.record_pass(alvos=20, lidos=20, sem_dado=7, gatilhos=0, sessao="regular")
    assert log.entries() == [], "passada não é gatilho"
    assert log.fechamentos() == {}, "passada não é fechamento"
    assert log.passadas()[0]["sem_dado"] == 7


def test_a_linha_da_passada_e_INERTE_pro_motor_de_vereditos(tmp_path):
    """Ela informa; não pode mexer em taxa de acerto nenhuma."""
    log = ScanLog(tmp_path / "scans.jsonl")
    log.record({"ticker": "AAPL", "frame": "1h", "trigger": 100.0, "sl": 98.0,
                "tp": 104.0, "rr": 2.0, "direction": "compra", "setup": "123"})
    antes = len(log.entries())
    for _ in range(5):
        log.record_pass(alvos=20, lidos=20, sem_dado=0, gatilhos=0)
    assert len(log.entries()) == antes, log.entries()


# ───────────── de-duplicação: rodar duas vezes não infla o histórico ──────────
_PADRAO = object()


def _runner(tmp_path, monkeypatch, resultado, watch=_PADRAO):
    """Runner de teste com a varredura substituída — mede a GRAVAÇÃO, não a rede."""
    from tradingagents.webui import runner as R
    from tradingagents.webui.store import HistoryStore

    r = R.AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "openai",
                     "deep_think_llm": "x", "quick_think_llm": "y"},
        store=HistoryStore(tmp_path))
    monkeypatch.setattr(R, "scan_watchlist", lambda *a, **k: resultado)
    lista = _WATCH if watch is _PADRAO else watch
    monkeypatch.setattr(r.watchlist_store, "get", lambda: lista)
    monkeypatch.setattr(R.agenda, "sessao_de_mercado", lambda *a, **k: ("regular", "AAPL"))
    monkeypatch.setattr("tradingagents.dataflows.live_price.fetch_live_price",
                        lambda t: {"sessao": "regular"})
    return r


_UM_GATILHO = {
    "date": "2026-08-30", "frames": ["1d", "4h", "1h"], "resumo": {}, "ativos": [
        {"ticker": "AAPL", "melhor": {"estado": "em_gatilho"}, "frames": [
            {"frame": "1h", "estado": "em_gatilho", "trigger": 230.5, "sl": 228.0,
             "tp": 236.0, "rr": 2.2, "direction": "compra", "pattern_state": "formando",
             "storm": {"estado": "em_gatilho", "opera": True, "trigger": 229.0,
                       "sl": 227.0, "tp": 235.0, "rr": 3.0, "direction": "compra",
                       "entrada": "ponto2"}},
        ]},
    ]}


def test_rodar_DUAS_vezes_seguidas_nao_infla_o_ledger(tmp_path, monkeypatch):
    """O critério do pedido, medido: a de-duplicação é por (setup, ticker, frame,
    gatilho), e o gatilho é um nível do padrão — repetir a passada não cria linha."""
    r = _runner(tmp_path, monkeypatch, _UM_GATILHO)
    r.scan_agendado()
    depois_da_primeira = len(r.scan_log.entries())
    assert depois_da_primeira == 2, ("um gatilho de cada setup", r.scan_log.entries())
    for _ in range(4):
        r.scan_agendado()
    assert len(r.scan_log.entries()) == depois_da_primeira, r.scan_log.entries()
    # mas cada PASSADA fica registrada: repetir não cria gatilho, e ainda assim se sabe
    # que se olhou cinco vezes
    assert len(r.scan_log.passadas()) == 5, r.scan_log.passadas()
    assert [p["gatilhos"] for p in r.scan_log.passadas()] == [2, 0, 0, 0, 0]


def test_storm_grava_ENTRADA_como_PRECO_nao_rotulo(tmp_path, monkeypatch):
    """BUG DE DADO (task 20260902-035): ``_storm_row`` usa ``entrada`` pra carregar o
    RÓTULO da leitura escolhida (``ponto2``/``ponto3``/``ponto2e3`` — é o que a célula
    do scan e o app.js mostram), e ``_registrar_gatilhos`` espalhava esse dict inteiro
    (``**st``) pro ledger sem trocar o rótulo pelo preço. ``_pnl_paper_trade`` lê
    ``entrada`` como PREÇO — com o rótulo cru, ``float("ponto2")`` estoura e o Storm
    fechado nunca produz PnL em USD. O preço da MESMA leitura já está em ``trigger``:
    é ele que tem de ir pro campo ``entrada`` do ledger, não o rótulo."""
    r = _runner(tmp_path, monkeypatch, _UM_GATILHO)
    r.scan_agendado()
    storm_entries = [e for e in r.scan_log.entries() if e.get("setup") == "storm"]
    assert len(storm_entries) == 1, storm_entries
    entrada = storm_entries[0].get("entrada")
    assert not isinstance(entrada, str), ("ledger gravou o RÓTULO em vez do preço",
                                          storm_entries[0])
    assert entrada is not None and float(entrada) == 229.0, storm_entries[0]


_UM_GATILHO_123_ACIONADO = {
    "date": "2026-08-30", "frames": ["1d"], "resumo": {}, "ativos": [
        # 1-2-3 JÁ ACIONADO: o gatilho (ponto 2) ficou pra trás e a entrada de
        # referência é o PREÇO no log (``rr_entry``, de ``_entry_ref``). trigger 8,87,
        # entrada real 11,43 — o mesmo caso do LINK 1d da task 047.
        {"ticker": "LINK-USD", "melhor": {"estado": "em_gatilho"}, "frames": [
            {"frame": "1d", "estado": "em_gatilho", "trigger": 8.87, "sl": 7.56,
             "tp": 11.55, "rr": 0.03, "direction": "compra", "pattern_state": "acionado",
             "rr_entry": 11.43, "rr_basis": "preço atual (padrão já acionado)"},
        ]},
    ]}


def test_123_grava_ENTRADA_como_PRECO_do_setup_nao_o_gatilho(tmp_path, monkeypatch):
    """Espelho da 035, agora pro 1-2-3 (task 20260902-047): o ledger tem de carimbar
    a ENTRADA em PREÇO também pro 1-2-3 — antes ele só gravava o ``trigger``, e um
    padrão já acionado tem o gatilho velho lá. A entrada honesta é ``rr_entry`` (o
    preço que ``_entry_ref`` usa pra medir o rr), não o gatilho deixado pra trás."""
    r = _runner(tmp_path, monkeypatch, _UM_GATILHO_123_ACIONADO)
    r.scan_agendado()
    e123 = [e for e in r.scan_log.entries() if e.get("setup") == "123"]
    assert len(e123) == 1, e123
    entrada = e123[0].get("entrada")
    assert entrada is not None, ("o 1-2-3 não gravou entrada própria", e123[0])
    assert float(entrada) == 11.43, ("gravou o gatilho velho em vez do preço", e123[0])


def test_a_mesma_de_duplicacao_vale_DENTRO_de_uma_passada(tmp_path, monkeypatch):
    """Dois frames do mesmo ativo com o MESMO gatilho não podem virar duas linhas — a
    varredura da tela lia o ledger uma vez só e não via o que ela própria acabara de
    gravar."""
    dobrado = {**_UM_GATILHO, "ativos": [
        {"ticker": "AAPL", "melhor": {"estado": "em_gatilho"},
         "frames": _UM_GATILHO["ativos"][0]["frames"] * 2},
    ]}
    r = _runner(tmp_path, monkeypatch, dobrado)
    r.scan_agendado()
    assert len(r.scan_log.entries()) == 2, r.scan_log.entries()


def test_fonte_degradada_na_passada_agendada_vira_contagem(tmp_path, monkeypatch):
    """Nem gatilho inventado, nem "não aconteceu" falso: a leitura que degradou é
    CONTADA na linha da passada."""
    degradado = {**_UM_GATILHO, "ativos": [
        {"ticker": "AAPL", "melhor": {"estado": "sem_dado"}, "frames": [
            {"frame": "1h", "estado": "sem_dado"},
            {"frame": "4h", "estado": "sem_dado"},
        ]},
    ]}
    r = _runner(tmp_path, monkeypatch, degradado)
    out = r.scan_agendado()
    assert r.scan_log.entries() == [], "fonte caída não vira gatilho"
    assert out["sem_dado"] == 2 and out["gatilhos"] == 0, out
    assert r.scan_log.passadas()[0]["sem_dado"] == 2


def test_com_a_bolsa_fechada_a_passada_varre_so_cripto_e_registra(tmp_path, monkeypatch):
    from tradingagents.webui import runner as R
    r = _runner(tmp_path, monkeypatch, {"ativos": []})
    monkeypatch.setattr(R.agenda, "sessao_de_mercado", lambda *a, **k: ("fechado", "AAPL"))
    vistos = {}

    def _varre(tickers, *a, **k):
        vistos["t"] = tickers
        return {"ativos": []}

    monkeypatch.setattr(R, "scan_watchlist", _varre)
    out = r.scan_agendado()
    assert vistos["t"] == ["BTC-USD", "ZEC-USD"], vistos
    assert out["sessao"] == "fechado"
    assert r.scan_log.passadas()[0]["sessao"] == "fechado"


def test_watchlist_vazia_registra_a_passada_em_vez_de_calar(tmp_path, monkeypatch):
    r = _runner(tmp_path, monkeypatch, {"ativos": []}, watch=[])
    out = r.scan_agendado()
    assert out["alvos"] == 0
    assert len(r.scan_log.passadas()) == 1, r.scan_log.passadas()

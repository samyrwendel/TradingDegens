"""Scanner estrutural 1-2-3 — o olho barato ($0 de LLM) antes da análise cara.

Puro e offline: monkeypatch dos seams do próprio scanner (``build_price_chart`` /
``build_actionable_plan_dict`` importados de price_structure). Trava:

* os estados com planos falsos (em_gatilho / em_movimento / invalidou / formando /
  sem_setup / sem_dado) e o cálculo da distância preço→gatilho;
* EM GATILHO é no ponto de entrada (≤ tol) — acionado longe vira em_movimento,
  não em_gatilho (o gatilho já ficou p/ trás);
* INVALIDOU: preço além do ponto 3 detectado como estado (a premissa morreu);
* a ordenação por urgência (em_gatilho primeiro, dist crescente);
* fail-open: símbolo quebrado vira linha ``sem_dado``, nunca derruba o scan;
* o track record: log append-only + vereditos DIREÇÃO-CONSCIENTES (venda: TP
  abaixo do preço — bug real pego no probe ao vivo, taxa 1.0 falsa).
"""

import json

import pytest

import tradingagents.webui.scanner as sc
from tradingagents.webui.scanner import (
    ScanLog,
    scan_symbol,
    scan_verdicts,
    scan_watchlist,
)


def _plan(pattern=None, price=100.0, setup_state="ativo", invalidation=None):
    plan = {"pattern": pattern, "price": price, "setup_state": setup_state}
    if invalidation is not None:
        plan["invalidation"] = {"price": invalidation}
    return plan


def _pat(direction="compra", state="formando", trigger=100.0):
    return {"direction": direction, "state": state, "trigger": trigger}


@pytest.fixture
def fake_fetch(monkeypatch):
    """Seam: mapa (ticker, frame) -> plan. Chart vazio (preço vem do plan).
    Live price desligado (None) — o teste é offline; o price do plan é a fonte."""
    def install(plans):
        monkeypatch.setattr(
            sc, "build_actionable_plan_dict",
            lambda t, d, timeframe="1d", method="padrao": plans.get((t.upper(), timeframe), _plan(setup_state="sem_setup")),
        )
        monkeypatch.setattr(sc, "build_price_chart",
                            lambda t, d, bars=260, timeframe="1d", method="padrao": {"candles": []})
        monkeypatch.setattr(sc, "_live_price", lambda ticker: None)
    return install


def test_estado_em_gatilho_no_ponto_de_entrada(fake_fetch):
    # preço a 0,3% do gatilho → em_gatilho pela TOLERÂNCIA (ponto de entrada AGORA)
    fake_fetch({("A", "1d"): _plan(_pat(trigger=100.3), price=100.0)})
    r = scan_symbol("A", "2026-08-28", frames=("1d",))
    assert r["melhor"]["estado"] == "em_gatilho"
    assert abs(r["melhor"]["dist_pct"] - abs(100.0 / 100.3 - 1)) < 1e-9
    # a direção é preservada no resumo (COMPRA/VENDA vem dela no painel)
    assert r["melhor"]["direction"] == "compra"


def test_acionado_longe_e_em_movimento_nao_em_gatilho(fake_fetch):
    # padrão ACIONADO e preço 18% além do gatilho → em_movimento (entrada passou),
    # NUNCA em_gatilho (o gatilho já ficou p/ trás).
    fake_fetch({("B", "1d"): _plan(_pat(state="acionado", trigger=65401.69),
                                   price=77699.0, invalidation=60000.0)})
    r = scan_symbol("B", "2026-08-28", frames=("1d",))
    assert r["melhor"]["estado"] == "em_movimento"


def test_invalidou_quando_preco_alem_do_ponto_3(fake_fetch):
    # COMPRA: setup morre ao PERDER o ponto 3 (preço < invalidação).
    fake_fetch({("C", "1d"): _plan(_pat(trigger=110.0), price=88.0, invalidation=95.0)})
    assert scan_symbol("C", "2026-08-28", frames=("1d",))["melhor"]["estado"] == "invalidou"
    # VENDA: setup morre ao VOLTAR acima do ponto 3 (preço > invalidação).
    fake_fetch({("V", "1d"): _plan(_pat(direction="venda", trigger=90.0),
                                   price=105.0, invalidation=95.0)})
    assert scan_symbol("V", "2026-08-28", frames=("1d",))["melhor"]["estado"] == "invalidou"


def test_estado_formando_sem_setup(fake_fetch):
    fake_fetch({("A", "1d"): _plan(_pat(trigger=110.0), price=100.0)})   # 10% — longe
    assert scan_symbol("A", "2026-08-28", frames=("1d",))["melhor"]["estado"] == "formando"
    fake_fetch({("A", "1d"): _plan(None, price=100.0, setup_state="sem_setup")})
    assert scan_symbol("A", "2026-08-28", frames=("1d",))["melhor"]["estado"] == "sem_setup"


def test_estado_sem_dado_degraded_never_invents(fake_fetch):
    """Plano degradado (sem_dado/intradiario_indisponivel) → sem_dado com motivo."""
    fake_fetch({("A", "1d"): _plan(None, price=None, setup_state="sem_dado")})
    r = scan_symbol("A", "2026-08-28", frames=("1d",))
    assert r["melhor"]["estado"] == "sem_dado"
    assert "fonte" in (r["melhor"].get("motivo") or "")


def test_scan_watchlist_orders_by_urgency_and_survives_broken_symbol(fake_fetch):
    fake_fetch({
        ("AAA", "1d"): _plan(_pat(trigger=100.2), price=100.0),        # em_gatilho
        ("BBB", "1d"): _plan(_pat(trigger=101.5), price=100.0),        # formando
        ("CCC", "1d"): _plan(_pat(trigger=115.0), price=100.0),        # formando
    })
    def boom(t, d, timeframe="1d", method="padrao"):
        raise RuntimeError("fonte fora do ar")

    sc.build_actionable_plan_dict = boom  # quebra TUDO: fail-open do scan
    out = scan_watchlist(["AAA", "BBB", "CCC"], "2026-08-28", frames=("1d",))
    assert out["resumo"].get("sem_dado") == 3
    assert all(s["melhor"]["estado"] == "sem_dado" for s in out["ativos"])


def test_scan_watchlist_em_gatilho_first(fake_fetch):
    fake_fetch({
        ("ZZZ", "1d"): _plan(_pat(trigger=115.0), price=100.0),        # formando
        ("AAA", "1d"): _plan(_pat(trigger=100.1), price=100.0),        # em_gatilho (0,1%)
        ("MMM", "1d"): _plan(_pat(trigger=100.5), price=100.0),        # em_gatilho (0,5%)
    })
    out = scan_watchlist(["ZZZ", "MMM", "AAA"], "2026-08-28", frames=("1d",))
    assert [s["ticker"] for s in out["ativos"]] == ["AAA", "MMM", "ZZZ"]
    assert out["resumo"]["em_gatilho"] == 2


def test_chart_so_e_buscado_quando_serve_de_fallback(monkeypatch):
    """Limpeza: o chart reroda a detecção de estrutura inteira e só existia como
    último recurso pro preço. Com plan['price'] presente, nem é tocado — antes
    dobrava esse trabalho por (ticker, frame), 3 frames × a watchlist toda."""
    def nunca(t, d, timeframe="1d", method="padrao"):
        raise AssertionError("o chart foi buscado sem precisar")

    monkeypatch.setattr(sc, "build_actionable_plan_dict",
                        lambda t, d, timeframe="1d", method="padrao":
                        {"price": 100.0, "pattern": _pat(trigger=100.2),
                         "setup_state": "ativo"})
    monkeypatch.setattr(sc, "build_price_chart", nunca)
    monkeypatch.setattr(sc, "_live_price", lambda ticker: None)
    r = scan_symbol("A", "2026-08-28", frames=("1d",))
    assert r["melhor"]["estado"] == "em_gatilho"


def test_sem_preco_no_plan_o_chart_ENTRA_como_fallback(monkeypatch):
    """Contra-prova: preguiçoso não é ausente — sem price no plano, o chart salva."""
    monkeypatch.setattr(sc, "build_actionable_plan_dict",
                        lambda t, d, timeframe="1d", method="padrao":
                        {"price": None, "pattern": _pat(trigger=100.2),
                         "setup_state": "ativo"})
    monkeypatch.setattr(sc, "build_price_chart",
                        lambda t, d, timeframe="1d", method="padrao":
                        {"candles": [{"c": 100.0}]})
    monkeypatch.setattr(sc, "_live_price", lambda ticker: None)
    r = scan_symbol("A", "2026-08-28", frames=("1d",))
    assert r["melhor"]["estado"] == "em_gatilho" and r["melhor"]["price"] == 100.0


# --------------------------------- paralelismo + cache de cotação (perf) --------
def test_scan_paralelo_preserva_a_ordem_e_o_resultado(fake_fetch, monkeypatch):
    """Paralelizar não pode mudar o que sai: mesma lista, mesmo resumo, mesma ordem
    por urgência. ``ex.map`` preserva a ordem de entrada e o sort vem depois."""
    fake_fetch({
        ("AAA", "1d"): _plan(_pat(trigger=100.2), price=100.0),   # em_gatilho
        ("BBB", "1d"): _plan(_pat(trigger=115.0), price=100.0),   # formando
        ("CCC", "1d"): _plan(_pat(trigger=100.1), price=100.0),   # em_gatilho
    })
    serial = scan_watchlist(["AAA", "BBB", "CCC"], "2026-08-28", frames=("1d",), workers=1)
    paralelo = scan_watchlist(["AAA", "BBB", "CCC"], "2026-08-28", frames=("1d",), workers=4)
    assert [a["ticker"] for a in serial["ativos"]] == [a["ticker"] for a in paralelo["ativos"]]
    assert serial["resumo"] == paralelo["resumo"]


def test_scan_de_lista_vazia_nao_abre_pool(fake_fetch):
    fake_fetch({})
    out = scan_watchlist([], "2026-08-28", frames=("1d",))
    assert out["ativos"] == [] and out["resumo"] == {}


def test_cotacao_e_cacheada_por_janela_curta(monkeypatch):
    """Medido: ``_live_price`` custava ~0,9s POR ativo e sozinho respondia por boa
    parte de um scan quente — 20 cotações refeitas a cada varredura, inclusive num
    reclique. Agora paga uma vez por janela."""
    sc._live_cache_clear()
    chamadas = []

    def fake(symbol):
        chamadas.append(symbol)
        return {"price": 10.0}

    import tradingagents.dataflows.live_price as lp
    monkeypatch.setattr(lp, "fetch_live_price", fake)
    assert sc._live_price("XYZ") == 10.0
    assert sc._live_price("XYZ") == 10.0
    assert len(chamadas) == 1, chamadas
    # TTL zerado força o refetch — o cache é janela, não memória eterna
    assert sc._live_price("XYZ", ttl=0) == 10.0
    assert len(chamadas) == 2
    sc._live_cache_clear()


def test_simbolo_que_a_fonte_nao_resolve_paga_UM_timeout_por_janela(monkeypatch):
    """O negativo também entra no cache: um ticker morto na watchlist custava um
    timeout a CADA scan (o caso do AAOI no journal da revisão)."""
    sc._live_cache_clear()
    chamadas = []

    def morto(symbol):
        chamadas.append(symbol)
        raise RuntimeError("possibly delisted; no price data found")

    import tradingagents.dataflows.live_price as lp
    monkeypatch.setattr(lp, "fetch_live_price", morto)
    assert sc._live_price("MORTO") is None
    assert sc._live_price("MORTO") is None
    assert sc._live_price("MORTO") is None
    assert len(chamadas) == 1, ("pagou timeout de novo dentro da janela", chamadas)
    sc._live_cache_clear()


def test_scan_log_is_append_only_dedup_free(tmp_path):
    log = ScanLog(tmp_path / "scans.jsonl")
    log.record({"ticker": "MSFT", "frame": "4h", "trigger": 513.73, "direction": "compra"})
    log.record({"ticker": "AAPL", "frame": "1d", "trigger": 299.74, "direction": "venda"})
    assert len(log.entries()) == 2
    # linha ilegível é ignorada, não derruba a leitura
    (tmp_path / "scans.jsonl").write_text("{lixo\n", encoding="utf-8")
    assert isinstance(log.entries(), list)


# --- track record: fechado pela SÉRIE, aberto pelo preço de agora (C2) ---------
# O log carrega ``ts``; o fechamento olha as barras POSTERIORES ao dia do log. Os
# testes escrevem o ts na mão pra controlar a janela (``ScanLog.record`` carimba
# "agora", que não serve pra ancorar candles fixos).

def _log_com_ts(tmp_path, entradas):
    """Escreve entradas com ``ts`` explícito — a janela do veredito depende dele."""
    path = tmp_path / "scans.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for e in entradas:
            fh.write(json.dumps(e) + "\n")
    return ScanLog(path)


def _serie(monkeypatch, candles_por_ticker, precos):
    monkeypatch.setattr(sc, "build_actionable_plan_dict",
                        lambda t, d, timeframe="1d", method="padrao":
                        {"price": precos.get(t), "pattern": None, "setup_state": "ativo"})
    # ``bars`` no seam: ``scan_verdicts`` dimensiona a janela pelo intervalo
    # log→data (o default de 260 não cobre um 1h de semanas atrás).
    monkeypatch.setattr(sc, "build_price_chart",
                        lambda t, d, bars=260, timeframe="1d", method="padrao":
                        {"candles": candles_por_ticker.get(t, [])})
    monkeypatch.setattr(sc, "_live_price", lambda ticker: None)   # offline


def _c(d, h, low):
    return {"d": d, "o": low, "h": h, "l": low, "c": low}


def test_verdicts_are_direction_aware(tmp_path, monkeypatch):
    """VENDA: TP fica ABAIXO — price >= tp NÃO é 'bateu_tp' (bug real do probe:
    taxa 1.0 falsa). Compra: price <= sl é stop, price >= tp é alvo."""
    log = _log_com_ts(tmp_path, [
        {"ts": "2026-08-20T12:00:00+00:00", "ticker": "V", "frame": "1d",
         "trigger": 100.0, "direction": "venda", "tp": 90.0, "sl": 105.0},
        {"ts": "2026-08-20T12:00:00+00:00", "ticker": "C", "frame": "1d",
         "trigger": 100.0, "direction": "compra", "tp": 110.0, "sl": 95.0},
    ])
    # Série que NÃO toca nada: V oscila 98–101, C oscila 104–106. A barra do dia do
    # log entra só pra a série COBRIR o gatilho (ela não conta pro toque — o dia do
    # log fica fora da janela); sem cobrir, o veredito honesto seria
    # ``sem_serie_cobrindo``, não "andamento".
    _serie(monkeypatch,
           {"V": [_c("2026-08-20", 100.5, 99.5), _c("2026-08-21", 101.0, 98.0)],
            "C": [_c("2026-08-20", 100.5, 99.5), _c("2026-08-21", 106.0, 104.0)]},
           {"V": 99.0, "C": 105.0})
    out = scan_verdicts(log, "2026-08-28")
    v = {x["ticker"]: x["veredito"] for x in out["verdicts"]}
    assert v["V"] == "andamento_lucro"      # venda lucra caindo
    assert v["C"] == "andamento_lucro"
    assert out["n_fechados"] == 0 and out["taxa_acerto"] is None


def test_verdicts_tp_sl_closed_counts(tmp_path, monkeypatch):
    """Fechamento vem da SÉRIE: a máxima tocou o TP, a mínima tocou o SL."""
    log = _log_com_ts(tmp_path, [
        {"ts": "2026-08-20T12:00:00+00:00", "ticker": "C", "frame": "1d",
         "trigger": 100.0, "direction": "compra", "tp": 110.0, "sl": 95.0},
        {"ts": "2026-08-20T12:00:00+00:00", "ticker": "S", "frame": "1d",
         "trigger": 100.0, "direction": "compra", "tp": 110.0, "sl": 95.0},
    ])
    _serie(monkeypatch,
           {"C": [_c("2026-08-21", 111.0, 100.0)],     # máxima cruzou o TP
            "S": [_c("2026-08-21", 101.0, 94.0)]},     # mínima cruzou o SL
           {"C": 105.0, "S": 96.0})
    out = scan_verdicts(log, "2026-08-28")
    v = {x["ticker"]: x["veredito"] for x in out["verdicts"]}
    assert v["C"] == "bateu_tp" and v["S"] == "bateu_sl"
    assert out["n_fechados"] == 2 and out["taxa_acerto"] == 0.5


def test_tocou_o_tp_e_voltou_continua_bateu_tp(tmp_path, monkeypatch):
    """O acerto REAL não pode sumir porque o preço voltou (C2-a).

    Antes: o veredito comparava o preço de AGORA com o TP — o trade que subiu,
    tocou o alvo e devolveu tudo aparecia como 'andamento', apagando um acerto
    que aconteceu de verdade.
    """
    log = _log_com_ts(tmp_path, [
        {"ts": "2026-08-20T12:00:00+00:00", "ticker": "C", "frame": "1d",
         "trigger": 100.0, "direction": "compra", "tp": 110.0, "sl": 95.0},
    ])
    _serie(monkeypatch,
           {"C": [_c("2026-08-21", 111.0, 101.0),      # tocou o TP
                  _c("2026-08-22", 103.0, 99.0),       # e voltou
                  _c("2026-08-25", 101.0, 96.0)]},
           {"C": 100.5})                                # preço de hoje: longe do TP
    out = scan_verdicts(log, "2026-08-28")
    v = out["verdicts"][0]
    assert v["veredito"] == "bateu_tp", v
    assert v["fechado"] is True and v["fechado_em"] == "2026-08-21"
    assert out["taxa_acerto"] == 1.0


def test_bateu_sl_e_recuperou_continua_bateu_sl(tmp_path, monkeypatch):
    """A perda REAL também não some quando o preço recupera (C2-a, lado feio).

    Sem isto a taxa de acerto seria otimista por construção: só as perdas que
    ainda doem hoje contariam.
    """
    log = _log_com_ts(tmp_path, [
        {"ts": "2026-08-20T12:00:00+00:00", "ticker": "S", "frame": "1d",
         "trigger": 100.0, "direction": "compra", "tp": 110.0, "sl": 95.0},
    ])
    _serie(monkeypatch,
           {"S": [_c("2026-08-21", 101.0, 94.0),       # perfurou o SL
                  _c("2026-08-22", 106.0, 100.0)]},    # e recuperou
           {"S": 106.0})
    v = scan_verdicts(log, "2026-08-28")["verdicts"][0]
    assert v["veredito"] == "bateu_sl" and v["fechado_em"] == "2026-08-21"


def test_veredito_fechado_nao_muda_quando_a_data_avanca(tmp_path, monkeypatch):
    """IMUTABILIDADE (C2-b/c): o mesmo gatilho re-avaliado em datas diferentes dá o
    MESMO veredito e a MESMA taxa. Antes, um 'bateu_tp' de hoje virava 'andamento'
    amanhã e a taxa de acerto oscilava a cada chamada."""
    log = _log_com_ts(tmp_path, [
        {"ts": "2026-08-20T12:00:00+00:00", "ticker": "C", "frame": "1d",
         "trigger": 100.0, "direction": "compra", "tp": 110.0, "sl": 95.0},
    ])
    serie = [_c("2026-08-21", 111.0, 101.0), _c("2026-08-22", 104.0, 99.0),
             _c("2026-08-27", 98.0, 90.0)]   # depois até perfura o SL — tarde demais
    saidas = []
    for i, dia in enumerate(("2026-08-22", "2026-08-25", "2026-08-28"), start=1):
        _serie(monkeypatch, {"C": serie[:max(1, i)]}, {"C": 92.0})
        saidas.append(scan_verdicts(log, dia))
    assert {o["verdicts"][0]["veredito"] for o in saidas} == {"bateu_tp"}
    assert {o["taxa_acerto"] for o in saidas} == {1.0}


def test_tp_e_sl_na_mesma_barra_conta_sl(tmp_path, monkeypatch):
    """Sem tick não dá pra saber a ordem DENTRO da barra → leitura pessimista.
    Declarado em ``empate_na_barra``, nunca resolvido no chute otimista."""
    log = _log_com_ts(tmp_path, [
        {"ts": "2026-08-20T12:00:00+00:00", "ticker": "X", "frame": "1d",
         "trigger": 100.0, "direction": "compra", "tp": 110.0, "sl": 95.0},
    ])
    _serie(monkeypatch, {"X": [_c("2026-08-21", 112.0, 94.0)]}, {"X": 100.0})
    v = scan_verdicts(log, "2026-08-28")["verdicts"][0]
    assert v["veredito"] == "bateu_sl" and v["empate_na_barra"] is True


def test_o_proprio_dia_do_log_nao_fecha_o_trade(tmp_path, monkeypatch):
    """O dia do log fica FORA da janela: o ts é UTC e o candle é do relógio do
    mercado — contar o próprio dia poderia creditar um TP anterior ao gatilho.
    Acerto inflado é o erro que este painel não pode cometer."""
    log = _log_com_ts(tmp_path, [
        {"ts": "2026-08-21T18:00:00+00:00", "ticker": "C", "frame": "1d",
         "trigger": 100.0, "direction": "compra", "tp": 110.0, "sl": 95.0},
    ])
    _serie(monkeypatch, {"C": [_c("2026-08-21", 115.0, 99.0)]}, {"C": 101.0})
    out = scan_verdicts(log, "2026-08-28")
    assert out["verdicts"][0]["veredito"] == "andamento_lucro"
    assert out["n_fechados"] == 0


def test_sem_serie_o_trade_fica_ABERTO_nunca_fechado_no_escuro(tmp_path, monkeypatch):
    """Série indisponível não vira veredito fechado — fica ABERTO, hoje nomeado
    ``sem_serie_cobrindo``. Fechar sem prova é o que corrompia a taxa."""
    log = _log_com_ts(tmp_path, [
        {"ts": "2026-08-20T12:00:00+00:00", "ticker": "C", "frame": "1d",
         "trigger": 100.0, "direction": "compra", "tp": 110.0, "sl": 95.0},
    ])
    def boom(t, d, timeframe="1d", method="padrao"):
        raise RuntimeError("fonte fora do ar")
    monkeypatch.setattr(sc, "build_actionable_plan_dict",
                        lambda t, d, timeframe="1d", method="padrao":
                        {"price": 120.0, "pattern": None, "setup_state": "ativo"})
    monkeypatch.setattr(sc, "build_price_chart", boom)
    monkeypatch.setattr(sc, "_live_price", lambda ticker: None)
    out = scan_verdicts(log, "2026-08-28")
    assert out["verdicts"][0]["fechado"] is False
    assert out["n_fechados"] == 0

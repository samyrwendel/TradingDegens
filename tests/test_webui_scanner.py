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
    Live price desligado (None) — o teste é offline; o price do plan é a fonte.
    Earnings também desligado (fonte indisponível, fail-open) — sem isso cada
    símbolo falso ("A"/"B"/"C") bateria de verdade no yfinance."""
    def install(plans):
        monkeypatch.setattr(
            sc, "build_actionable_plan_dict",
            lambda t, d, timeframe="1d", method="padrao": plans.get((t.upper(), timeframe), _plan(setup_state="sem_setup")),
        )
        monkeypatch.setattr(sc, "build_price_chart",
                            lambda t, d, bars=260, timeframe="1d", method="padrao": {"candles": []})
        monkeypatch.setattr(sc, "_live_price", lambda ticker: None)
        monkeypatch.setattr(
            sc, "earnings_window_status",
            lambda symbol, curr_date, window_days, asset_type="stock": {
                "status": "fonte_indisponivel", "date": None, "days_ahead": None,
                "in_window": None, "window_days": window_days,
            },
        )
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
    """O FALLBACK: ``pat`` sem ``ciclo``/``invalidado`` (plano ANTIGO, de antes da
    task 013) cai na conta local por preço. É o chão do detector ausente — não a
    régua que uma run atual usa (essa é a de baixo, por FECHAMENTO)."""
    # COMPRA: setup morre ao PERDER o ponto 3 (preço < invalidação).
    fake_fetch({("C", "1d"): _plan(_pat(trigger=110.0), price=88.0, invalidation=95.0)})
    assert scan_symbol("C", "2026-08-28", frames=("1d",))["melhor"]["estado"] == "invalidou"
    # VENDA: setup morre ao VOLTAR acima do ponto 3 (preço > invalidação).
    fake_fetch({("V", "1d"): _plan(_pat(direction="venda", trigger=90.0),
                                   price=105.0, invalidation=95.0)})
    assert scan_symbol("V", "2026-08-28", frames=("1d",))["melhor"]["estado"] == "invalidou"


# ══════════ INVALIDAÇÃO É DE FECHAMENTO, NUNCA DE PAVIO (DA-153, precisa a 031) ═
#
# O Samyr, sozinho: *"ou ele ainda não invalidou pq não fechou o dia?"* — sim, e é
# a régua que ``_primeira_barra_alem`` (price_structure.py) já usa: "a primeira
# barra que FECHA além do nível", pela mesma razão que o stop leva folga de ATR —
# não ser tirado por sombra. Uma run ATUAL sempre carrega ``ciclo``/``invalidado``
# no pattern (``Pattern123.to_dict``, sempre presentes) — e é esse par que
# ``_estado_do_ciclo``/``"invalidado" in pat`` consulta ANTES de cair no fallback
# de preço acima. O teste acima usa ``_pat()`` puro (sem esses campos) de
# propósito: ele mede o fallback, não uma run real — os dois de baixo medem a run
# real, com os campos que ela sempre manda.


def _pat_com_ciclo(*, direction="compra", state="formando", trigger=100.0,
                    ciclo="vivo", invalidado=False):
    """Um ``pat`` como uma run ATUAL o serializa — com ``ciclo``/``invalidado``,
    nunca ausentes (``Pattern123.to_dict``). ``_pat()`` acima os omite de
    propósito, pra testar o fallback isolado; aqui eles vêm sempre."""
    return {"direction": direction, "state": state, "trigger": trigger,
            "ciclo": ciclo, "invalidado": invalidado}


def test_pavio_alem_da_invalidacao_SEM_fechar_continua_vivo(fake_fetch):
    """O preço (pavio/intrabar) já passou do ponto 3, mas nenhuma barra FECHOU
    além dele — o detector não confirmou (``ciclo="vivo"``, ``invalidado=False``).
    O setup continua respirando: não pode sair como ``invalidou``."""
    fake_fetch({("C", "1d"): _plan(
        _pat_com_ciclo(trigger=110.0, ciclo="vivo", invalidado=False),
        price=88.0, invalidation=95.0)})   # 88 < 95: intrabar já perfurou
    estado = scan_symbol("C", "2026-08-28", frames=("1d",))["melhor"]["estado"]
    assert estado != "invalidou", ("pavio intrabar matou um setup que respira", estado)
    assert estado == "formando", estado   # longe do gatilho (20%), nunca acionou


def test_fechamento_ALEM_da_invalidacao_confirma_e_sai_da_faixa(fake_fetch):
    """A barra FECHOU além do ponto 3 (``ciclo="invalidado_sem_acionar"``,
    ``invalidado=True``) — mesmo que o PREÇO atual (pavio de volta) esteja do
    lado vivo, o fechamento já decidiu: o padrão morreu e "voltou continua
    morto" (mesma regra do card, DA-125/task 013)."""
    fake_fetch({("C", "1d"): _plan(
        _pat_com_ciclo(trigger=110.0, ciclo="invalidado_sem_acionar", invalidado=True),
        price=200.0, invalidation=95.0)})   # preço ATUAL do lado vivo — não importa
    estado = scan_symbol("C", "2026-08-28", frames=("1d",))["melhor"]["estado"]
    assert estado == "invalidou", ("fechamento confirmado tem de mandar", estado)


def test_estado_formando_sem_setup(fake_fetch):
    fake_fetch({("A", "1d"): _plan(_pat(trigger=110.0), price=100.0)})   # 10% — longe
    assert scan_symbol("A", "2026-08-28", frames=("1d",))["melhor"]["estado"] == "formando"
    fake_fetch({("A", "1d"): _plan(None, price=100.0, setup_state="sem_setup")})
    assert scan_symbol("A", "2026-08-28", frames=("1d",))["melhor"]["estado"] == "sem_setup"


# ---------------------------- calendário de resultados (task 044) --------------
def test_scan_symbol_carrega_earnings_uma_vez_por_ativo_nao_por_frame(monkeypatch):
    """A leitura é do ATIVO, não do frame — chamar 1x mesmo com N frames evita
    N chamadas ao cache (DA-058) pro mesmo dado."""
    monkeypatch.setattr(sc, "build_actionable_plan_dict",
                        lambda t, d, timeframe="1d", method="padrao": _plan(setup_state="sem_setup"))
    monkeypatch.setattr(sc, "build_price_chart",
                        lambda t, d, bars=260, timeframe="1d", method="padrao": {"candles": []})
    monkeypatch.setattr(sc, "_live_price", lambda ticker: None)
    chamadas = []

    def fake_earnings(symbol, curr_date, window_days, asset_type="stock"):
        chamadas.append((symbol, asset_type))
        return {"status": "ok", "date": "2026-09-10", "days_ahead": 5,
                "in_window": True, "window_days": window_days}

    monkeypatch.setattr(sc, "earnings_window_status", fake_earnings)
    r = scan_symbol("A", "2026-08-28", frames=("1w", "1d", "4h", "1h"))
    assert len(chamadas) == 1, chamadas
    assert chamadas[0] == ("A", "stock")
    assert r["earnings"]["in_window"] is True


def test_scan_symbol_cripto_marca_asset_type(monkeypatch):
    monkeypatch.setattr(sc, "build_actionable_plan_dict",
                        lambda t, d, timeframe="1d", method="padrao": _plan(setup_state="sem_setup"))
    monkeypatch.setattr(sc, "build_price_chart",
                        lambda t, d, bars=260, timeframe="1d", method="padrao": {"candles": []})
    monkeypatch.setattr(sc, "_live_price", lambda ticker: None)
    vistos = []

    def fake_earnings(symbol, curr_date, window_days, asset_type="stock"):
        vistos.append(asset_type)
        return {}

    monkeypatch.setattr(sc, "earnings_window_status", fake_earnings)
    scan_symbol("BTC-USD", "2026-08-28", frames=("1d",))
    assert vistos == ["crypto"]


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
    monkeypatch.setattr(sc, "earnings_window_status",
                        lambda symbol, curr_date, window_days, asset_type="stock": {})
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
    monkeypatch.setattr(sc, "earnings_window_status",
                        lambda symbol, curr_date, window_days, asset_type="stock": {})
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


# ══════════ PAPER TRADING NO TRACK RECORD (DA-154) ═══════════════════════════
#
# A proposta do Samyr: "como se fosse paper" — POSIÇÃO FIXA em dólares por
# operação, não RISCO fixo. Quantidade = banca/entrada; resultado = variação
# percentual do preço × banca — a perda por trade VARIA com a distância do
# stop, o oposto de "arriscar sempre a mesma banca".


def _v(veredito="bateu_tp", direction="compra", trigger=100.0, tp=110.0, sl=95.0,
      rr=2.0, entrada=None, ts="2026-08-20T12:00:00+00:00", fechado_em="2026-08-25",
      ticker="C", frame="1d", setup="123", preco_agora=None):
    v = {"veredito": veredito, "direction": direction, "trigger": trigger, "tp": tp,
        "sl": sl, "rr": rr, "ts": ts, "fechado_em": fechado_em, "ticker": ticker,
        "frame": frame, "setup": setup}
    if entrada is not None:
        v["entrada"] = entrada
    if preco_agora is not None:
        v["preco_agora"] = preco_agora
    return v


def test_pnl_paper_trade_compra_bateu_tp_e_bateu_sl():
    # COMPRA bateu TP: quantidade = 100/100 = 1, ganha (110-100)/100 = 10% = $10.
    ganho = sc._pnl_paper_trade(_v(veredito="bateu_tp"), banca=100.0)
    assert ganho["pnl_usd"] == 10.0 and ganho["pnl_pct"] == 10.0
    # COMPRA bateu SL: perde (95-100)/100 = -5% = -$5 — NÃO é -$100 (não é risco fixo).
    perda = sc._pnl_paper_trade(_v(veredito="bateu_sl"), banca=100.0)
    assert perda["pnl_usd"] == -5.0 and perda["pnl_pct"] == -5.0


def test_pnl_paper_trade_venda_inverte_o_sinal():
    # VENDA bateu TP (preço CAIU até o alvo abaixo): lucro, sinal invertido.
    v = _v(veredito="bateu_tp", direction="venda", trigger=100.0, tp=90.0, sl=105.0)
    ganho = sc._pnl_paper_trade(v, banca=100.0)
    assert ganho["pnl_usd"] == 10.0, ganho     # (90-100)/100 = -10%, invertido = +10%
    v = _v(veredito="bateu_sl", direction="venda", trigger=100.0, tp=90.0, sl=105.0)
    perda = sc._pnl_paper_trade(v, banca=100.0)
    assert perda["pnl_usd"] == -5.0, perda     # (105-100)/100 = +5%, invertido = -5%


def test_pnl_paper_trade_usa_ENTRADA_quando_o_log_a_carimba():
    """Storm carimba ``entrada`` (o ponto de entrada real, não sempre o gatilho) —
    o PnL tem de usar essa referência, não o trigger, quando ela existe."""
    v = _v(veredito="bateu_tp", trigger=100.0, entrada=105.0, tp=115.0)
    p = sc._pnl_paper_trade(v, banca=100.0)
    assert p["pnl_usd"] == round(100.0 * (115.0 - 105.0) / 105.0, 2), p


def test_pnl_paper_trade_None_para_aberto_e_sem_base():
    assert sc._pnl_paper_trade(_v(veredito="andamento_lucro"), banca=100.0) is None
    assert sc._pnl_paper_trade(_v(veredito="sem_dado"), banca=100.0) is None
    v = _v(veredito="bateu_tp", trigger=None, entrada=None)
    assert sc._pnl_paper_trade(v, banca=100.0) is None


def test_pnl_paper_trade_storm_LEDGER_ANTIGO_com_rotulo_reconstroi_pelo_trigger():
    """BUG (task 20260902-035): ``_storm_row`` carimba ``entrada`` com o RÓTULO da
    leitura escolhida (``ponto2``/``ponto3``/``ponto2e3`` — útil pra célula do scan),
    e o ledger gravava esse rótulo cru. ``_pnl_paper_trade`` fazia ``float("ponto3")``,
    estourava ``ValueError`` e devolvia ``None`` — o Storm fechado saía SEM PnL em USD,
    silenciosamente, sempre. O preço da MESMA leitura está em ``trigger`` (a leitura
    escolhida é uma só): um ``entrada`` que não converte pra número é tratado como
    ausente, e o PnL sai do trigger — sem reescrever o ledger append-only."""
    v = _v(veredito="bateu_tp", trigger=105.0, entrada="ponto3", tp=115.0, setup="storm")
    p = sc._pnl_paper_trade(v, banca=100.0)
    assert p is not None, "Storm com ledger antigo (rótulo em vez de preço) não pode virar PnL em branco"
    assert p["pnl_usd"] == round(100.0 * (115.0 - 105.0) / 105.0, 2), p


def test_pnl_paper_aberto_storm_LEDGER_ANTIGO_com_rotulo_reconstroi_pelo_trigger():
    """Mesmo bug, na posição ABERTA (marcação a mercado)."""
    v = _v(veredito="andamento_lucro", trigger=105.0, entrada="ponto2", setup="storm",
           preco_agora=110.0)
    p = sc._pnl_paper_aberto(v, banca=100.0)
    assert p is not None, "posição aberta do Storm com ledger antigo não pode ficar em branco"
    assert p["pnl_usd"] == round(100.0 * (110.0 - 105.0) / 105.0, 2), p


def test_pnl_risco_fixo_e_a_leitura_ALTERNATIVA():
    """Se arriscasse a banca inteira por trade: ganha rr×banca no alvo, perde a
    banca inteira no stop — DIFERENTE do PnL de posição fixa (a pergunta é outra)."""
    ganho = sc._pnl_paper_trade(_v(veredito="bateu_tp", rr=2.0), banca=100.0)
    assert ganho["pnl_risco_fixo_usd"] == 200.0
    assert ganho["pnl_risco_fixo_usd"] != ganho["pnl_usd"]
    perda = sc._pnl_paper_trade(_v(veredito="bateu_sl", rr=2.0), banca=100.0)
    assert perda["pnl_risco_fixo_usd"] == -100.0


def test_pnl_paper_resumo_curva_de_equity_e_CRONOLOGICA():
    """A curva soma na ORDEM DO FECHAMENTO, não na ordem em que os trades chegam
    — um fechado antes tem de aparecer antes na curva, mesmo se veio depois na
    lista de entrada."""
    trades = [
        _v(veredito="bateu_tp", fechado_em="2026-08-27", ticker="B"),   # +$10, 2º
        _v(veredito="bateu_sl", fechado_em="2026-08-25", ticker="A"),   # -$5, 1º
    ]
    r = sc._pnl_paper_resumo(trades, banca=100.0)
    assert [c["ticker"] for c in r["curva_equity"]] == ["A", "B"]
    assert r["curva_equity"][0]["equity_usd"] == -5.0
    assert r["curva_equity"][1]["equity_usd"] == 5.0     # -5 + 10
    assert r["pnl_total_usd"] == 5.0
    assert r["pnl_medio_usd"] == 2.5
    assert r["melhor_trade"]["ticker"] == "B" and r["pior_trade"]["ticker"] == "A"
    # % sobre o capital empregado: banca=100 × n=2 trades = 200 empregados, +5 total
    assert r["pnl_total_pct"] == 2.5


def test_pnl_paper_resumo_gate_de_N_igual_ao_da_confiabilidade():
    from tradingagents.webui.execucao import _N_MINIMO, _N_OPERAVEL
    assert sc._pnl_paper_resumo([], banca=100.0)["nivel"] == "insuficiente"
    quatro = [_v(ticker=str(i)) for i in range(_N_MINIMO - 1)]
    assert sc._pnl_paper_resumo(quatro, banca=100.0)["nivel"] == "insuficiente"
    dez = [_v(ticker=str(i)) for i in range(_N_MINIMO)]
    assert sc._pnl_paper_resumo(dez, banca=100.0)["nivel"] == "preliminar"
    vinte = [_v(ticker=str(i)) for i in range(_N_OPERAVEL)]
    assert sc._pnl_paper_resumo(vinte, banca=100.0)["nivel"] == "operavel"


def test_pnl_paper_resumo_sem_trade_nenhum_declara_vazio_nao_inventa():
    r = sc._pnl_paper_resumo([], banca=100.0)
    assert r == {"n": 0, "nivel": "insuficiente", "banca_por_trade": 100.0,
                "pnl_total_usd": None, "pnl_total_pct": None, "pnl_medio_usd": None,
                "melhor_trade": None, "pior_trade": None, "curva_equity": []}


def test_scan_verdicts_expoe_paper_por_setup_e_por_frame(tmp_path, monkeypatch):
    """Fim a fim: um gatilho do 1-2-3 fecha em TP, e ``out["paper"]`` aparece com
    a MESMA decomposição do acerto (agregado/por_setup/por_frame), com a banca
    que foi pedida — não o padrão, quando um valor é passado."""
    log = _log_com_ts(tmp_path, [
        {"ts": "2026-08-20T12:00:00+00:00", "ticker": "C", "frame": "1d",
         "trigger": 100.0, "direction": "compra", "tp": 110.0, "sl": 95.0,
         "rr": 2.0, "setup": "123"},
    ])
    _serie(monkeypatch, {"C": [_c("2026-08-21", 111.0, 100.0)]}, {"C": 105.0})
    out = scan_verdicts(log, "2026-08-28", banca=50.0)
    paper = out["paper"]
    assert paper["banca_por_trade"] == 50.0
    assert "posição FIXA" in paper["premissa"] and "sem custos" in paper["premissa"]
    assert paper["agregado"]["n"] == 1
    assert paper["agregado"]["pnl_total_usd"] == 5.0   # 50 × (110-100)/100
    assert paper["por_setup"]["123"]["pnl_total_usd"] == 5.0
    assert paper["por_setup"]["storm"]["n"] == 0
    assert paper["por_frame"]["1d"]["pnl_total_usd"] == 5.0
    assert "4h" not in paper["por_frame"]      # sem fechado nesse frame: some, não n=0


def test_scan_verdicts_banca_padrao_quando_nao_pedida(tmp_path, monkeypatch):
    log = _log_com_ts(tmp_path, [])
    _serie(monkeypatch, {}, {})
    out = scan_verdicts(log, "2026-08-28")
    assert out["paper"]["banca_por_trade"] == sc._BANCA_PADRAO == 100.0


# ══════════ A CARTEIRA VIRTUAL (DA-155) ═══════════════════════════════════════
#
# Paper trading não é só recalcular o passado — é acompanhar a posição
# SIMULADA enquanto ela vive: posições ABERTAS com PnL não realizado (marcado
# a mercado pelo preço de agora), somando num SALDO ao lado do que já fechou.
# O "reset" da simulação é um MARCO de tempo, nunca um apagão do ledger.


def test_pnl_paper_aberto_marca_a_mercado_com_o_preco_de_agora():
    aberto = sc._pnl_paper_aberto(
        _v(veredito="andamento_lucro", trigger=100.0, preco_agora=108.0), banca=100.0)
    assert aberto["pnl_usd"] == 8.0 and aberto["pnl_pct"] == 8.0   # (108-100)/100


def test_pnl_paper_aberto_None_para_fechado_ou_sem_preco_agora():
    assert sc._pnl_paper_aberto(_v(veredito="bateu_tp"), banca=100.0) is None
    fechado = _v(veredito="andamento_lucro")
    fechado.pop("preco_agora", None)   # nunca teve preco_agora carimbado
    assert sc._pnl_paper_aberto(fechado, banca=100.0) is None


def test_apos_marco_sem_marco_tudo_conta():
    assert sc._apos_marco(_v(ts="2020-01-01T00:00:00+00:00"), None) is True


def test_apos_marco_filtra_por_ts_lexicografico():
    marco = "2026-08-22T00:00:00+00:00"
    antes = _v(ts="2026-08-20T12:00:00+00:00")
    depois = _v(ts="2026-08-25T12:00:00+00:00")
    assert sc._apos_marco(antes, marco) is False
    assert sc._apos_marco(depois, marco) is True


def test_carteira_paper_soma_realizado_e_nao_realizado():
    verdicts = [
        _v(veredito="bateu_tp", ticker="C1", fechado_em="2026-08-22"),           # +$10
        _v(veredito="bateu_sl", ticker="C2", fechado_em="2026-08-23"),           # -$5
        _v(veredito="andamento_lucro", ticker="C3", trigger=100.0, preco_agora=104.0),  # +$4 não realizado
        _v(veredito="sem_dado", ticker="C4"),   # nem fechado nem aberto: fora
    ]
    c = sc._carteira_paper(verdicts, banca=100.0, marco=None)
    assert c["n_fechadas"] == 2 and c["n_abertas"] == 1
    assert c["realizado_usd"] == 5.0            # 10 - 5
    assert c["nao_realizado_usd"] == 4.0
    assert c["saldo_usd"] == 9.0                # 5 + 4
    assert c["abertas"][0]["ticker"] == "C3" and c["abertas"][0]["pnl_usd"] == 4.0


def test_carteira_paper_marco_exclui_gatilhos_ANTERIORES(tmp_path):
    marco = "2026-08-22T00:00:00+00:00"
    verdicts = [
        _v(veredito="bateu_tp", ticker="VELHO", ts="2026-08-10T00:00:00+00:00"),   # antes do marco
        _v(veredito="bateu_tp", ticker="NOVO", ts="2026-08-25T00:00:00+00:00"),    # depois
    ]
    c = sc._carteira_paper(verdicts, banca=100.0, marco=marco)
    assert c["n_fechadas"] == 1
    assert c["curva_equity"][0]["ticker"] == "NOVO"


def test_carteira_paper_sem_nada_declara_saldo_zero_nao_None():
    """Carteira NOVA (marco recém-posto, nada aconteceu ainda desde ele): saldo
    é $0 declarado — não confundir com "não sei", que seria None."""
    c = sc._carteira_paper([], banca=100.0, marco="2026-09-01T00:00:00+00:00")
    assert c["saldo_usd"] == 0.0
    assert c["n_fechadas"] == 0 and c["n_abertas"] == 0
    assert c["abertas"] == []


def test_scan_verdicts_expoe_carteira_com_marco(tmp_path, monkeypatch):
    """Fim a fim: scan_verdicts recebe o marco e ele filtra a carteira — o
    resumo "agregado" (LEDGER INTEIRO) não é afetado, só out["paper"]["carteira"]."""
    log = _log_com_ts(tmp_path, [
        {"ts": "2026-08-10T12:00:00+00:00", "ticker": "VELHO", "frame": "1d",
         "trigger": 100.0, "direction": "compra", "tp": 110.0, "sl": 95.0, "rr": 2.0},
        {"ts": "2026-08-25T12:00:00+00:00", "ticker": "NOVO", "frame": "1d",
         "trigger": 100.0, "direction": "compra", "tp": 110.0, "sl": 95.0, "rr": 2.0},
    ])
    _serie(monkeypatch,
          {"VELHO": [_c("2026-08-11", 111.0, 100.0)],
           "NOVO": [_c("2026-08-26", 111.0, 100.0)]},
          {"VELHO": 105.0, "NOVO": 105.0})
    out = scan_verdicts(log, "2026-08-28", banca=100.0, marco="2026-08-22T00:00:00+00:00")
    assert out["paper"]["agregado"]["n"] == 2         # o relatório do ledger inteiro
    assert out["paper"]["carteira"]["n_fechadas"] == 1   # a carteira, só desde o marco
    assert out["paper"]["carteira"]["marco"] == "2026-08-22T00:00:00+00:00"


def test_paper_wallet_store_marco_none_ate_resetar(tmp_path):
    from tradingagents.webui.store import PaperWalletStore
    w = PaperWalletStore(tmp_path)
    assert w.marco() is None
    novo = w.resetar("2026-09-01T12:00:00+00:00")
    assert novo == "2026-09-01T12:00:00+00:00"
    assert w.marco() == "2026-09-01T12:00:00+00:00"


def test_paper_wallet_store_persiste_em_disco(tmp_path):
    from tradingagents.webui.store import PaperWalletStore
    PaperWalletStore(tmp_path).resetar("2026-09-01T12:00:00+00:00")
    reaberta = PaperWalletStore(tmp_path)   # nova instância, mesmo diretório
    assert reaberta.marco() == "2026-09-01T12:00:00+00:00"

"""The on-demand Erick-method analyst: wiring + the deterministic method read.

No LLM and no network here — the graph wiring is asserted structurally and the
method read is driven with a synthetic (monkeypatched) chart/plan so the verdict
mapping is pinned deterministically.
"""

import pytest

import tradingagents.agents.utils.erick_method as em
from tradingagents.agents.utils.erick_method import (
    _days_ahead,
    _decide,
    _drop_decelerating,
    _estado,
    _fine_sell_triggered,
    _gate_abre,
    _liq_entry_ref,
    _rsi_divergence,
    _rsi_series,
    _swing_points,
    _tese_read,
    build_erick_method_section,
    ensure_erick_method_coverage,
)
from tradingagents.graph.analyst_execution import build_analyst_execution_plan
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.webui.progress import build_plan
from tradingagents.webui.runner import extract_result, select_analysts_for_asset


@pytest.fixture(autouse=True)
def _no_earnings_network(monkeypatch):
    """A camada de ponderação consulta o calendário de balanço em TODA decisão —
    sem isto os testes de seção batem no yfinance de verdade. Hermético e
    determinístico: sem agenda publicada (na_janela=False, informação, não erro)."""
    monkeypatch.setattr(
        em, "_earnings_read",
        lambda s, d: {"status": "sem_agenda", "ev": None, "dias": None,
                      "na_janela": False, "ausente": None,
                      "leitura": "sem data de balanço publicada — sem risco de evento conhecido"},
    )


# --------------------------------------------------------------- wiring --------
def test_select_analysts_appends_erick_on_demand():
    # Padrão is untouched…
    assert select_analysts_for_asset("stock") == ["market", "social", "news", "fundamentals"]
    assert select_analysts_for_asset("crypto") == ["market", "social", "news"]
    # …and the method only adds the erick analyst when asked, at the end.
    assert select_analysts_for_asset("stock", include_erick=True)[-1] == "erick"
    assert "erick" in select_analysts_for_asset("crypto", include_erick=True)
    # Padrão default really is off.
    assert "erick" not in select_analysts_for_asset("crypto")


def test_execution_plan_has_erick_spec():
    plan = build_analyst_execution_plan(["market", "erick"])
    spec = plan.specs[-1]
    assert spec.key == "erick"
    assert spec.agent_node == "Erick Analyst"
    assert spec.tool_node == "tools_erick"
    assert spec.report_key == "erick_report"


def test_conditional_routes_erick():
    logic = ConditionalLogic()

    class _Msg:
        tool_calls = []

    class _MsgWithCalls:
        tool_calls = [{"name": "get_indicators"}]

    assert logic.should_continue_erick({"messages": [_Msg()]}) == "Msg Clear Erick"
    assert logic.should_continue_erick({"messages": [_MsgWithCalls()]}) == "tools_erick"


def test_progress_plan_includes_erick_stage():
    nodes = [p["node"] for p in build_plan(["market", "erick"])]
    assert "Erick Analyst" in nodes
    # Erick belongs to the "Analistas" phase, before the debate.
    erick = next(p for p in build_plan(["market", "erick"]) if p["node"] == "Erick Analyst")
    assert erick["phase"] == "Analistas"
    assert erick["order"] < 50  # ahead of Bull Researcher


def test_extract_result_carries_erick_report():
    state = {"erick_report": "## 🧭 Método Erick — leitura", "investment_debate_state": {}}
    out = extract_result(state, "Hold")
    assert out["erick_report"].startswith("## 🧭 Método Erick")
    # Absent (Padrão run) → empty string, never missing.
    assert extract_result({"investment_debate_state": {}}, "Hold")["erick_report"] == ""


# ------------------------------------------------- verdict/peso mapping ---------
def _read(trend, at_media=False, extended=False, below=False):
    return {
        "close": 100.0, "e8": 99.0, "e21": 98.0, "e50": 95.0,
        "dist8": 0.01, "dist21": 0.02,
        "trend": trend, "at_media": at_media, "extended": extended, "below": below,
    }


def test_decide_uptrend_at_average_acts_half():
    d = _decide(_read("alta", at_media=True))
    assert d["acao"] == "AGIR"
    assert d["peso"] == "meia posição"


def test_decide_uptrend_extended_waits_cash():
    d = _decide(_read("alta", extended=True))
    assert d["acao"] == "AGUARDAR"
    assert d["peso"] == "caixa"


def test_decide_downtrend_is_cash_filter():
    d = _decide(_read("baixa", below=True))
    assert d["acao"] == "AGUARDAR"
    assert d["peso"] == "caixa"


def test_decide_transition_at_average_initial_only():
    d = _decide(_read("transicao", at_media=True))
    assert d["acao"] == "AGIR"
    assert d["peso"] == "posição inicial"


# ------------------------------------- drop_nature manda no _decide/_estado -----
# Uma liquidação COM a evidência do classificador: o eixo de entrada é a média DIÁRIA
# que sobe (MMS200) — NUNCA a EMA 4h invertida (que é o sintoma da liquidação).
_LIQ_DROP_EVID = {
    "classification": "liquidacao_saudavel",
    "reasons": ["queda de -17,0% recuou a uma média que sobe (MMS200)"],
    "evidence": {"asset": {"active_rising_label": "MMS200", "ma200": 250.0, "ma50": 270.0}},
}


def test_default_none_is_byte_for_byte_with_mechanical():
    """Fail-open: drop_cls=None (default) reproduz a mecânica de hoje BYTE-A-BYTE —
    não regride os testes/chamadores que passam None."""
    for trend, kw in [("alta", {"at_media": True}), ("alta", {"extended": True}),
                      ("alta", {}), ("baixa", {"below": True}),
                      ("transicao", {"at_media": True}), ("transicao", {})]:
        r = _read(trend, **kw)
        assert _decide(r) == _decide(r, None)
        d = _decide(r)
        assert _estado(d["acao"], trend) == _estado(d["acao"], trend, None)


def test_fraqueza_vetoes_even_a_mechanical_agir():
    """fraqueza → CAIXA, mesmo com um AGIR mecânico (estrutura rompida veta)."""
    d = _decide(_read("alta", at_media=True), {"classification": "fraqueza"})
    assert d["acao"] == "AGUARDAR" and d["peso"] == "caixa"
    # o Estado é a fonte única: fraqueza carimba CAIXA independentemente da ação/trend.
    assert _estado("AGIR", "alta", "fraqueza") == "CAIXA"


def test_liquidacao_downtrend_at_media_acts_initial():
    """liquidação + baixa + toque na média → AGIR / posição inicial (recuo comprável),
    com a ENTRADA ancorada na média DIÁRIA que sobe (MMS200), NÃO na EMA 4h invertida."""
    d = _decide(_read("baixa", at_media=True), _LIQ_DROP_EVID)
    assert d["acao"] == "AGIR" and d["peso"] == "posição inicial"
    assert _estado(d["acao"], "baixa", "liquidacao_saudavel") == "AGIR"
    # v2 (correção de sinal): a entrada cita a média DIÁRIA que sobe e trata a EMA 4h
    # como SINTOMA — o texto v1 ("recuo comprável na média (EMA 8 · EMA 21)") sumiu.
    assert "MMS200 diária" in d["entrada"]
    assert "sintoma da liquidação" in d["entrada"]
    assert "recuo comprável na média (EMA 8" not in d["entrada"]


def test_liquidacao_downtrend_no_touch_waits_not_cash_state():
    """liquidação + baixa SEM toque → AGUARDAR; o Estado é AGUARDAR (não CAIXA): a
    inversão das EMAs curtas é o sintoma da liquidação, não um downtrend."""
    d = _decide(_read("baixa"), _LIQ_DROP_EVID)
    assert d["acao"] == "AGUARDAR" and d["peso"] == "caixa"
    assert _estado("AGUARDAR", "baixa", "liquidacao_saudavel") == "AGUARDAR"
    # sem a natureza da queda, a mesma baixa leria CAIXA (mecânica de hoje).
    assert _estado("AGUARDAR", "baixa", None) == "CAIXA"
    # aguarda o toque na média DIÁRIA que sobe (não na EMA 4h invertida).
    assert "MMS200 diária" in d["entrada"]


def test_fine_veto_caps_liquidacao_at_aguardar():
    """Mitigação 1 (v2): fine_veto (1-2-3 de venda no 15m acionado) trava a liquidação
    em AGUARDAR/caixa mesmo com o preço na média (o que seria AGIR)."""
    d = _decide(_read("baixa", at_media=True), _LIQ_DROP_EVID, fine_veto=True)
    assert d["acao"] == "AGUARDAR" and d["peso"] == "caixa"
    assert "1-2-3 de venda ACIONADO" in d["entrada"]
    # sem veto, o MESMO cenário promove a AGIR (posição inicial).
    d2 = _decide(_read("baixa", at_media=True), _LIQ_DROP_EVID, fine_veto=False)
    assert d2["acao"] == "AGIR" and d2["peso"] == "posição inicial"


def test_fine_sell_triggered_detects_venda_acionado():
    """O seam do veto lê o plano 15m JÁ computado: só um 1-2-3 de VENDA ACIONADO dispara
    (compra / formando / plano ausente não vetam). Fail-open → False."""
    assert _fine_sell_triggered({"pattern": {"direction": "venda", "state": "acionado"}}) is True
    assert _fine_sell_triggered({"pattern": {"direction": "venda", "state": "formando"}}) is False
    assert _fine_sell_triggered({"pattern": {"direction": "compra", "state": "acionado"}}) is False
    assert _fine_sell_triggered(None) is False


def test_liq_entry_ref_uses_daily_rising_average():
    """A referência de entrada é a média DIÁRIA que sobe (active_rising_label da
    evidência) — nunca a EMA 4h. Fail-open → texto genérico da média diária."""
    ref = _liq_entry_ref(_LIQ_DROP_EVID)
    assert ref.startswith("MMS200 diária") and "250" in ref
    d50 = {"evidence": {"asset": {"active_rising_label": "MMS50", "ma50": 270.0}}}
    assert _liq_entry_ref(d50).startswith("MMS50 diária")
    # sem nível casado, ainda cita a diária pelo label; sem label/evidência → genérico.
    assert _liq_entry_ref({"evidence": {"asset": {"active_rising_label": "MMS200"}}}) == "MMS200 diária"
    assert _liq_entry_ref(None) == "média diária que sobe"
    assert _liq_entry_ref({}) == "média diária que sobe"


# ---------------------------------------------- section (synthetic data) -------
def _fake_uptrend_at_media_chart():
    # last close hugging EMA8/21 (recuo concluído) in a stacked-up regime
    return {
        "candles": [{"c": 100.0}],
        "ema": {"8": [99.8], "21": [99.6], "50": [96.0]},
    }


def _fake_plan_with_realize():
    return {
        "setup_state": "ativo",
        "realize_zone": {"label": "topo anterior 2026-05-13", "low": 108.0, "high": 112.0, "price": 110.0},
    }


def test_section_crypto_cites_intraday_entry_exit_and_weight(monkeypatch):
    monkeypatch.setattr(em, "build_price_chart", lambda s, d, timeframe="1d": _fake_uptrend_at_media_chart())
    monkeypatch.setattr(em, "build_actionable_plan_dict", lambda s, d, tf: _fake_plan_with_realize())
    monkeypatch.setattr(em, "_drop_nature", lambda *a, **k: None)  # concern testado à parte
    section = build_erick_method_section("BTC-USD", "2026-08-24", "crypto")
    # The four things the acceptance requires, all present:
    assert "intradiário" in section  # timeframe declared
    assert "4 horas" in section
    assert "**Entrada (recuo à média):**" in section
    assert "**Saída (antes da reversão):**" in section
    assert "**Peso relativo do trade:**" in section
    # method-coherent framing that a market-analyst clone would not carry
    assert "Tático × estrutural" in section


def test_section_emits_single_state_enum(monkeypatch):
    """Item 6b: the method emits ONE state (AGIR/AGUARDAR/CAIXA), computed once, and
    the sub-blocks derive from it — no parallel 'Veredito'."""
    monkeypatch.setattr(em, "build_price_chart", lambda s, d, timeframe="1d": _fake_uptrend_at_media_chart())
    monkeypatch.setattr(em, "build_actionable_plan_dict", lambda s, d, tf: _fake_plan_with_realize())
    monkeypatch.setattr(em, "_drop_nature", lambda *a, **k: None)
    section = build_erick_method_section("BTC-USD", "2026-08-24", "crypto")
    assert "**Estado (Método Erick):** AGIR" in section
    # exactly one state label, no competing 'Veredito' line in the deterministic part
    assert section.count("Estado (Método Erick):") == 1
    assert "Veredito" not in section


def test_section_stock_reads_intraday_like_crypto(monkeypatch):
    """An equity now has keyless intraday (yfinance), so the Erick section reads the
    4h swing frame for a stock too — no longer a daily-only 'no intraday for stocks'
    fallback (fork brief 25/08 item 6)."""
    monkeypatch.setattr(em, "build_price_chart", lambda s, d, timeframe="1d": _fake_uptrend_at_media_chart())
    monkeypatch.setattr(em, "build_actionable_plan_dict", lambda s, d, tf: _fake_plan_with_realize())
    monkeypatch.setattr(em, "_drop_nature", lambda *a, **k: None)
    section = build_erick_method_section("BE", "2026-08-24", "stock")
    assert "4 horas" in section          # swing frame, same as crypto
    assert "não existe para ação" not in section  # the stale claim is gone
    assert "**Peso relativo do trade:**" in section


def test_section_stock_degrades_to_daily_when_intraday_absent(monkeypatch):
    """When the equity intraday source has no candle (empty 4h/15m chart) the read
    falls back to the daily and DECLARES the degrade — never fabricates a bar."""
    def chart(s, d, timeframe="1d"):
        # 4h/15m empty (out of window); daily has a real read.
        if timeframe == "1d":
            return _fake_uptrend_at_media_chart()
        return {"candles": [], "ema": {}}

    monkeypatch.setattr(em, "build_price_chart", chart)
    monkeypatch.setattr(em, "build_actionable_plan_dict", lambda s, d, tf: _fake_plan_with_realize())
    monkeypatch.setattr(em, "_drop_nature", lambda *a, **k: None)
    section = build_erick_method_section("BE", "2019-01-15", "stock")
    assert "diário" in section
    assert "indisponível" in section.lower()
    assert "**Peso relativo do trade:**" in section


def test_section_no_candle_is_honest_not_fabricated(monkeypatch):
    # crypto feed down for BOTH intraday and the daily fallback -> no read
    monkeypatch.setattr(em, "build_price_chart", lambda s, d, timeframe="1d": {"candles": [], "ema": {}})
    section = build_erick_method_section("BTC-USD", "2026-08-24", "crypto")
    assert "nada inventado" in section.lower()
    assert "**Peso relativo" not in section  # no fake verdict when there is no data


def _fake_plan_with_pattern():
    return {
        "setup_state": "aguardar_rompimento",
        "realize_zone": {"label": "topo anterior", "low": 108.0, "high": 112.0, "price": 110.0},
        "pattern": {
            "p1": {"date": "2026-08-01", "price": 90.0},
            "p2": {"date": "2026-08-05", "price": 98.0},
            "p3": {"date": "2026-08-09", "price": 93.0},
            "trigger": 98.0, "state": "formando", "direction": "compra",
        },
    }


def test_section_surfaces_123_trigger_in_method_read(monkeypatch):
    """GAP1: o gatilho 1-2-3 do 15m/4h aparece DENTRO da leitura do método (self-
    contained), não só na seção do analista de mercado."""
    monkeypatch.setattr(em, "build_price_chart", lambda s, d, timeframe="1d": _fake_uptrend_at_media_chart())
    monkeypatch.setattr(em, "build_actionable_plan_dict", lambda s, d, tf: _fake_plan_with_pattern())
    monkeypatch.setattr(em, "_drop_nature", lambda *a, **k: None)
    section = build_erick_method_section("BTC-USD", "2026-08-24", "crypto")
    assert "Gatilho 1-2-3 de compra (4h)" in section
    assert "rompimento de 98.00" in section
    assert "em formação" in section


def test_pattern_line_venda_uses_perda_wording():
    plan = {"pattern": {"trigger": 100.0, "state": "acionado", "direction": "venda"}}
    line = em._pattern_line(plan, "15m")
    assert "de venda (15m)" in line
    assert "perda de 100.00" in line
    assert "acionado" in line
    # sem padrão → sem linha
    assert em._pattern_line({"pattern": None}, "4h") is None


def test_coverage_is_fail_open(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("feed exploded")

    monkeypatch.setattr(em, "build_price_chart", _boom)
    # enrichment must never break the report
    assert ensure_erick_method_coverage("PROSE", "BTC-USD", "2026-08-24", "crypto") == "PROSE"


# ---------------------------- coerência: drop_nature manda no Estado da seção ----
def _fake_downtrend_at_media_chart():
    # EMAs invertidas (baixa) com o preço colado na média (recuo concluído) — o
    # cenário de uma liquidação: pilha curta invertida, mas recuo comprável.
    return {"candles": [{"c": 98.2}], "ema": {"8": [98.0], "21": [98.2], "50": [99.0]}}


_LIQ_DROP = {"classification": "liquidacao_saudavel",
             "reasons": ["queda de -17,0% recuou a uma média que sobe (MMS200)"]}


def test_section_drop_nature_before_estado_and_derives_it(monkeypatch):
    """Coerência: numa liquidação, a «🩸 Natureza da queda» vem ANTES do Estado, e o
    Estado (AGIR) DERIVA dela — não uma re-leitura anexada no fim que o contradiz."""
    monkeypatch.setattr(em, "build_price_chart", lambda s, d, timeframe="1d": _fake_downtrend_at_media_chart())
    monkeypatch.setattr(em, "build_actionable_plan_dict", lambda s, d, tf: _fake_plan_with_realize())
    monkeypatch.setattr(em, "_drop_nature", lambda *a, **k: _LIQ_DROP)
    section = build_erick_method_section("AVGO", "2026-08-26", "stock")
    assert "**Estado (Método Erick):** AGIR" in section
    # a natureza da queda aparece ACIMA do Estado (ordem = coerência de leitura)
    assert section.index("🩸 Natureza da queda") < section.index("Estado (Método Erick)")
    assert "Deriva da natureza da queda" in section
    assert "posição inicial" in section
    # a string de re-leitura contraditória não existe mais
    assert "Re-leitura" not in section
    # e nenhuma frase "evitar/fraqueza" contradizendo a liquidação na parte determinística
    assert "fraqueza: evitar" not in section


def test_section_liquidacao_entry_cites_daily_average(monkeypatch):
    """v2 (correção de sinal na seção): numa liquidação que AGE, a linha de ENTRADA
    cita a média DIÁRIA que sobe (MMS200); a EMA 4h invertida aparece só como SINTOMA,
    nunca como eixo de entrada."""
    monkeypatch.setattr(em, "build_price_chart", lambda s, d, timeframe="1d": _fake_downtrend_at_media_chart())
    monkeypatch.setattr(em, "build_actionable_plan_dict", lambda s, d, tf: _fake_plan_with_realize())
    monkeypatch.setattr(em, "_drop_nature", lambda *a, **k: _LIQ_DROP_EVID)
    section = build_erick_method_section("AVGO", "2026-08-26", "stock")
    assert "**Estado (Método Erick):** AGIR" in section
    assert "MMS200 diária" in section
    assert "sintoma da liquidação" in section
    # a formulação v1 (entrada ancorada na EMA 8·EMA 21) não existe mais
    assert "recuo comprável na média (EMA 8" not in section


def _fake_plan_sell_triggered():
    return {"setup_state": "sem_setup",
            "pattern": {"trigger": 97.0, "state": "acionado", "direction": "venda"}}


def test_section_mitigation1_15m_sell_caps_liquidacao_at_aguardar(monkeypatch):
    """Mitigação 1 provada na seção: liquidação que iria AGIR, com 1-2-3 de venda no
    15m, é vetada a AGUARDAR (o recuo virou ruptura)."""
    monkeypatch.setattr(em, "build_price_chart", lambda s, d, timeframe="1d": _fake_downtrend_at_media_chart())
    monkeypatch.setattr(em, "build_actionable_plan_dict", lambda s, d, tf: _fake_plan_sell_triggered())
    monkeypatch.setattr(em, "_drop_nature", lambda *a, **k: _LIQ_DROP)
    section = build_erick_method_section("AVGO", "2026-08-26", "stock")
    assert "**Estado (Método Erick):** AGUARDAR" in section
    assert "**Estado (Método Erick):** AGIR" not in section


def test_section_fail_open_drop_none_matches_mechanical(monkeypatch):
    """fail-open: _drop_nature=None reproduz a leitura mecânica byte-a-byte (a seção
    é idêntica à de antes do fix, sem bloco de natureza da queda)."""
    monkeypatch.setattr(em, "build_price_chart", lambda s, d, timeframe="1d": _fake_downtrend_at_media_chart())
    monkeypatch.setattr(em, "build_actionable_plan_dict", lambda s, d, tf: _fake_plan_with_realize())
    monkeypatch.setattr(em, "_drop_nature", lambda *a, **k: None)
    section = build_erick_method_section("AVGO", "2026-08-26", "stock")
    # baixa sem natureza da queda → CAIXA (mecânica de hoje), sem bloco de queda.
    assert "**Estado (Método Erick):** CAIXA" in section
    assert "🩸 Natureza da queda" not in section
    assert "Deriva da natureza da queda" not in section


# -------------------------- camada de ponderação: TIER 0 / TIER 2 / TIER 3 --------
_real_earnings_read = em._earnings_read


def test_days_ahead_prefers_field_then_recomputes():
    assert _days_ahead({"days_ahead": 56}, "2026-08-27") == 56
    # payload antigo do cache sem o campo → recalcula da data
    assert _days_ahead({"date": "2026-10-22"}, "2026-08-27") == 56
    assert _days_ahead(None, "2026-08-27") is None
    assert _days_ahead({"date": "lixo"}, "2026-08-27") is None


def test_rsi_series_is_wilder_and_pads_none():
    closes = [float(i) for i in range(1, 60)]  # alta monotônica
    rsi = _rsi_series(closes)
    assert all(v is None for v in rsi[:14])
    assert rsi[14] == 100.0            # só ganhos → RSI 100
    assert rsi[-1] > 99.0
    assert _rsi_series([1.0, 2.0]) == [None, None]


def test_swing_points_finds_local_extremes():
    vals = [1.0, 2.0, 3.0, 2.0, 1.0, 2.0, 3.0, 4.0, 3.0, 2.0]
    highs, lows = _swing_points(vals, k=1)
    assert highs == [2, 7] and lows == [4]  # 2<3>2 e 3<4>3; fundo único no 4


def test_rsi_divergence_bearish_measured():
    """Topo do preço MAIS ALTO com RSI fazendo topo mais baixo = bearish medida.
    Curva validada: rali forte 80→109 (RSI 100) · recuo · rali fraco → 110 (RSI 83)."""
    closes = [80 + i * (29 / 20) for i in range(21)]     # rali forte → 109
    closes += [109 - i * 1.8 for i in range(1, 6)]       # recuo → 100
    closes += [100 + i * 0.5 for i in range(1, 21)]      # rali fraco → 110
    closes += [110 - i * 1.5 for i in range(1, 4)]       # cauda p/ confirmar o swing
    out = _rsi_divergence({"candles": [{"c": c} for c in closes]})
    assert out["measured"] is True
    assert out["kind"] == "bearish"
    assert "109" in out["detail"] and "110" in out["detail"]


def test_rsi_divergence_short_series_is_not_measured():
    out = _rsi_divergence({"candles": [{"c": 100.0}] * 10})
    assert out["measured"] is False
    assert "série curta" in out["detail"]


def test_drop_decelerating_three_states():
    def chart(closes):
        return {"candles": [{"c": c} for c in closes]}

    # queda que encolhe: -10 nas 5 anteriores, -5 nas últimas 5 → desacelerando
    decel = _drop_decelerating(chart([100, 98, 96, 94, 92, 90, 88, 87, 86, 85.5, 85]))
    assert decel["decelerando"] is True
    # queda que acelera: -5 antes, -20 agora → NÃO
    accel = _drop_decelerating(chart([100, 99, 98, 97, 96, 95, 93, 91, 89, 87, 85]))
    assert accel["decelerando"] is False
    # série curta → None (NÃO MEDIDA, não False)
    short = _drop_decelerating(chart([100.0, 99.0, 98.0]))
    assert short["decelerando"] is None
    # sem queda prévia → False com motivo
    rise = _drop_decelerating(chart([100.0 + i for i in range(11)]))
    assert rise["decelerando"] is False and "sem queda prévia" in rise["detail"]


def test_earnings_read_tri_state(monkeypatch):
    """A fonte do calendário tem TRÊS estados e os dois negativos não podem virar a
    mesma frase: sem agenda é informação; fonte caída é ignorância (na_janela=None)."""
    import tradingagents.dataflows.earnings_calendar as ec

    monkeypatch.setattr(em, "_earnings_read", _real_earnings_read)  # desfaz o autouse

    def _status(ret):
        monkeypatch.setattr(ec, "get_next_earnings_status", lambda s, d: ret)
        return em._earnings_read

    read = _status((None, ec.STATUS_SEM_AGENDA))
    out = read("X", "2026-08-27")
    assert out["na_janela"] is False and out["ausente"] is None

    read = _status((None, ec.STATUS_FONTE_INDISPONIVEL))
    out = read("X", "2026-08-27")
    assert out["na_janela"] is None and out["ausente"] is not None

    read = _status(({"date": "2026-10-22", "days_ahead": 56}, ec.STATUS_OK))
    out = read("X", "2026-08-27")
    assert out["na_janela"] is False and out["dias"] == 56

    read = _status(({"date": "2026-08-27", "is_today": True}, ec.STATUS_OK))
    out = read("X", "2026-08-27")
    assert out["na_janela"] is True and out["dias"] == 0


def test_tese_read_declares_monthly_absent(monkeypatch):
    def chart(s, d, timeframe="1d"):
        # ambos os frames de tese em alta (pilha empilhada); série longa p/ o RSI
        closes = [100.0 + i * 0.5 for i in range(60)]
        last = closes[-1]
        return {"candles": [{"c": c} for c in closes],
                "ema": {"8": [last], "21": [last - 1.0], "50": [last - 2.0]}}

    monkeypatch.setattr(em, "build_price_chart", chart)
    tese = _tese_read("INTC", "2026-08-27")
    assert tese["regime"] == "alta" and tese["frame"] == "1w"
    assert tese["leituras"] == {"1w": "alta", "1d": "alta"}
    assert any("mensal (1mo)" in a for a in tese["ausentes"])


# ------------------------------------------- a PORTA TIER 2 (o fix do INTC) ------
def _factors_full(**over):
    """Fatores do INTC 27/08: as 5 condições presentes. Cada teste da guarda
    remove/neutraliza UMA — a porta tem que fechar."""
    f = {
        "tese": {"regime": "alta", "frame": "1w", "leituras": {"1w": "alta", "1d": "alta"},
                 "divergencias": {"1w": {"measured": False, "kind": None, "detail": ""},
                                  "1d": {"measured": False, "kind": None, "detail": ""}},
                 "ausentes": []},
        "earnings": {"status": "ok", "ev": {"date": "2026-10-22", "days_ahead": 56},
                     "dias": 56, "na_janela": False, "ausente": None,
                     "leitura": "sem balanço até 2026-10-22 (56 dias)"},
        "divergencia": {"measured": False, "kind": None, "detail": ""},
        "decel": {"decelerando": True, "detail": ""},
        "ancora": {"nome": "NVDA", "em_alta": True, "bateu_balanco": True},
        "ausentes": [],
    }
    f.update(over)
    return f


def test_gate_intc_opens_and_decides_initial():
    """Aceitação spec §5.1: downtrend 4h + tese semanal alta + as demais condições
    → AGUARDAR / posição inicial — NÃO caixa. O 4h foi rebaixado a TIMING."""
    factors = _factors_full()
    d = _decide(_read("baixa", below=True), None, False, factors)
    assert d["acao"] == "AGUARDAR"
    assert d["peso"] == "posição inicial"
    assert "TIMING" in d["entrada"]
    # o Estado espelha: AGUARDAR (não CAIXA-tese)
    assert _estado(d["acao"], "baixa", None, True) == "AGUARDAR"
    assert _estado(d["acao"], "baixa", None, False) == "CAIXA"


@pytest.mark.parametrize("label,over,drop_cls", [
    ("sem_tese_alta", {"tese": dict(_factors_full()["tese"], regime="baixa")}, None),
    ("tese_ausente", {"tese": dict(_factors_full()["tese"], regime=None)}, None),
    ("queda_nao_desacelera", {"decel": {"decelerando": False, "detail": ""}}, None),
    ("decel_nao_medido", {"decel": {"decelerando": None, "detail": ""}}, None),
    ("balanco_na_janela", {"earnings": dict(_factors_full()["earnings"], na_janela=True)}, None),
    ("balanco_nao_medido", {"earnings": dict(_factors_full()["earnings"], na_janela=None)}, None),
    ("ancora_fora_de_alta", {"ancora": {"nome": "NVDA", "em_alta": False, "bateu_balanco": True}}, None),
    ("ancora_ausente", {"ancora": None}, None),
    ("divergencia_bearish_na_tese", {"tese": dict(_factors_full()["tese"],
        divergencias={"1w": {"measured": True, "kind": "bearish", "detail": ""},
                      "1d": {"measured": False, "kind": None, "detail": ""}})}, None),
    ("fraqueza_veta", {}, "fraqueza"),
])
def test_gate_fail_closed_any_condition_missing(label, over, drop_cls):
    """A porta NÃO afrouxa: qualquer condição ausente/não medida/contrária → fecha e
    vale o filtro de hoje (caixa contra médias invertidas)."""
    factors = _factors_full(**over)
    assert _gate_abre(_read("baixa", below=True), drop_cls, factors) is False
    d = _decide(_read("baixa", below=True), {"classification": drop_cls} if drop_cls else None,
                False, factors)
    assert d["peso"] == "caixa"


def test_gate_never_touches_non_downtrend_or_without_factors():
    """A porta só existe no ramo da baixa; sem factors=None ela é fechada por
    construção (invariante byte-a-byte dos outros ramos preservado)."""
    assert _gate_abre(_read("alta", at_media=True), None, _factors_full()) is False
    assert _gate_abre(_read("baixa", below=True), None, None) is False


def test_decide_without_factors_is_byte_for_byte():
    """factors=None (default) reproduz a mecânica de hoje BYTE-A-BYTE — o mesmo
    contrato do drop=None."""
    for trend, kw in [("alta", {"at_media": True}), ("baixa", {"below": True}),
                      ("transicao", {})]:
        r = _read(trend, **kw)
        assert _decide(r) == _decide(r, None, False, None)
        d = _decide(r)
        assert _estado(d["acao"], trend) == _estado(d["acao"], trend, None, False)

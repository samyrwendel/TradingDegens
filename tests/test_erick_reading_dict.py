"""O DICT do Método Erick (card da tela) == a DECISÃO do módulo (task 20260904-003).

`erick_reading_dict` compõe as MESMAS funções de `build_erick_method_section` (o
texto do analista `erick`). Este teste SOLDA os dois: a decisão do card (estado,
ação, entrada, saída, peso) tem de ser LITERALMENTE a que o módulo renderiza — se
alguém mudar um sem o outro, o teste quebra. Hermético: os seams de dado
(build_price_chart, build_actionable_plan_dict, _drop_nature, _factors, _fine_plan)
são sintéticos; a lógica de decisão real (_ema_read/_decide/_estado/_saida) roda.
"""

import pytest

import tradingagents.agents.utils.erick_method as em
from tradingagents.agents.utils.erick_method import (
    build_erick_method_section,
    erick_reading_dict,
)


@pytest.fixture(autouse=True)
def _no_earnings_network(monkeypatch):
    monkeypatch.setattr(
        em, "_earnings_read",
        lambda s, d: {"status": "sem_agenda", "ev": None, "dias": None,
                      "na_janela": False, "ausente": None,
                      "leitura": "sem data de balanço publicada — sem risco de evento conhecido"},
    )


def _monta_seams(monkeypatch, close, e8, e21, e50):
    """Chart sintético (1 candle + EMAs) e planos hermeticos — a decisão sai do
    _ema_read/_decide reais sobre estes números."""
    chart = {"candles": [{"c": close}], "ema": {"8": [e8], "21": [e21], "50": [e50]}}
    plan = {"realize_zone": {"label": "topo anterior", "price": round(close * 1.1, 2),
                             "low": None, "high": None},
            "pattern": None, "buy_zone": None, "stop": None, "target": None,
            "invalidation": None, "risk_reward": None}
    monkeypatch.setattr(em, "build_price_chart", lambda s, d, timeframe=None: chart)
    monkeypatch.setattr(em, "build_actionable_plan_dict", lambda s, d, f: plan)
    monkeypatch.setattr(em, "_drop_nature", lambda s, d, a: {"classification": None})
    monkeypatch.setattr(em, "_fine_plan", lambda s, d: None)
    monkeypatch.setattr(
        em, "_factors",
        lambda s, d, chart, drop: {"tese": {}, "earnings": {"na_janela": False, "leitura": "—"},
                                   "ausentes": [], "ancora": {}})


def test_o_card_carrega_LITERALMENTE_a_decisao_do_modulo(monkeypatch):
    """A solda: cada campo de decisão do dict aparece no texto do módulo."""
    _monta_seams(monkeypatch, close=99.2, e8=99.0, e21=98.0, e50=95.0)
    d = erick_reading_dict("AVGO", "2026-09-03", "stock")
    md = build_erick_method_section("AVGO", "2026-09-03", "stock")
    assert d["disponivel"] is True, d
    for campo in ("estado", "entrada", "saida", "peso"):
        assert d[campo] and d[campo] in md, (campo, d[campo])


def test_o_mapeamento_da_decisao_esta_pinado(monkeypatch):
    """Pino do vocabulário: alta + toque na média → AGIR / meia posição (recuo)."""
    _monta_seams(monkeypatch, close=99.2, e8=99.0, e21=98.0, e50=95.0)
    d = erick_reading_dict("AVGO", "2026-09-03", "stock")
    assert d["trend"] == "alta", d
    assert d["at_media"] is True, d               # 99,2 vs EMA8 99,0 → toque
    assert d["acao"] == "AGIR", d
    assert d["peso"] == "meia posição", d
    # regime das médias exposto pro card (o que a fonte computou)
    assert d["e8"] == 99.0 and d["e21"] == 98.0 and d["e50"] == 95.0, d
    assert "8" in d["emas"] and "21" in d["emas"], d


def test_baixa_sem_gate_e_caixa(monkeypatch):
    """DENTE do outro lado: baixa (médias invertidas) → AGUARDAR / caixa."""
    _monta_seams(monkeypatch, close=95.0, e8=96.0, e21=98.0, e50=100.0)
    d = erick_reading_dict("GOOGL", "2026-09-03", "stock")
    assert d["trend"] == "baixa", d
    assert d["acao"] == "AGUARDAR" and d["peso"] == "caixa", d


def test_fora_de_candle_declara_indisponivel(monkeypatch):
    """Sem candle a leitura NÃO inventa — devolve disponivel=False, como a seção."""
    monkeypatch.setattr(em, "build_price_chart", lambda s, d, timeframe=None: {"candles": [], "ema": {}})
    d = erick_reading_dict("XYZ", "2026-09-03", "stock")
    assert d["disponivel"] is False, d
    assert "motivo" in d

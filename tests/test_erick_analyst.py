"""The on-demand Erick-method analyst: wiring + the deterministic method read.

No LLM and no network here — the graph wiring is asserted structurally and the
method read is driven with a synthetic (monkeypatched) chart/plan so the verdict
mapping is pinned deterministically.
"""

import pytest

import tradingagents.agents.utils.erick_method as em
from tradingagents.agents.utils.erick_method import (
    _decide,
    build_erick_method_section,
    ensure_erick_method_coverage,
)
from tradingagents.graph.analyst_execution import build_analyst_execution_plan
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.webui.progress import build_plan
from tradingagents.webui.runner import extract_result, select_analysts_for_asset


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
    section = build_erick_method_section("BTC-USD", "2026-08-24", "crypto")
    # The four things the acceptance requires, all present:
    assert "intradiário" in section  # timeframe declared
    assert "4 horas" in section
    assert "**Entrada (recuo à média):**" in section
    assert "**Saída (antes da reversão):**" in section
    assert "**Peso relativo do trade:**" in section
    # method-coherent framing that a market-analyst clone would not carry
    assert "Tático × estrutural" in section


def test_section_stock_reads_intraday_like_crypto(monkeypatch):
    """An equity now has keyless intraday (yfinance), so the Erick section reads the
    4h swing frame for a stock too — no longer a daily-only 'no intraday for stocks'
    fallback (fork brief 25/08 item 6)."""
    monkeypatch.setattr(em, "build_price_chart", lambda s, d, timeframe="1d": _fake_uptrend_at_media_chart())
    monkeypatch.setattr(em, "build_actionable_plan_dict", lambda s, d, tf: _fake_plan_with_realize())
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


def test_coverage_is_fail_open(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("feed exploded")

    monkeypatch.setattr(em, "build_price_chart", _boom)
    # enrichment must never break the report
    assert ensure_erick_method_coverage("PROSE", "BTC-USD", "2026-08-24", "crypto") == "PROSE"

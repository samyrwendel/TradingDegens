"""The runner drives the engine on a worker thread and extracts display fields.

A fake graph stands in for TradingAgentsGraph so these tests never call an LLM.
"""

import time

import pytest

import tradingagents.webui.runner as runner_module
from tradingagents.webui.runner import (
    AnalysisRunner,
    _pipeline_version,
    extract_result,
    fetch_derivatives_report,
    module_axes,
    select_analysts_for_asset,
    timeframes_for_asset,
)
from tradingagents.webui.store import HistoryStore


@pytest.fixture(autouse=True)
def _stub_price_chart(monkeypatch):
    """Keep the worker tests hermetic (no network).

    ``_worker`` always calls ``fetch_price_chart`` — a real, date-guarded price
    series fetch — after the fake graph returns. No test here asserts on the
    chart payload (chart building has its own coverage in test_price_structure.py),
    and the live fetch can race the ``_wait`` deadline under load, making
    ``test_runner_persists_to_history`` and its siblings flaky in the full suite.
    """
    monkeypatch.setattr(runner_module, "fetch_price_chart", lambda t, d, tf="1d", method="padrao": {})

FINAL_STATE = {
    "final_trade_decision": "Rating: Buy\nStrong conviction.",
    "investment_plan": "Rating: Overweight",
    "trader_investment_plan": "Enter half now.",
    "market_report": "## Multi-timeframe\nWeekly up, daily up.",
    "sentiment_report": "Neutral chatter.",
    "news_report": "Prediction markets: 62% cut.",
    "fundamentals_report": "Solid margins.",
    "investment_debate_state": {
        "bull_history": "Bull: growth accelerating.",
        "bear_history": "Bear: valuation stretched.",
        "judge_decision": "Manager: lean bull.",
    },
    "risk_debate_state": {
        "judge_decision": "Final: Buy, size modestly.",
        "aggressive_history": "push it",
        "conservative_history": "careful",
        "neutral_history": "balanced",
    },
}


class _FakeGraph:
    def __init__(self, callbacks, final_state, signal, raise_exc=None):
        self.callbacks = callbacks
        self.final_state = final_state
        self.signal = signal
        self.raise_exc = raise_exc

    def propagate(self, ticker, date, asset_type="stock", timeframe="1d"):
        self.timeframe = timeframe
        import uuid as _uuid

        from tradingagents.webui.progress import ProgressCallbackHandler
        for cb in self.callbacks:
            # Only the progress handler consumes node starts; the real
            # UsageMetadataCallbackHandler intentionally leaves it unimplemented.
            if isinstance(cb, ProgressCallbackHandler):
                for node in ("Market Analyst", "Portfolio Manager"):
                    cb.on_chat_model_start(
                        {}, [], run_id=_uuid.uuid4(),
                        metadata={"langgraph_node": node},
                    )
            if hasattr(cb, "usage_metadata"):
                cb.usage_metadata.update(
                    {"gpt-4o-mini": {"input_tokens": 120_000, "output_tokens": 6_000}}
                )
        if self.raise_exc:
            raise self.raise_exc
        return self.final_state, self.signal


def _factory(final_state=FINAL_STATE, signal="Buy", raise_exc=None):
    def make(config, selected_analysts, callbacks):
        return _FakeGraph(callbacks, final_state, signal, raise_exc)
    return make


def _wait(runner, run_id, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = runner.status(run_id)
        if snap and snap["status"] != "running":
            return snap
        time.sleep(0.02)
    raise AssertionError("run did not finish in time")


def test_select_analysts_crypto_drops_fundamentals():
    assert "fundamentals" not in select_analysts_for_asset("crypto")
    assert select_analysts_for_asset("stock") == ["market", "social", "news", "fundamentals"]


def test_extract_result_surfaces_both_theses():
    r = extract_result(FINAL_STATE, "Buy")
    assert r["verdict"] == "Buy"
    assert r["bull"] == "Bull: growth accelerating."
    assert r["bear"] == "Bear: valuation stretched."
    assert r["risk_decision"] == "Final: Buy, size modestly."
    assert "Multi-timeframe" in r["market_report"]


def test_extract_result_missing_fields_default_empty():
    r = extract_result({}, "Hold")
    assert r["bull"] == "" and r["bear"] == ""
    assert r["verdict"] == "Hold"
    assert r["degraded"] == []


def test_extract_result_surfaces_degraded_sources():
    fs = dict(FINAL_STATE)
    fs["degraded_sources"] = [
        {"label": "News Analyst", "report_key": "news_report",
         "reason": "RuntimeError: down", "kind": "missing"}
    ]
    r = extract_result(fs, "Buy")
    assert r["degraded"] == fs["degraded_sources"]


def test_extract_result_normalizes_a_resumed_legacy_degraded_note():
    """Regression (task 20260828-003): a checkpoint written before the structured
    entry existed carries a bare string; the UI must still NAME the source."""
    fs = dict(FINAL_STATE)
    fs["degraded_sources"] = [
        "Bear Researcher (texto suspeito: severity=suspect invented=20 (1.73%) [DILUÇÃO])"
    ]
    r = extract_result(fs, "Buy")
    assert r["degraded"] == [{
        "label": "Bear Researcher",
        "report_key": "",
        "reason": "texto suspeito: severity=suspect invented=20 (1.73%) [DILUÇÃO]",
        "kind": "suspect",
    }]


def test_extract_result_exposes_single_canonical_final_decision():
    """One binding decision per run in a single field — the pt-BR enum of the
    risk/portfolio verdict (bug: four modules each stated a 'final' action)."""
    cases = {
        "Buy": "COMPRAR", "Overweight": "AUMENTAR", "Hold": "MANTER",
        "Underweight": "REDUZIR", "Sell": "VENDER",
    }
    for signal, pt in cases.items():
        r = extract_result(FINAL_STATE, signal)
        assert r["final_decision"] == pt
        # It IS the verdict — never a competing value.
        assert r["verdict"] == signal


def test_extract_result_final_decision_falls_back_to_signal():
    """An unexpected signal string degrades to itself, never crashes."""
    r = extract_result({}, "Weird")
    assert r["final_decision"] == "Weird"


def test_extract_result_has_audit_and_axes_defaults():
    """Item 8/10: the audit footer + reading-axes fields are always present (the
    runner fills them on completion; here they default to empty, never missing)."""
    r = extract_result({}, "Hold")
    assert r["audit"] == {}
    assert r["axes"] == {}
    assert r["as_of_price"] is None


def test_module_axes_every_module_declares_eixo_and_horizonte():
    """Item 8: each module carries an axis + horizon so a weekly-up/daily-down/reduce/
    wait spread reads as LAYERS, not a contradiction. The verdict is the position axis."""
    axes = module_axes()
    for key in ("veredito", "juiz", "tecnico", "erick", "trader"):
        assert axes[key]["eixo"] and axes[key]["horizonte"]
    assert axes["veredito"]["eixo"] == "posição"
    assert axes["erick"]["eixo"] == "tático"
    assert axes["tecnico"]["eixo"] == "estrutural"


def test_pipeline_version_is_a_string():
    """Item 10: audit footer version resolves to a string, never raises."""
    v = _pipeline_version()
    assert isinstance(v, str) and v


def test_runner_completes_and_extracts(tmp_path):
    store = HistoryStore(tmp_path)
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=store, graph_factory=_factory())
    run_id = runner.start("AAPL", "2026-08-22")
    snap = _wait(runner, run_id)
    assert snap["status"] == "done"
    assert snap["asset_type"] == "stock"
    assert snap["result"]["verdict"] == "Buy"
    assert snap["result"]["bull"] and snap["result"]["bear"]
    assert snap["cost"]["usd"] > 0
    assert snap["progress"]["percent"] == 100


def test_runner_detects_crypto(tmp_path, monkeypatch):
    # stub the vendor fetch so this stays a hermetic unit test (no network)
    monkeypatch.setattr(runner_module, "fetch_derivatives_report", lambda t, d: "")
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    run_id = runner.start("BTC-USD", "2026-08-22")
    snap = _wait(runner, run_id)
    assert snap["asset_type"] == "crypto"


def test_fetch_derivatives_report_returns_vendor_text(monkeypatch):
    import tradingagents.dataflows.interface as itf
    monkeypatch.setattr(itf, "route_to_vendor",
                        lambda name, sym, date: f"## Funding (Hyperliquid) {sym}")
    assert "Hyperliquid" in fetch_derivatives_report("BTC-USD", "2026-08-22")


def test_fetch_derivatives_report_fails_open(monkeypatch):
    import tradingagents.dataflows.interface as itf

    def boom(*a, **k):
        raise RuntimeError("vendor down")

    monkeypatch.setattr(itf, "route_to_vendor", boom)
    assert fetch_derivatives_report("BTC-USD", "2026-08-22") == ""


def test_crypto_run_attaches_derivatives_report(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_module, "fetch_derivatives_report",
                        lambda t, d: "## Funding (Hyperliquid info API)")
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    run_id = runner.start("BTC-USD", "2026-08-22")
    snap = _wait(runner, run_id)
    assert "Hyperliquid" in snap["result"]["derivatives_report"]


def test_stock_run_has_no_derivatives_report(tmp_path):
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    run_id = runner.start("AAPL", "2026-08-22")
    snap = _wait(runner, run_id)
    assert snap["result"]["derivatives_report"] == ""


# ---------------------- calendário de resultados (task 20260901-044) -----------
def test_extract_result_earnings_defaults_empty():
    assert extract_result({}, "Hold")["earnings"] == {}


def test_full_run_attaches_earnings_field(tmp_path, monkeypatch):
    """O método completo (padrão/erick) preenche ``result["earnings"]`` — mesma
    leitura tri-state que o Erick já usa internamente como fator, exposta como
    campo estruturado pra tela (reusa o cache DA-058, sem fetch novo)."""
    fake = {"status": "ok", "date": "2026-09-10", "days_ahead": 5,
            "in_window": True, "window_days": 21}
    chamadas = []

    def fake_earnings(ticker, date, window_days, asset_type):
        chamadas.append((ticker, date, window_days, asset_type))
        return fake

    monkeypatch.setattr(runner_module, "earnings_window_status", fake_earnings)
    monkeypatch.setattr(runner_module, "plano_com_storm", lambda t, d, tf, method: {})
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    run_id = runner.start("AAPL", "2026-08-22")
    snap = _wait(runner, run_id)
    assert snap["result"]["earnings"] == fake
    assert chamadas == [("AAPL", "2026-08-22", runner_module._EARNINGS_WINDOW_DAYS, "stock")]


def test_structural_run_attaches_earnings_field(tmp_path, monkeypatch):
    """O atalho estrutural (setup123/storm123, $0 de LLM) TAMBÉM preenche
    ``result["earnings"]`` — o dado é do ATIVO, não do pipeline que rodou."""
    fake = {"status": "sem_agenda", "date": None, "days_ahead": None,
            "in_window": False, "window_days": 21}
    monkeypatch.setattr(runner_module, "earnings_window_status", lambda *a, **k: fake)
    monkeypatch.setattr(runner_module, "plano_com_storm", lambda t, d, tf, method: {})
    monkeypatch.setattr(runner_module, "fetch_price_chart",
                        lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "leitura_multiframe",
                        lambda ticker, date, asset_type, method, timeframe: {})
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path))
    run_id = runner.start("AAPL", "2026-08-22", method="setup123", reuse=False)
    snap = _wait(runner, run_id)
    assert snap["result"]["earnings"] == fake


def test_history_is_watchlist_that_only_grows(tmp_path):
    # Lista de observação (task 011): history() mostra UM item por ticker já pesquisado,
    # persistente e SÓ CRESCE — um ativo antigo (fora da janela dos 25 runs) continua
    # na lista, com o veredito do run mais recente e a contagem de análises.
    store = HistoryStore(tmp_path)

    def rec(rid, tk, verdict):
        return {"run_id": rid, "ticker": tk, "date": "2026-08-22", "asset_type": "stock",
                "status": "done", "verdict": verdict, "cost_usd": 0.01, "elapsed": 1.0,
                "finished_at": "2026-08-22T12:00:00", "result": {"verdict": verdict}}

    store.save(rec("old", "OLD", "Buy"))            # o mais antigo
    for i in range(30):
        store.save(rec(f"r{i}", f"T{i}", "Hold"))
    store.save(rec("old2", "OLD", "Sell"))          # 2ª análise do OLD (mais recente)
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=store, graph_factory=_factory())
    hist = runner.history()
    tickers = {r["ticker"] for r in hist}
    assert "OLD" in tickers                          # não caiu por causa do limite
    old = next(r for r in hist if r["ticker"] == "OLD")
    assert old["run_id"] == "old2" and old["verdict"] == "Sell"   # o mais recente
    assert old["count"] == 2                          # duas análises do OLD
    assert len([r for r in hist if r["ticker"] == "OLD"]) == 1    # um item por ticker


def test_runner_persists_to_history(tmp_path):
    store = HistoryStore(tmp_path)
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=store, graph_factory=_factory())
    run_id = runner.start("MSFT", "2026-08-22")
    _wait(runner, run_id)
    recent = runner.history()
    assert any(r["run_id"] == run_id and r["verdict"] == "Buy" for r in recent)
    # a run this object "forgot" is still resolvable from disk
    full = store.get(run_id)
    assert full["result"]["bear"] == "Bear: valuation stretched."


def test_runner_error_path_is_captured(tmp_path):
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path)}, store=HistoryStore(tmp_path),
        graph_factory=_factory(raise_exc=RuntimeError("boom")),
    )
    run_id = runner.start("AAPL", "2026-08-22")
    snap = _wait(runner, run_id)
    assert snap["status"] == "error"
    assert "boom" in snap["error"]
    # Nada concluído (o fake não produziu texto de etapa) → sem parcial, result None:
    # erro honesto de "tela vazia" só quando realmente não há o que preservar.
    assert snap["result"] is None


class _FakeGraphPartial:
    """Fake que PRODUZ o texto de algumas etapas (raciocínio-ao-vivo) e então ERRA no
    meio — pra provar que o erro preserva as concluídas (task 015)."""

    def __init__(self, callbacks, texts, raise_exc):
        self.callbacks = callbacks
        self.texts = texts
        self.raise_exc = raise_exc

    def propagate(self, ticker, date, asset_type="stock", timeframe="1d"):
        from tradingagents.webui.progress import (
            ProgressCallbackHandler, ThinkingCallbackHandler,
        )
        import uuid as _uuid
        for cb in self.callbacks:
            if isinstance(cb, ProgressCallbackHandler):
                # avança o stepper até o nó que vai falhar (Research Manager / juiz)
                for node in ("Market Analyst", "Bull Researcher", "Research Manager"):
                    cb.on_chat_model_start({}, [], run_id=_uuid.uuid4(),
                                           metadata={"langgraph_node": node})
            if isinstance(cb, ThinkingCallbackHandler):
                for node, text in self.texts.items():
                    cb.tracker.set_by_node(node, text)
        raise self.raise_exc


def test_error_midway_preserves_completed_steps_as_partial(tmp_path):
    """Task 015: um erro no meio NÃO zera a análise — o result parcial traz as etapas
    concluídas (analistas + debate) e marca a etapa que falhou, em vez de result None."""
    texts = {
        "Market Analyst": "Leitura técnica: tendência de alta no diário, acima da MMS200.",
        "Bull Researcher": "Tese de alta: momentum forte e volume crescente.",
        "Bear Researcher": "Tese de baixa: valuation esticado após a corrida.",
    }

    def factory(config, selected_analysts, callbacks):
        return _FakeGraphPartial(callbacks, texts, RuntimeError("429 rate limit no juiz"))

    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=factory)
    run_id = runner.start("AAPL", "2026-08-22")
    snap = _wait(runner, run_id)
    assert snap["status"] == "error"
    r = snap["result"]
    assert r is not None, "erro no meio deve preservar um result PARCIAL, não None"
    assert r.get("partial") is True
    # as etapas concluídas estão preservadas
    assert "tendência de alta" in r["market_report"]
    assert "momentum forte" in r["bull"]
    assert "valuation esticado" in r["bear"]
    # marca a etapa que falhou e para nela (não zera)
    assert r.get("failed_step") and r["failed_step"].get("label")
    # o parcial vai pro histórico (não é 'done', verdict None) mas com o result preservado
    rec = HistoryStore(tmp_path).get(run_id) if False else runner.store.get(run_id)
    assert rec["status"] == "error"
    assert (rec.get("result") or {}).get("partial") is True
    assert "tendência de alta" in rec["result"]["market_report"]


def test_runner_empty_ticker_rejected(tmp_path):
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    try:
        runner.start("   ", "2026-08-22")
    except ValueError:
        return
    raise AssertionError("expected ValueError for empty ticker")


# --------------------------------------------------- timeframe selector (005) ---
def test_timeframes_for_asset_ladder():
    """Widest→narrowest. The full intraday ladder is operable for BOTH crypto and
    equity now (each frame has a real keyless source — exchange for crypto, yfinance
    for equity; a symbol/date with no candle degrades honestly on demand). This is
    the single source both UI + endpoint validate against (task 007/25-08)."""
    assert timeframes_for_asset("crypto") == ["1w", "1d", "4h", "1h", "15m"]
    assert timeframes_for_asset("stock") == ["1w", "1d", "4h", "1h", "15m"]


def test_run_result_carries_timeframe_ladder(tmp_path):
    """Every run persists the shown frame + the ladder so a history reload can
    rebuild the selector."""
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    run_id = runner.start("AAPL", "2026-08-22")
    snap = _wait(runner, run_id)
    assert snap["result"]["timeframe"] == "1d"
    assert snap["result"]["timeframes"] == ["1w", "1d", "4h", "1h", "15m"]


# ------------------------------------------- name resolution / search (035) ---
def test_start_resolves_name_to_symbol(tmp_path, monkeypatch):
    """Typing a company NAME resolves to the symbol before the run (Microsoft->MSFT)."""
    monkeypatch.setattr(runner_module, "resolve_ticker_query",
                        lambda term: "MSFT" if term == "Microsoft" else None)
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    run_id = runner.start("Microsoft", "2026-08-22")
    snap = _wait(runner, run_id)
    assert snap["ticker"] == "MSFT"


def test_start_keeps_plain_symbol_untouched(tmp_path, monkeypatch):
    """A plain ticker passes through as-is (resolver returns None for a symbol)."""
    monkeypatch.setattr(runner_module, "resolve_ticker_query", lambda term: None)
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    run_id = runner.start("AAPL", "2026-08-22")
    snap = _wait(runner, run_id)
    assert snap["ticker"] == "AAPL"


def test_runner_search_and_names_delegate(tmp_path, monkeypatch):
    """The runner exposes search + batch name resolution for the UI (fail-open)."""
    monkeypatch.setattr(runner_module, "fetch_symbol_search",
                        lambda term, limit=8: [{"symbol": "MSFT", "name": "Microsoft Corporation"}])
    monkeypatch.setattr(runner_module, "fetch_symbol_names",
                        lambda symbols: {"MSFT": "Microsoft Corporation"})
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    assert runner.search_symbols("micro")[0]["symbol"] == "MSFT"
    assert runner.resolve_names(["MSFT"]) == {"MSFT": "Microsoft Corporation"}


def test_resolve_ticker_query_symbol_is_offline(monkeypatch):
    """A plain ticker must resolve to None WITHOUT any network call (the search seam
    would fail the test if reached)."""
    import tradingagents.dataflows.symbol_search as ss
    monkeypatch.setattr(ss, "_yahoo_search",
                        lambda *a, **k: pytest.fail("plain ticker must not hit the network"))
    assert runner_module.resolve_ticker_query("AAPL") is None
    assert runner_module.resolve_ticker_query("BTC-USD") is None


def test_timeframe_view_recomputes_for_crypto(tmp_path, monkeypatch):
    """A valid crypto frame recomputes chart + plan on that frame (no network here:
    the two builders are stubbed)."""
    monkeypatch.setattr(runner_module, "fetch_price_chart",
                        lambda t, d, tf="1d", method="padrao": {"timeframe": tf, "candles": [{"d": "x"}]})
    monkeypatch.setattr(runner_module, "fetch_actionable_plan",
                        lambda t, d, tf="1d", method="padrao": {"timeframe": tf, "setup_state": "ativo"})
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    view = runner.timeframe_view("btc-usd", "2026-08-22", "15m")
    assert view["asset_type"] == "crypto"
    assert view["timeframe"] == "15m" and view["requested"] == "15m"
    assert view["degraded"] is False and view["notice"] is None
    assert view["price_chart"]["timeframe"] == "15m"
    assert view["actionable"]["timeframe"] == "15m"
    assert view["timeframes"] == ["1w", "1d", "4h", "1h", "15m"]
    assert view["ticker"] == "BTC-USD"  # normalized upper


def test_timeframe_view_weekly_for_stock(tmp_path, monkeypatch):
    """The weekly frame is resampled from the daily series, so /api/chart?tf=1w must
    work for an EQUITY — it must not reject the stock with 'indisponível' (task 007)."""
    monkeypatch.setattr(runner_module, "fetch_price_chart",
                        lambda t, d, tf="1d", method="padrao": {"timeframe": tf, "candles": [{"d": "2025-01-12"}]})
    monkeypatch.setattr(runner_module, "fetch_actionable_plan",
                        lambda t, d, tf="1d", method="padrao": {"timeframe": tf, "setup_state": "ativo"})
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    view = runner.timeframe_view("AAPL", "2026-08-22", "1w")
    assert view["asset_type"] == "stock"
    assert view["timeframe"] == "1w" and view["requested"] == "1w"
    assert view["degraded"] is False and view["notice"] is None
    assert view["price_chart"]["timeframe"] == "1w"
    assert view["timeframes"] == ["1w", "1d", "4h", "1h", "15m"]


def test_timeframe_view_recomputes_intraday_for_stock(tmp_path, monkeypatch):
    """An equity intraday frame is now operable (yfinance keyless): a stock 15m
    request recomputes chart + plan on that frame, no rejection, no degrade."""
    monkeypatch.setattr(runner_module, "fetch_price_chart",
                        lambda t, d, tf="1d", method="padrao": {"timeframe": tf, "candles": [{"d": "x"}]})
    monkeypatch.setattr(runner_module, "fetch_actionable_plan",
                        lambda t, d, tf="1d", method="padrao": {"timeframe": tf, "setup_state": "ativo"})
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    view = runner.timeframe_view("MSFT", "2026-08-22", "15m")
    assert view["asset_type"] == "stock"
    assert view["timeframe"] == "15m" and view["requested"] == "15m"
    assert view["degraded"] is False and view["notice"] is None
    assert view["price_chart"]["timeframe"] == "15m"
    assert view["timeframes"] == ["1w", "1d", "4h", "1h", "15m"]


def test_timeframe_view_stock_intraday_unavailable_not_swapped(tmp_path, monkeypatch):
    """A stock intraday frame the source genuinely has no candle for
    (``intradiario_indisponivel`` — e.g. a backtest beyond yfinance's window) stays
    on the requested frame with an honest plan, NOT degraded to the daily."""
    monkeypatch.setattr(
        runner_module, "fetch_price_chart",
        lambda t, d, tf="1d", method="padrao": {"timeframe": tf, "candles": ([{"d": "x"}] if tf == "1d" else [])},
    )
    monkeypatch.setattr(
        runner_module, "fetch_actionable_plan",
        lambda t, d, tf="1d", method="padrao": {"timeframe": tf, "setup_state": ("ativo" if tf == "1d" else "intradiario_indisponivel")},
    )
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    view = runner.timeframe_view("AAPL", "2019-01-15", "15m")
    assert view["degraded"] is False and view["notice"] is None
    assert view["requested"] == "15m" and view["timeframe"] == "15m"
    assert view["actionable"]["setup_state"] == "intradiario_indisponivel"


def test_timeframe_view_rejects_unknown_frame(tmp_path):
    """A frame no asset offers (e.g. ``3m``) is still a ValueError — the ladder is
    the single source of truth and does not include it."""
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    with pytest.raises(ValueError):
        runner.timeframe_view("AAPL", "2026-08-22", "3m")


def test_timeframe_view_falls_back_on_intraday_outage(tmp_path, monkeypatch):
    """A crypto intraday source outage (empty candles) degrades to the daily and
    says so with a notice — never fabricates a bar (criterion 7)."""
    monkeypatch.setattr(
        runner_module, "fetch_price_chart",
        lambda t, d, tf="1d", method="padrao": {"timeframe": tf, "candles": ([{"d": "x"}] if tf == "1d" else [])},
    )
    monkeypatch.setattr(
        runner_module, "fetch_actionable_plan",
        lambda t, d, tf="1d", method="padrao": {"timeframe": tf, "setup_state": ("ativo" if tf == "1d" else "sem_dado")},
    )
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    view = runner.timeframe_view("BTC-USD", "2026-08-22", "1h")
    assert view["degraded"] is True
    assert view["requested"] == "1h" and view["timeframe"] == "1d"
    assert view["price_chart"]["candles"]  # daily fallback has candles
    assert "indisponível" in (view["notice"] or "").lower()


# ------------------------------------------- verdict per timeframe (task 012) ---
def test_start_threads_timeframe_and_stamps(tmp_path, monkeypatch):
    """The requested timeframe reaches graph.propagate and is stamped on the run
    (verdict_timeframe) + the chart opens on that same frame."""
    monkeypatch.setattr(runner_module, "fetch_actionable_plan", lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_derivatives_report", lambda t, d: "")
    seen = {}

    class _Rec:
        def __init__(self, callbacks):
            self.callbacks = callbacks

        def propagate(self, ticker, date, asset_type="stock", timeframe="1d"):
            seen["tf"] = timeframe
            return FINAL_STATE, "Buy"

    def factory(config, selected, callbacks):
        return _Rec(callbacks)

    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=factory)
    run_id = runner.start("BTC-USD", "2026-08-22", timeframe="4h")
    snap = _wait(runner, run_id)
    assert seen["tf"] == "4h"                       # reached the engine
    assert snap["status"] == "done"
    assert snap["verdict_timeframe"] == "4h"        # stamped on the snapshot
    assert snap["result"]["verdict_timeframe"] == "4h"
    assert snap["result"]["timeframe"] == "4h"      # chart opens on the verdict frame
    assert runner.store.get(run_id)["verdict_timeframe"] == "4h"


def test_start_defaults_to_daily(tmp_path):
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    run_id = runner.start("AAPL", "2026-08-22")
    snap = _wait(runner, run_id)
    assert snap["verdict_timeframe"] == "1d"


def test_start_accepts_intraday_timeframe_for_stock(tmp_path):
    """An equity intraday frame is now operable (yfinance keyless): a 15m verdict
    request reaches the engine and is stamped, just like a crypto intraday run."""
    seen = {}

    class _Rec:
        def __init__(self, callbacks):
            self.callbacks = callbacks

        def propagate(self, ticker, date, asset_type="stock", timeframe="1d"):
            seen["tf"] = timeframe
            return FINAL_STATE, "Buy"

    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path),
                            graph_factory=lambda c, s, cb: _Rec(cb))
    run_id = runner.start("AAPL", "2026-08-22", timeframe="15m")
    snap = _wait(runner, run_id)
    assert seen["tf"] == "15m"
    assert snap["status"] == "done"
    assert snap["verdict_timeframe"] == "15m"


def test_start_rejects_unknown_timeframe(tmp_path):
    """A frame no asset offers (``3m``) is still rejected at start (single ladder)."""
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    with pytest.raises(ValueError):
        runner.start("AAPL", "2026-08-22", timeframe="3m")


# ------------------------------------------------ background runs (task 010) ---
def _blocking_factory(gate, final_state=FINAL_STATE, signal="Buy"):
    """A graph whose ``propagate`` blocks on ``gate`` so the run stays ``running``
    long enough to be observed as an in-flight background run (the whole point of
    task 010: a run keeps computing server-side while the client looks elsewhere)."""
    class _Block:
        def __init__(self, callbacks):
            self.callbacks = callbacks

        def propagate(self, ticker, date, asset_type="stock", timeframe="1d"):
            gate.wait(3.0)
            return final_state, signal

    def make(config, selected_analysts, callbacks):
        return _Block(callbacks)

    return make


def test_active_runs_lists_in_flight_run(tmp_path):
    """A run still executing shows up in active_runs / history as ``running`` with
    no verdict yet but a live progress marker — and is deduped to the single
    persisted row once it finishes."""
    import threading

    gate = threading.Event()
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path),
                            graph_factory=_blocking_factory(gate))
    run_id = runner.start("AAPL", "2026-08-22")
    try:
        active = runner.active_runs()
        row = next((r for r in active if r["run_id"] == run_id), None)
        assert row is not None and row["status"] == "running"
        assert row["verdict"] is None
        assert "percent" in row["progress"]
        # /api/history merges the live run in front (it is not on disk yet)
        assert any(r["run_id"] == run_id and r["status"] == "running"
                   for r in runner.history())
    finally:
        gate.set()
    snap = _wait(runner, run_id)
    assert snap["status"] == "done"
    # finished: it leaves the active set and is not double-listed in history
    assert all(r["run_id"] != run_id for r in runner.active_runs())
    matches = [r for r in runner.history() if r["run_id"] == run_id]
    assert len(matches) == 1 and matches[0]["status"] == "done"


def test_timeframe_view_leaves_equity_intraday_note_alone(tmp_path, monkeypatch):
    """When the plan itself is the expected 'intradiario_indisponivel' (the source
    genuinely has no candle for this symbol/date), the view does NOT masquerade it as
    a daily fallback — it returns the explicit unavailable read on the requested
    frame."""
    # Force the allowed set to include an intraday frame so we reach the builders,
    # then have the plan report the equity 'unavailable' state with empty candles.
    monkeypatch.setattr(runner_module, "timeframes_for_asset",
                        lambda at: ["1d", "15m"])
    monkeypatch.setattr(runner_module, "fetch_price_chart",
                        lambda t, d, tf="1d", method="padrao": {"timeframe": tf, "candles": []})
    monkeypatch.setattr(runner_module, "fetch_actionable_plan",
                        lambda t, d, tf="1d", method="padrao": {"timeframe": tf, "setup_state": "intradiario_indisponivel"})
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    view = runner.timeframe_view("AAPL", "2026-08-22", "15m")
    assert view["degraded"] is False
    assert view["timeframe"] == "15m"
    assert view["actionable"]["setup_state"] == "intradiario_indisponivel"


# ─────────── ESTADO TERMINAL ⇒ HISTÓRICO JÁ ESCRITO (DA-130) ────────────────
def test_run_estrutural_so_vira_done_DEPOIS_de_persistir(tmp_path, monkeypatch):
    """Quem vê o estado terminal tem de ver a linha do histórico junto.

    O ``_worker`` já documentava a disciplina ("flipped onto the run only after the
    history write, so any poller that sees a terminal status also sees the persisted
    history row"); o ``_worker_estrutural`` fazia o CONTRÁRIO — virava ``done`` e só
    então persistia. Entre uma coisa e outra, uma consulta via run concluída **sem
    linha no histórico**.

    Não é hipótese: era a corrida que derrubava ``test_a_escada_sobrevive_ao_HISTORICO``
    sob carga da suíte, e na tela seria o histórico sem a análise que acabou de sair.

    O teste força a janela (um ``_persist`` lento) e mede o que um poller veria.
    """
    from tests.test_escada_multiframe import _runner_estrutural

    r = _runner_estrutural(tmp_path, monkeypatch)
    original = r._persist
    visto = {"done_sem_registro": 0}

    def _lento(run, status):
        # A JANELA, aberta de propósito: é exatamente o intervalo que a ordem errada
        # deixava exposto — só que aqui ele dura o bastante pra ser observado.
        for _ in range(40):
            if r.status(run.run_id).get("status") in ("done", "error"):
                visto["done_sem_registro"] += 1
            time.sleep(0.005)
        original(run, status)

    monkeypatch.setattr(r, "_persist", _lento)
    rid = r.start("BTC-USD", "2026-08-31", method="setup123", timeframe="1d",
                  reuse=False)
    fim = time.time() + 30
    while time.time() < fim and r.status(rid).get("status") not in ("done", "error"):
        time.sleep(0.02)

    assert visto["done_sem_registro"] == 0, (
        "houve um intervalo em que a run já se dizia concluída e o histórico ainda "
        "não tinha a linha", visto)
    registro = r.store.get(rid)
    assert registro is not None and registro.get("result") is not None, registro

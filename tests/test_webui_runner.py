"""The runner drives the engine on a worker thread and extracts display fields.

A fake graph stands in for TradingAgentsGraph so these tests never call an LLM.
"""

import time

import pytest

import tradingagents.webui.runner as runner_module
from tradingagents.webui.runner import (
    AnalysisRunner,
    extract_result,
    fetch_derivatives_report,
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
    monkeypatch.setattr(runner_module, "fetch_price_chart", lambda t, d, tf="1d": {})

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


def _wait(runner, run_id, timeout=3.0):
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
    """Widest→narrowest. The weekly (resampled from the daily series) is operable for
    both; crypto also gets the intraday ladder, an equity does not (no keyless feed).
    This is the single source both UI + endpoint validate against (task 007)."""
    assert timeframes_for_asset("crypto") == ["1w", "1d", "4h", "1h", "15m"]
    assert timeframes_for_asset("stock") == ["1w", "1d"]


def test_run_result_carries_timeframe_ladder(tmp_path):
    """Every run persists the shown frame + the ladder so a history reload can
    rebuild the selector."""
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    run_id = runner.start("AAPL", "2026-08-22")
    snap = _wait(runner, run_id)
    assert snap["result"]["timeframe"] == "1d"
    assert snap["result"]["timeframes"] == ["1w", "1d"]


def test_timeframe_view_recomputes_for_crypto(tmp_path, monkeypatch):
    """A valid crypto frame recomputes chart + plan on that frame (no network here:
    the two builders are stubbed)."""
    monkeypatch.setattr(runner_module, "fetch_price_chart",
                        lambda t, d, tf="1d": {"timeframe": tf, "candles": [{"d": "x"}]})
    monkeypatch.setattr(runner_module, "fetch_actionable_plan",
                        lambda t, d, tf="1d": {"timeframe": tf, "setup_state": "ativo"})
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
    work for an EQUITY too (unlike intraday, which an equity has no keyless feed for)
    — it must not reject the stock with 'indisponível' (task 007)."""
    monkeypatch.setattr(runner_module, "fetch_price_chart",
                        lambda t, d, tf="1d": {"timeframe": tf, "candles": [{"d": "2025-01-12"}]})
    monkeypatch.setattr(runner_module, "fetch_actionable_plan",
                        lambda t, d, tf="1d": {"timeframe": tf, "setup_state": "ativo"})
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    view = runner.timeframe_view("AAPL", "2026-08-22", "1w")
    assert view["asset_type"] == "stock"
    assert view["timeframe"] == "1w" and view["requested"] == "1w"
    assert view["degraded"] is False and view["notice"] is None
    assert view["price_chart"]["timeframe"] == "1w"
    assert view["timeframes"] == ["1w", "1d"]


def test_timeframe_view_rejects_intraday_for_stock(tmp_path):
    """An equity has no keyless intraday — an intraday request is a ValueError (the
    UI already disables those buttons)."""
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    with pytest.raises(ValueError):
        runner.timeframe_view("AAPL", "2026-08-22", "15m")


def test_timeframe_view_falls_back_on_intraday_outage(tmp_path, monkeypatch):
    """A crypto intraday source outage (empty candles) degrades to the daily and
    says so with a notice — never fabricates a bar (criterion 7)."""
    monkeypatch.setattr(
        runner_module, "fetch_price_chart",
        lambda t, d, tf="1d": {"timeframe": tf, "candles": ([{"d": "x"}] if tf == "1d" else [])},
    )
    monkeypatch.setattr(
        runner_module, "fetch_actionable_plan",
        lambda t, d, tf="1d": {"timeframe": tf, "setup_state": ("ativo" if tf == "1d" else "sem_dado")},
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
    monkeypatch.setattr(runner_module, "fetch_actionable_plan", lambda t, d, tf="1d": {})
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


def test_start_rejects_intraday_timeframe_for_stock(tmp_path):
    """An equity has no keyless intraday frame — a 15m verdict request is rejected
    here just like /api/chart rejects the 15m chart."""
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    with pytest.raises(ValueError):
        runner.start("AAPL", "2026-08-22", timeframe="15m")


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
    """When the plan itself is the expected 'intradiário indisponível para ação'
    (equity), the view does NOT masquerade it as a daily fallback — it returns the
    explicit unavailable read. (Defensive: the UI blocks this, but the endpoint is
    reachable directly.)"""
    # Force the allowed set to include an intraday frame so we reach the builders,
    # then have the plan report the equity 'unavailable' state with empty candles.
    monkeypatch.setattr(runner_module, "timeframes_for_asset",
                        lambda at: ["1d", "15m"])
    monkeypatch.setattr(runner_module, "fetch_price_chart",
                        lambda t, d, tf="1d": {"timeframe": tf, "candles": []})
    monkeypatch.setattr(runner_module, "fetch_actionable_plan",
                        lambda t, d, tf="1d": {"timeframe": tf, "setup_state": "intradiario_indisponivel"})
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    view = runner.timeframe_view("AAPL", "2026-08-22", "15m")
    assert view["degraded"] is False
    assert view["timeframe"] == "15m"
    assert view["actionable"]["setup_state"] == "intradiario_indisponivel"

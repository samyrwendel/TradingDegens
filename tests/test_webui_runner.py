"""The runner drives the engine on a worker thread and extracts display fields.

A fake graph stands in for TradingAgentsGraph so these tests never call an LLM.
"""

import time

import tradingagents.webui.runner as runner_module
from tradingagents.webui.runner import (
    AnalysisRunner,
    extract_result,
    fetch_derivatives_report,
    select_analysts_for_asset,
)
from tradingagents.webui.store import HistoryStore

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

    def propagate(self, ticker, date, asset_type="stock"):
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

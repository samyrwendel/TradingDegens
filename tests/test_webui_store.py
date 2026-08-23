"""History store round-trips records and returns them newest-first."""

from tradingagents.webui.store import HistoryStore


def _rec(run_id, ticker, verdict):
    return {
        "run_id": run_id, "ticker": ticker, "date": "2026-08-22",
        "asset_type": "stock", "status": "done", "verdict": verdict,
        "cost_usd": 0.026, "elapsed": 100.0, "finished_at": "2026-08-22T12:00:00",
        "result": {"bull": "b", "bear": "r", "verdict": verdict},
    }


def test_save_and_get_roundtrip(tmp_path):
    store = HistoryStore(tmp_path)
    store.save(_rec("r1", "AAPL", "Buy"))
    got = store.get("r1")
    assert got["ticker"] == "AAPL"
    assert got["result"]["bear"] == "r"


def test_recent_is_newest_first(tmp_path):
    store = HistoryStore(tmp_path)
    for i in range(3):
        store.save(_rec(f"r{i}", f"T{i}", "Hold"))
    recent = store.recent()
    assert [r["run_id"] for r in recent] == ["r2", "r1", "r0"]
    # summaries only — no heavy result payload in the index
    assert "result" not in recent[0]


def test_recent_limit(tmp_path):
    store = HistoryStore(tmp_path)
    for i in range(10):
        store.save(_rec(f"r{i}", "X", "Sell"))
    assert len(store.recent(limit=4)) == 4


def test_get_unknown_returns_none(tmp_path):
    assert HistoryStore(tmp_path).get("nope") is None


def test_recent_empty_when_no_history(tmp_path):
    assert HistoryStore(tmp_path).recent() == []

"""HTTP routing over a real socket, driving a fake engine (no LLM calls)."""

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.server import make_server
from tradingagents.webui.store import HistoryStore
from tests.test_webui_runner import FINAL_STATE, _blocking_factory, _factory


@pytest.fixture()
def server(tmp_path):
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path)},
        store=HistoryStore(tmp_path),
        graph_factory=_factory(),
    )
    httpd = make_server("127.0.0.1", 0, runner=runner)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=5) as resp:
        return resp.status, json.loads(resp.read())


def _post(base, path, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        base + path, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read())


def test_health(server):
    status, body = _get(server, "/api/health")
    assert status == 200 and body["ok"] is True


def test_config_endpoint_reports_manaus(server):
    status, body = _get(server, "/api/config")
    assert status == 200
    assert body["tz"] == "America/Manaus"
    assert body["now"].endswith("-04:00")
    assert len(body["today"]) == 10  # YYYY-MM-DD


def test_index_served(server):
    with urllib.request.urlopen(server + "/", timeout=5) as resp:
        html = resp.read().decode()
    assert "TradingDegens" in html
    assert resp.headers["Content-Type"].startswith("text/html")


def test_static_asset_served(server):
    with urllib.request.urlopen(server + "/static/app.js", timeout=5) as resp:
        assert resp.status == 200
        assert "renderMarkdown" in resp.read().decode()


def test_analyze_then_status_flow(server):
    status, body = _post(server, "/api/analyze", {"ticker": "AAPL", "date": "2026-08-22"})
    assert status == 200
    run_id = body["run_id"]
    # poll to completion
    for _ in range(150):
        _, snap = _get(server, "/api/status/" + run_id)
        if snap["status"] != "running":
            break
        time.sleep(0.02)
    assert snap["status"] == "done"
    assert snap["result"]["verdict"] == "Buy"
    # history now lists it
    _, hist = _get(server, "/api/history")
    assert any(r["run_id"] == run_id for r in hist["runs"])


def test_analyze_rejects_empty_ticker(server):
    try:
        _post(server, "/api/analyze", {"ticker": ""})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        return
    raise AssertionError("expected HTTP 400")


def test_unknown_run_is_404(server):
    try:
        _get(server, "/api/status/does-not-exist")
    except urllib.error.HTTPError as e:
        assert e.code == 404
        return
    raise AssertionError("expected HTTP 404")


# ------------------------------------------------- timeframe selector (005) -----
def test_chart_endpoint_recomputes_timeframe(server, monkeypatch):
    """GET /api/chart recomputes the chart + plan on the requested frame."""
    import tradingagents.webui.runner as rm
    monkeypatch.setattr(rm, "fetch_price_chart",
                        lambda t, d, tf="1d": {"timeframe": tf, "candles": [{"d": "x"}]})
    monkeypatch.setattr(rm, "fetch_actionable_plan",
                        lambda t, d, tf="1d": {"timeframe": tf, "setup_state": "ativo"})
    status, body = _get(server, "/api/chart?ticker=BTC-USD&date=2026-08-22&tf=4h")
    assert status == 200
    assert body["timeframe"] == "4h"
    assert body["price_chart"]["timeframe"] == "4h"
    assert body["timeframes"] == ["1w", "1d", "4h", "1h", "15m"]


def test_chart_endpoint_rejects_intraday_for_stock(server):
    try:
        _get(server, "/api/chart?ticker=AAPL&date=2026-08-22&tf=15m")
    except urllib.error.HTTPError as e:
        assert e.code == 400
        return
    raise AssertionError("expected HTTP 400")


def test_chart_endpoint_requires_ticker(server):
    try:
        _get(server, "/api/chart?tf=1d")
    except urllib.error.HTTPError as e:
        assert e.code == 400
        return
    raise AssertionError("expected HTTP 400")


# ------------------------------------------------ background runs (task 010) ---
def test_runs_and_history_expose_in_flight(tmp_path):
    """A run still executing is reachable both at /api/runs and merged into
    /api/history as ``running`` — so the UI can show it as em-andamento and re-open
    it without the analysis ever being cancelled."""
    gate = threading.Event()
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path)},
        store=HistoryStore(tmp_path),
        graph_factory=_blocking_factory(gate),
    )
    httpd = make_server("127.0.0.1", 0, runner=runner)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        _, started = _post(base, "/api/analyze", {"ticker": "AAPL", "date": "2026-08-22"})
        run_id = started["run_id"]
        _, runs = _get(base, "/api/runs?status=running")
        assert any(r["run_id"] == run_id and r["status"] == "running" for r in runs["runs"])
        _, hist = _get(base, "/api/history")
        assert any(r["run_id"] == run_id and r["status"] == "running" for r in hist["runs"])
    finally:
        gate.set()
        httpd.shutdown()

"""Owner-gate no DELETE do histórico (task 20260827-004).

O histórico é PÚBLICO pra LEITURA de propósito (track record). Mas APAGAR um ativo do
histórico é só do DONO — senão qualquer visitante destrói o track record. Prova, pela
borda HTTP real: público que tenta DELETE leva 403 owner_only e NADA é apagado; o dono
logado apaga normal; e a LEITURA (/api/history, /api/runs) segue pública.
"""
import json
import os
import threading
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

import pytest

import tradingagents.webui.runner as runner_module
from tradingagents.webui.auth import OwnerAuth
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.server import make_server
from tradingagents.webui.store import HistoryStore

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setattr(runner_module, "fetch_price_chart", lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_actionable_plan", lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_derivatives_report", lambda t, d: "")
    yield
    os.environ.pop("TRADINGDEGENS_OWNER_TOKEN", None)


def _seed(store, ticker):
    """Grava um registro DONE no histórico (o track record que o público pode ler)."""
    store.save({
        "run_id": f"{ticker}-seed-1", "ticker": ticker, "date": "2026-08-22",
        "asset_type": "stock", "status": "done", "error": None, "error_code": None,
        "verdict": "Buy", "verdict_timeframe": "1d", "method": "padrao",
        "cost_usd": 0, "elapsed": 1, "finished_at": "2026-08-22T10:00:00-04:00",
        "result": {"verdict": "Buy"}, "cost": {"usd": 0, "complete": True},
    })


def _serve(tmp_path, token="senha-dono"):
    os.environ["TRADINGDEGENS_OWNER_TOKEN"] = token
    store = HistoryStore(tmp_path)
    _seed(store, "AAPL")
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "openai"},
        store=store)
    httpd = make_server("127.0.0.1", 0, runner=runner, auth=OwnerAuth())
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{port}", runner


def _client():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def _req(opener, base, path, method, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    hdr = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(base + path, data=data, headers=hdr, method=method)
    try:
        with opener.open(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_public_delete_is_refused_and_nothing_removed(tmp_path):
    httpd, base, runner = _serve(tmp_path)
    try:
        op = _client()
        code, body = _req(op, base, "/api/history/AAPL", "DELETE")
        assert code == 403 and body["error_code"] == "owner_only"
        # o track record continua lá — nada foi apagado.
        assert any(s.get("ticker") == "AAPL" for s in runner.store.recent(10))
    finally:
        httpd.shutdown()


def test_public_read_of_history_and_runs_stays_open(tmp_path):
    httpd, base, _runner = _serve(tmp_path)
    try:
        op = _client()  # sem login
        code, hist = _req(op, base, "/api/history", "GET")
        assert code == 200
        assert any(s.get("ticker") == "AAPL" for s in hist["runs"])
        code, runs = _req(op, base, "/api/runs", "GET")
        assert code == 200 and "runs" in runs  # leitura pública intacta
    finally:
        httpd.shutdown()


def test_owner_can_delete(tmp_path):
    httpd, base, runner = _serve(tmp_path, token="abrakadabra")
    try:
        op = _client()
        code, body = _req(op, base, "/api/login", "POST", {"password": "abrakadabra"})
        assert code == 200 and body["owner"] is True
        code, body = _req(op, base, "/api/history/AAPL", "DELETE")
        assert code == 200 and body["ok"] is True
        assert body["removed"] >= 1
        # apagado de verdade — o ativo saiu do histórico.
        assert not any(s.get("ticker") == "AAPL" for s in runner.store.recent(10))
    finally:
        httpd.shutdown()

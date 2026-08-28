"""Owner-gate do "atualizar etapa" (task 002 / DA-062), pela borda HTTP real.

Atualizar uma etapa RE-RODA o pipeline pela credencial do SERVIDOR e reescreve o
resultado publicado. Isso é do dono — um visitante não pode queimar a cota do Samyr
nem mexer no track record. Prova pelo HTTP: público leva 403 e NADA re-roda; dono
com run não retomável leva um 409 honesto; dono com run retomável dispara.
"""
import json
import os
import threading
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

import pytest

import tradingagents.webui.runner as runner_module
from tests.test_webui_runner import FINAL_STATE, _FakeGraph, _wait
from tradingagents.webui.auth import OwnerAuth
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.server import make_server
from tradingagents.webui.store import HistoryStore

pytestmark = pytest.mark.integration

MARKET = "Market Analyst"
TOKEN = "senha-dono"


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setattr(runner_module, "fetch_price_chart",
                        lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_actionable_plan",
                        lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_derivatives_report", lambda t, d: "")
    yield
    os.environ.pop("TRADINGDEGENS_OWNER_TOKEN", None)


def _serve(tmp_path, calls):
    os.environ["TRADINGDEGENS_OWNER_TOKEN"] = TOKEN

    def factory(config, selected_analysts, callbacks):
        calls.append(dict(config))
        return _FakeGraph(callbacks, FINAL_STATE, "Buy")

    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "openai",
                     "data_cache_dir": str(tmp_path)},
        store=HistoryStore(tmp_path), graph_factory=factory)
    httpd = make_server("127.0.0.1", 0, runner=runner, auth=OwnerAuth())
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}", runner


def _put(runner, rid, resumable=True):
    runner.active.put(rid, {
        "run_id": rid, "ticker": "AAPL", "date": "2020-01-02",
        "asset_type": "stock", "timeframe": "1d", "method": "padrao",
        "selected_analysts": ["market", "social", "news", "fundamentals"],
        "started_at": "2026-08-27T10:00:00-04:00", "resumable": resumable,
        "overrides": {"allow_server_key": True} if resumable else {},
    })


def _req(opener, base, path, payload=None):
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(base + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with opener.open(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_public_refresh_is_refused_and_nothing_reruns(tmp_path):
    calls: list = []
    httpd, base, runner = _serve(tmp_path, calls)
    try:
        _put(runner, "r-01")
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        code, body = _req(op, base, "/api/run/r-01/refresh-step", {"node": MARKET})
        assert code == 403 and body["error_code"] == "owner_only"
        assert calls == []                      # nada re-rodou pela cota do dono
    finally:
        httpd.shutdown()


def test_owner_refresh_reruns_the_stage(tmp_path):
    calls: list = []
    httpd, base, runner = _serve(tmp_path, calls)
    try:
        _put(runner, "r-02")
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        assert _req(op, base, "/api/login", {"password": TOKEN})[0] == 200
        code, body = _req(op, base, "/api/run/r-02/refresh-step", {"node": MARKET})
        assert code == 200 and body["ok"] is True and body["node"] == MARKET
        assert _wait(runner, "r-02")["status"] == "done"
        assert len(calls) == 1
    finally:
        httpd.shutdown()


def test_owner_refresh_on_byok_run_is_a_honest_409(tmp_path):
    calls: list = []
    httpd, base, runner = _serve(tmp_path, calls)
    try:
        _put(runner, "byok-01", resumable=False)
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        assert _req(op, base, "/api/login", {"password": TOKEN})[0] == 200
        code, body = _req(op, base, "/api/run/byok-01/refresh-step", {"node": MARKET})
        assert code == 409 and body["error_code"] == "not_resumable"
        assert calls == []
    finally:
        httpd.shutdown()


def test_owner_refresh_of_unknown_run_is_404(tmp_path):
    calls: list = []
    httpd, base, _runner = _serve(tmp_path, calls)
    try:
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        assert _req(op, base, "/api/login", {"password": TOKEN})[0] == 200
        code, _body = _req(op, base, "/api/run/fantasma/refresh-step", {"node": MARKET})
        assert code == 404
    finally:
        httpd.shutdown()

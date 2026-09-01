"""Login do dono destrava a chave do servidor; público usa a própria (BYOK).

Regra (Samyr): público sem login NÃO usa a chave do servidor — precisa da própria
ou não roda. Dono LOGADO (senha em env, verificada server-side) usa a env sem colar
nada. A chave do servidor jamais vai ao cliente; senha nunca em log/resposta.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

import pytest

import tradingagents.webui.runner as runner_module
from tests.test_webui_runner import FINAL_STATE, _FakeGraph
from tradingagents.webui.auth import OwnerAuth
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.server import make_server
from tradingagents.webui.store import HistoryStore


# ------------------------------------------------------------------ helpers ----
@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setattr(runner_module, "fetch_price_chart", lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_actionable_plan", lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_derivatives_report", lambda t, d: "")
    yield
    # os testes HTTP setam a env crua; limpa pra não vazar owner-login pra outros arquivos
    os.environ.pop("TRADINGDEGENS_OWNER_TOKEN", None)


def _base(tmp_path):
    return {"results_dir": str(tmp_path), "llm_provider": "openai",
            "deep_think_llm": "gpt-5.5", "quick_think_llm": "gpt-5.4-mini", "backend_url": None}


def _capturing_factory(captured):
    def make(config, selected, callbacks):
        captured.append(dict(config))
        return _FakeGraph(callbacks, FINAL_STATE, "Buy")
    return make


def _wait(runner, run_id, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = runner.status(run_id)
        if snap and snap["status"] != "running":
            return snap
        time.sleep(0.02)
    raise AssertionError("run did not finish in time")


# --------------------------------------------------------- OwnerAuth (unidade) --
def test_auth_disabled_without_token(monkeypatch):
    monkeypatch.delenv("TRADINGDEGENS_OWNER_TOKEN", raising=False)
    auth = OwnerAuth()
    assert auth.enabled() is False
    assert auth.verify_password("anything") is False


def test_auth_verifies_password(monkeypatch):
    monkeypatch.setenv("TRADINGDEGENS_OWNER_TOKEN", "s3nha-do-dono")
    auth = OwnerAuth()
    assert auth.enabled() is True
    assert auth.verify_password("s3nha-do-dono") is True
    assert auth.verify_password("errada") is False
    assert auth.verify_password(None) is False


def test_auth_session_lifecycle(monkeypatch):
    monkeypatch.setenv("TRADINGDEGENS_OWNER_TOKEN", "x")
    auth = OwnerAuth()
    sid = auth.create_session()
    assert auth.is_valid(sid) is True
    assert auth.is_valid("nope") is False
    assert auth.is_valid(None) is False
    auth.destroy(sid)
    assert auth.is_valid(sid) is False


def test_auth_session_expires(monkeypatch):
    monkeypatch.setenv("TRADINGDEGENS_OWNER_TOKEN", "x")
    auth = OwnerAuth(ttl_seconds=10)
    sid = auth.create_session()
    # empurra o carimbo pro passado além do TTL -> expira e é removida
    auth._sessions[sid] = auth._sessions[sid] - 999_999
    assert auth.is_valid(sid) is False


# --------------------------------------------------- runner gate (defesa) -------
def test_run_refused_without_key_and_without_owner(tmp_path):
    captured = []
    runner = AnalysisRunner(base_config=_base(tmp_path), store=HistoryStore(tmp_path),
                            graph_factory=_capturing_factory(captured))
    # allow_server_key=False = requisição PÚBLICA explícita (o server marca assim)
    snap = _wait(runner, runner.start("AAPL", "2026-08-22",
                                      overrides={"allow_server_key": False}))
    assert snap["status"] == "error"
    assert snap["error_code"] == "need_key"
    assert captured == []  # o grafo NUNCA foi construído (não caiu na env)


def test_run_allowed_for_owner_uses_server_env(tmp_path):
    captured = []
    runner = AnalysisRunner(base_config=_base(tmp_path), store=HistoryStore(tmp_path),
                            graph_factory=_capturing_factory(captured))
    # allow_server_key=True simula a sessão do dono: roda e cai na env (sem llm_api_key)
    snap = _wait(runner, runner.start("AAPL", "2026-08-22",
                                      overrides={"allow_server_key": True}))
    assert snap["status"] == "done"
    assert "llm_api_key" not in captured[0]  # usa a env do servidor, não uma chave injetada


def test_run_allowed_with_user_key_without_owner(tmp_path):
    captured = []
    runner = AnalysisRunner(base_config=_base(tmp_path), store=HistoryStore(tmp_path),
                            graph_factory=_capturing_factory(captured))
    snap = _wait(runner, runner.start("AAPL", "2026-08-22", overrides={"api_key": "sk-U"}))
    assert snap["status"] == "done"
    assert captured[0]["llm_api_key"] == "sk-U"


def test_ask_refused_without_key_and_owner(tmp_path, monkeypatch):
    runner = AnalysisRunner(base_config=_base(tmp_path), store=HistoryStore(tmp_path))
    monkeypatch.setattr(runner, "_load_record", lambda rid: {
        "run_id": "r", "ticker": "AAPL", "status": "done",
        "result": {"verdict": "Buy", "price_structure": {"zones": []}, "verdict_timeframe": "1d"}})
    out = runner.ask("r", "e aí?", overrides={"allow_server_key": False})
    assert out["error_code"] == "need_key"
    assert "answer" not in out


def test_test_key_refused_without_key_and_owner(tmp_path):
    runner = AnalysisRunner(base_config=_base(tmp_path), store=HistoryStore(tmp_path))
    out = runner.test_key({"allow_server_key": False})   # público sem chave própria
    assert out["ok"] is False
    assert out["error_code"] == "need_key"


# ------------------------------------------------------------- HTTP + cookie ----
def _make_server(tmp_path, factory, token="senha-dono"):
    import os
    os.environ["TRADINGDEGENS_OWNER_TOKEN"] = token
    auth = OwnerAuth()  # lê o token acima
    runner = AnalysisRunner(base_config=_base(tmp_path), store=HistoryStore(tmp_path),
                            graph_factory=factory)
    httpd = make_server("127.0.0.1", 0, runner=runner, auth=auth)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{port}"


def _client():
    # opener com cookie jar: guarda o cookie de sessão HttpOnly entre chamadas
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def _post(opener, base, path, payload, headers=None):
    hdr = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode(), headers=hdr)
    try:
        with opener.open(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(opener, base, path):
    try:
        with opener.open(base + path, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _run_to_end(opener, base, rid):
    for _ in range(200):
        _, snap = _get(opener, base, "/api/status/" + rid)
        if snap["status"] != "running":
            return snap
        time.sleep(0.02)
    raise AssertionError("run travou")


def test_http_public_without_key_is_refused(tmp_path):
    captured = []
    httpd, base = _make_server(tmp_path, _capturing_factory(captured))
    try:
        op = _client()
        code, body = _post(op, base, "/api/analyze", {"ticker": "AAPL", "date": "2026-08-22"})
        assert code == 403
        assert body["error_code"] == "need_key"
        assert captured == []       # nenhuma run criada; nunca tocou a env
    finally:
        httpd.shutdown()


def test_http_public_with_own_key_runs(tmp_path):
    captured = []
    httpd, base = _make_server(tmp_path, _capturing_factory(captured))
    try:
        op = _client()
        code, body = _post(op, base, "/api/analyze", {"ticker": "AAPL", "date": "2026-08-22"},
                           headers={"X-LLM-Key": "sk-PUB"})
        assert code == 200
        snap = _run_to_end(op, base, body["run_id"])
        assert snap["status"] == "done"
        assert captured[0]["llm_api_key"] == "sk-PUB"
    finally:
        httpd.shutdown()


def test_http_owner_login_then_run_uses_server_key(tmp_path):
    captured = []
    httpd, base = _make_server(tmp_path, _capturing_factory(captured), token="abrakadabra")
    try:
        op = _client()
        # senha errada não entra
        code, body = _post(op, base, "/api/login", {"password": "nope"})
        assert code == 401 and body["ok"] is False
        # config ainda diz público
        _, cfg = _get(op, base, "/api/config")
        assert cfg["owner"] is False and cfg["owner_login_enabled"] is True
        # login correto -> cookie de sessão
        code, body = _post(op, base, "/api/login", {"password": "abrakadabra"})
        assert code == 200 and body["owner"] is True
        _, cfg = _get(op, base, "/api/config")
        assert cfg["owner"] is True
        # agora roda SEM colar chave -> usa a env do servidor
        code, body = _post(op, base, "/api/analyze", {"ticker": "AAPL", "date": "2026-08-22"})
        assert code == 200
        snap = _run_to_end(op, base, body["run_id"])
        assert snap["status"] == "done"
        assert "llm_api_key" not in captured[0]  # env do servidor, não uma chave do corpo
        # a senha e a chave do servidor nunca aparecem nas respostas
        blob = json.dumps([body, cfg], default=str)
        assert "abrakadabra" not in blob
    finally:
        httpd.shutdown()


def test_http_logout_returns_to_public(tmp_path):
    httpd, base = _make_server(tmp_path, _capturing_factory([]), token="pw")
    try:
        op = _client()
        _post(op, base, "/api/login", {"password": "pw"})
        _, cfg = _get(op, base, "/api/config")
        assert cfg["owner"] is True
        _post(op, base, "/api/logout", {})
        _, cfg = _get(op, base, "/api/config")
        assert cfg["owner"] is False
        # sem sessão e sem chave -> recusado de novo
        code, body = _post(op, base, "/api/analyze", {"ticker": "AAPL", "date": "2026-08-22"})
        assert code == 403 and body["error_code"] == "need_key"
    finally:
        httpd.shutdown()


def test_http_login_cookie_is_httponly(tmp_path):
    httpd, base = _make_server(tmp_path, _capturing_factory([]), token="pw")
    try:
        # inspeciona o header Set-Cookie cru pra confirmar HttpOnly + SameSite
        req = urllib.request.Request(
            base + "/api/login", data=json.dumps({"password": "pw"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            set_cookie = resp.headers.get("Set-Cookie") or ""
        assert "td_session=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=Strict" in set_cookie
    finally:
        httpd.shutdown()

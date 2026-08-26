"""Login da assinatura NO FRONTEND, enviado ao servidor, SÓ pro dono (task 017).

Regra (Samyr): o dono conecta a assinatura pela tela; o token vai por HEADER ao
servidor (nunca querystring/log/resposta), é guardado server-side (0600) e a
capacidade é EXCLUSIVA do dono logado (task 042) — público barrado 403 server-side.
"""

import json
import os
import stat
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
from tradingagents.webui.subscription import SubscriptionStore

pytestmark = pytest.mark.integration


# ---------------------------------------------------------- unit: store --------
def test_store_connect_status_disconnect(tmp_path):
    store = SubscriptionStore(tmp_path / "sub.json")
    assert store.status() == {"connected": False, "kind": None, "connected_at": None}
    st = store.connect("sk-SECRET-123", kind="openai", connected_at="2026-08-26T10:00:00-04:00")
    assert st == {"connected": True, "kind": "openai", "connected_at": "2026-08-26T10:00:00-04:00"}
    assert "sk-SECRET-123" not in json.dumps(st)          # status NUNCA traz o token
    assert store.token() == "sk-SECRET-123"               # server-side lê o valor
    assert store.disconnect()["connected"] is False
    assert store.token() is None


def test_store_file_is_0600_and_holds_token(tmp_path):
    path = tmp_path / "sub.json"
    SubscriptionStore(path).connect("sk-SECRET-xyz")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600   # segredo só pro dono do processo
    assert "sk-SECRET-xyz" in path.read_text()            # persistido server-side


def test_store_rejects_empty_token(tmp_path):
    with pytest.raises(ValueError):
        SubscriptionStore(tmp_path / "sub.json").connect("   ")


def test_store_failopen_on_corrupt_file(tmp_path):
    path = tmp_path / "sub.json"
    path.write_text("{not json")
    store = SubscriptionStore(path)
    assert store.status()["connected"] is False           # corrompido → não conectada
    assert store.token() is None


# ---------------------------------------------------------- HTTP: gating -------
@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setattr(runner_module, "fetch_price_chart", lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_actionable_plan", lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_derivatives_report", lambda t, d: "")
    yield
    os.environ.pop("TRADINGDEGENS_OWNER_TOKEN", None)


def _server(tmp_path, token="senha-dono"):
    os.environ["TRADINGDEGENS_OWNER_TOKEN"] = token
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "openai",
                     "deep_think_llm": "gpt-5.5", "quick_think_llm": "gpt-5.4-mini"},
        store=HistoryStore(tmp_path))
    sub = SubscriptionStore(tmp_path / "sub.json")
    httpd = make_server("127.0.0.1", 0, runner=runner, auth=OwnerAuth(), subscription=sub)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{port}", sub


def _client():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def _call(op, base, method, path, headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers or {})
    try:
        r = op.open(req)
        return r.status, json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or "{}")


TOKEN = "sk-SUBSCRIPTION-TOP-SECRET-42"


def test_public_is_barred_403_on_all_subscription_routes(tmp_path):
    httpd, base, _sub = _server(tmp_path)
    try:
        op = _client()
        for method, path, hdr in [
            ("GET", "/api/subscription/status", None),
            ("POST", "/api/subscription/connect", {"X-Subscription-Token": TOKEN}),
            ("POST", "/api/subscription/disconnect", None),
        ]:
            code, body = _call(op, base, method, path, headers=hdr)
            assert code == 403, (path, code, body)
            assert body.get("error_code") == "owner_only"
    finally:
        httpd.shutdown()


def test_owner_connects_via_header_token_never_leaks(tmp_path):
    httpd, base, sub = _server(tmp_path)
    try:
        op = _client()
        assert _call(op, base, "POST", "/api/login", body={"password": "senha-dono"})[0] == 200
        # antes: não conectada
        assert _call(op, base, "GET", "/api/subscription/status")[1]["connected"] is False
        # conecta pelo HEADER (nunca querystring/corpo)
        code, body = _call(op, base, "POST", "/api/subscription/connect",
                           headers={"X-Subscription-Token": TOKEN}, body={"kind": "openai"})
        assert code == 200 and body["connected"] is True
        # a credencial chegou ao servidor (arquivo escrito) mas NÃO volta ao cliente
        assert sub.token() == TOKEN
        assert TOKEN not in json.dumps(body)
        assert _call(op, base, "GET", "/api/subscription/status")[1]["connected"] is True
        blob = json.dumps([body, _call(op, base, "GET", "/api/subscription/status")[1]])
        assert TOKEN not in blob
        # desconecta
        assert _call(op, base, "POST", "/api/subscription/disconnect")[1]["connected"] is False
        assert sub.token() is None
    finally:
        httpd.shutdown()


def test_connect_without_token_header_is_rejected(tmp_path):
    httpd, base, sub = _server(tmp_path)
    try:
        op = _client()
        _call(op, base, "POST", "/api/login", body={"password": "senha-dono"})
        code, body = _call(op, base, "POST", "/api/subscription/connect", body={"kind": "openai"})
        assert code == 400 and body["ok"] is False
        assert sub.token() is None                        # nada gravado sem token
    finally:
        httpd.shutdown()

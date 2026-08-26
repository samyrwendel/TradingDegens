"""Endpoints OAuth da assinatura (task 019): /oauth/start (só-dono) e /oauth/callback.

Prova, sobre o server HTTP real (stdlib), que:
- público não inicia o fluxo (403 owner_only);
- o dono logado recebe a URL de autorização certa (verifier NUNCA volta);
- o callback valida o ``state``, troca o code (mockado) por token, guarda no store 017
  e faz a ponte pro codex-proxy (arquivo auth.json no formato openai) — sem vazar segredo.
"""

import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

import pytest

import tradingagents.webui.runner as runner_module
from tradingagents.webui import oauth_codex
from tradingagents.webui.auth import OwnerAuth
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.server import make_server
from tradingagents.webui.store import HistoryStore
from tradingagents.webui.subscription import SubscriptionStore


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr(runner_module, "fetch_price_chart", lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_actionable_plan", lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_derivatives_report", lambda t, d: "")
    # Detecção do login do CLI (task 020) → caminhos inexistentes (não ler a box).
    monkeypatch.setenv("TRADINGDEGENS_CODEX_AUTH_FILE", str(tmp_path / "no-codex.json"))
    monkeypatch.setenv("TRADINGDEGENS_CLAUDE_CREDS_FILE", str(tmp_path / "no-claude.json"))
    monkeypatch.setenv("TRADINGDEGENS_GEMINI_DIR", str(tmp_path / "no-gemini"))
    yield
    os.environ.pop("TRADINGDEGENS_OWNER_TOKEN", None)


def _server(tmp_path):
    os.environ["TRADINGDEGENS_OWNER_TOKEN"] = "senha-dono"
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


def _post(op, base, path, payload):
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with op.open(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get_raw(op, base, path):
    try:
        with op.open(base + path, timeout=5) as r:
            return r.status, r.read().decode("utf-8"), r.headers.get_content_type()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8"), "?"


def test_oauth_start_is_owner_only(tmp_path):
    httpd, base, _ = _server(tmp_path)
    try:
        op = _client()
        code, body = _post(op, base, "/api/subscription/oauth/start", {})
        assert code == 403 and body["error_code"] == "owner_only"
    finally:
        httpd.shutdown()


def test_oauth_start_returns_authorize_url_no_secret(tmp_path):
    httpd, base, _ = _server(tmp_path)
    try:
        op = _client()
        _post(op, base, "/api/login", {"password": "senha-dono"})
        code, body = _post(op, base, "/api/subscription/oauth/start", {})
        assert code == 200 and body["ok"] is True
        url = body["authorize_url"]
        assert url.startswith("https://auth.openai.com/oauth/authorize?")
        assert "client_id=app_EMoamEEZ73f0CkXaXp7hrann" in url
        assert "code_challenge_method=S256" in url
        # o verifier (segredo) nunca aparece na resposta
        assert "code_verifier" not in json.dumps(body)
    finally:
        httpd.shutdown()


def test_oauth_callback_closes_the_loop(tmp_path, monkeypatch):
    # mocka a troca do code por token (sem rede) e redireciona a ponte pro tmp
    fake = {"access_token": "AT-live", "refresh_token": "RT-live", "expires_in": 3600,
            "id_token": None}
    monkeypatch.setattr(oauth_codex, "exchange_code",
                        lambda code, verifier, **kw: dict(fake, _seen=(code, verifier)))
    bridge_file = tmp_path / "opencode-auth.json"
    monkeypatch.setenv("TRADINGDEGENS_CODEX_AUTH_FILE", str(bridge_file))

    httpd, base, sub = _server(tmp_path)
    try:
        op = _client()
        _post(op, base, "/api/login", {"password": "senha-dono"})
        _, body = _post(op, base, "/api/subscription/oauth/start", {})
        state = body["state"]

        # simula o retorno do issuer: ?code&state -> callback fecha o token
        status, html, ctype = _get_raw(
            op, base, "/api/subscription/oauth/callback?code=THE-CODE&state=" +
            urllib.parse.quote(state))
        assert status == 200 and ctype == "text/html"
        assert "conectada" in html.lower()

        # store 017 guardou o token (server-side); nunca voltou ao cliente
        assert sub.token() == "AT-live"
        assert "AT-live" not in html  # o token não vaza na página de retorno

        # ponte pro codex-proxy escrita no formato openai
        rec = json.loads(bridge_file.read_text())["openai"]
        assert rec["access"] == "AT-live" and rec["refresh"] == "RT-live"
        assert rec["type"] == "oauth"
        assert oct(bridge_file.stat().st_mode)[-3:] == "600"  # 0600
    finally:
        httpd.shutdown()


def test_oauth_callback_rejects_unknown_state(tmp_path):
    httpd, base, sub = _server(tmp_path)
    try:
        op = _client()
        _post(op, base, "/api/login", {"password": "senha-dono"})
        status, html, _ = _get_raw(
            op, base, "/api/subscription/oauth/callback?code=x&state=forjado")
        assert status == 200
        assert "inválido" in html.lower() or "expirado" in html.lower()
        assert sub.token() is None  # nada foi gravado
    finally:
        httpd.shutdown()

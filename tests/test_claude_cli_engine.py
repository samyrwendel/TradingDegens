"""Motor via assinatura Claude (CLI OAuth) — proxy + provider ``claude-cli`` (task 030).

Prova, sem gastar cota nem exigir rede em CI:

  * o proxy reescreve a auth (descarta ``x-api-key`` do cliente, injeta o Bearer da
    assinatura + o beta OAuth ``oauth-2025-04-20``) e faz streaming do corpo;
  * sem token válido, o proxy responde 503 (gap honesto, não finge);
  * o factory roteia ``claude-cli`` pro proxy — base_url do cliente é IGNORADA (força
    o proxy) e a api_key vira dummy (a auth real é do proxy);
  * o gate owner-only do runner barra ``claude-cli`` de quem não é dono (mesmo com um
    BYOK falso), e libera pro dono (allow_server_key=True);
  * o default OpenAI/Anthropic segue intacto.

Um smoke REAL contra a assinatura roda só com ``RUN_CLAUDE_CLI_LIVE=1`` e credencial
presente (evita consumir a cota — compartilhada com o mainbot — em runs normais).
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

import pytest

from tradingagents.llm_clients import claude_cli_proxy as proxy_mod
from tradingagents.llm_clients.claude_cli_proxy import (
    _merge_beta,
    build_server,
    load_oauth_token,
    proxy_base_url,
)


# ---------------------------------------------------------------- helpers ----
def _write_creds(path, token="sk-ant-oat-test", expires_ms_from_now=3_600_000):
    exp = int(time.time() * 1000) + expires_ms_from_now
    path.write_text(json.dumps({
        "claudeAiOauth": {"accessToken": token, "expiresAt": exp,
                          "scopes": ["user:inference"], "subscriptionType": "max"}
    }))
    return path


class _FakeResp:
    """Resposta canônica do upstream (uma leitura de corpo, depois EOF)."""
    status = 200

    def __init__(self):
        self._sent = False

    def getheaders(self):
        return [("Content-Type", "application/json"), ("x-request-id", "req_test")]

    def read(self, n=-1):
        if self._sent:
            return b""
        self._sent = True
        return b'{"content":[{"type":"text","text":"PONG"}]}'

    def close(self):
        pass


class _FakeConn:
    """Captura o que o proxy MANDARIA pro upstream (sem sair pra rede)."""
    captured: dict = {}

    def request(self, method, path, body=None, headers=None):
        _FakeConn.captured = {
            "method": method, "path": path,
            "headers": {k.lower(): v for k, v in (headers or {}).items()},
            "body": body,
        }

    def getresponse(self):
        return _FakeResp()

    def close(self):
        pass


class _ProxyServer:
    """Sobe o proxy num thread em porta efêmera; fecha no __exit__."""
    def __init__(self):
        self.httpd = build_server("127.0.0.1", 0)
        self.port = self.httpd.server_address[1]

    def __enter__(self):
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._t.start()
        return self

    def url(self, path="/v1/messages"):
        return f"http://127.0.0.1:{self.port}{path}"

    def __exit__(self, *exc):
        self.httpd.shutdown()


# ---------------------------------------------------------------- token ------
def test_load_oauth_token_valid_missing_expired(tmp_path, monkeypatch):
    # válido
    p = _write_creds(tmp_path / "cred.json")
    monkeypatch.setenv("CLAUDE_CLI_CREDENTIALS", str(p))
    tok, err = load_oauth_token()
    assert tok == "sk-ant-oat-test" and err is None

    # expirado → gap honesto
    _write_creds(p, expires_ms_from_now=-1000)
    tok, err = load_oauth_token()
    assert tok is None and "expirado" in err

    # ausente
    monkeypatch.setenv("CLAUDE_CLI_CREDENTIALS", str(tmp_path / "nope.json"))
    tok, err = load_oauth_token()
    assert tok is None and err


def test_merge_beta_dedups_and_preserves_client_betas():
    assert _merge_beta(None) == "oauth-2025-04-20"
    assert _merge_beta("oauth-2025-04-20") == "oauth-2025-04-20"
    merged = _merge_beta("prompt-caching-2024-07-31")
    assert "prompt-caching-2024-07-31" in merged and "oauth-2025-04-20" in merged


# ---------------------------------------------------------------- proxy ------
def test_proxy_rewrites_auth_headers(tmp_path, monkeypatch):
    """O proxy descarta x-api-key do cliente e injeta Bearer da assinatura + beta OAuth,
    preservando betas do cliente e o content-type — sem sair pra rede (upstream fake)."""
    p = _write_creds(tmp_path / "cred.json", token="sk-ant-oat-SECRET")
    monkeypatch.setenv("CLAUDE_CLI_CREDENTIALS", str(p))
    # substitui só a abertura do upstream (não o http.client global que urllib usa)
    monkeypatch.setattr(proxy_mod, "_open_upstream", lambda timeout=600: _FakeConn())

    with _ProxyServer() as srv:
        req = urllib.request.Request(
            srv.url("/v1/messages"),
            data=b'{"model":"claude-haiku-4-5","max_tokens":8,"messages":[]}',
            method="POST",
        )
        req.add_header("x-api-key", "sk-dummy-client")       # deve ser DESCARTADO
        req.add_header("authorization", "Bearer client-junk")  # idem
        req.add_header("anthropic-beta", "prompt-caching-2024-07-31")  # deve ser PRESERVADO
        req.add_header("content-type", "application/json")
        resp = urllib.request.urlopen(req, timeout=10)
        body = resp.read()

    cap = _FakeConn.captured
    hdrs = cap["headers"]
    assert cap["method"] == "POST" and cap["path"] == "/v1/messages"
    assert "x-api-key" not in hdrs                       # chave do cliente descartada
    assert hdrs["authorization"] == "Bearer sk-ant-oat-SECRET"  # Bearer da assinatura
    assert "oauth-2025-04-20" in hdrs["anthropic-beta"]         # beta OAuth injetado
    assert "prompt-caching-2024-07-31" in hdrs["anthropic-beta"]  # beta do cliente mantido
    assert hdrs["anthropic-version"]                    # versão default preenchida
    assert hdrs.get("content-type") == "application/json"
    assert b"PONG" in body                              # corpo do upstream repassado


def test_proxy_503_without_token(tmp_path, monkeypatch):
    """Sem token válido, o proxy responde 503 (gap honesto) e nem toca no upstream."""
    monkeypatch.setenv("CLAUDE_CLI_CREDENTIALS", str(tmp_path / "absent.json"))
    with _ProxyServer() as srv:
        req = urllib.request.Request(srv.url("/v1/messages"), data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=10)
        assert ei.value.code == 503
        payload = json.loads(ei.value.read())
        assert payload["error"]["type"] == "proxy_error"


def test_proxy_healthz_reflects_token(tmp_path, monkeypatch):
    p = _write_creds(tmp_path / "cred.json")
    monkeypatch.setenv("CLAUDE_CLI_CREDENTIALS", str(p))
    with _ProxyServer() as srv:
        with urllib.request.urlopen(srv.url("/healthz"), timeout=10) as r:
            assert r.status == 200
            assert json.loads(r.read())["ok"] is True


# ---------------------------------------------------------------- factory ----
def test_factory_routes_claude_cli_to_proxy_and_forces_base_url():
    """``claude-cli`` roteia pro proxy: base_url do cliente é IGNORADA (força o proxy)
    e a api_key vira dummy (a auth real é injetada pelo proxy)."""
    from tradingagents.llm_clients import create_llm_client

    client = create_llm_client(
        provider="claude-cli",
        model="claude-haiku-4-5",
        base_url="https://api.anthropic.com",  # deve ser IGNORADA
        api_key="sk-should-be-dropped",         # deve ser trocada por dummy
        max_tokens=16,
    )
    assert client.__class__.__name__ == "AnthropicClient"
    assert client.base_url == proxy_base_url()          # forçou o proxy
    assert client.base_url != "https://api.anthropic.com"
    assert client.kwargs.get("api_key") == "claude-cli-oauth"  # dummy, não a do cliente


def test_factory_openai_and_anthropic_unchanged():
    """Regressão: o factory ainda resolve openai (compatível) e anthropic normalmente."""
    from tradingagents.llm_clients import create_llm_client

    an = create_llm_client(provider="anthropic", model="claude-sonnet-5", api_key="k")
    assert an.__class__.__name__ == "AnthropicClient"
    assert an.base_url is None                          # anthropic não força proxy

    oa = create_llm_client(provider="openai", model="gpt-5.5", api_key="k")
    assert oa.__class__.__name__ == "OpenAIClient"


# ---------------------------------------------------------------- owner gate -
def test_owner_only_blocked_helper():
    from tradingagents.webui.runner import _owner_only_blocked

    cc = {"llm_provider": "claude-cli"}
    assert _owner_only_blocked(cc, {"allow_server_key": True}) is False   # dono
    assert _owner_only_blocked(cc, {"allow_server_key": False}) is True   # público
    assert _owner_only_blocked(cc, {}) is True                            # sem flag = barra
    assert _owner_only_blocked(cc, {"allow_server_key": True, "api_key": "x"}) is False
    # provedor normal nunca é owner-only
    assert _owner_only_blocked({"llm_provider": "openai"}, {"allow_server_key": False}) is False


def _make_runner(tmp_path):
    from tradingagents.webui.runner import AnalysisRunner
    from tradingagents.webui.store import HistoryStore

    def _factory(config, selected_analysts, callbacks):
        class _G:
            def propagate(self, ticker, date, asset_type="stock", timeframe="1d"):
                return ({"final_trade_decision": "Final: Buy.",
                         "market_report": "ok"}, "Buy")
        return _G()

    return AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "openai"},
        store=HistoryStore(tmp_path), graph_factory=_factory,
    )


def _wait(runner, run_id, timeout=4.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = runner.status(run_id)
        if snap and snap["status"] != "running":
            return snap
        time.sleep(0.02)
    raise AssertionError("run não terminou a tempo")


def test_runner_gate_blocks_public_claude_cli(tmp_path):
    """Público (allow_server_key=False) pedindo claude-cli é barrado owner_only — mesmo
    mandando um BYOK falso (a assinatura é do dono, não se destrava com chave qualquer)."""
    runner = _make_runner(tmp_path)
    rid = runner.start("AAPL", "2026-08-22", overrides={
        "provider": "claude-cli", "allow_server_key": False, "api_key": "sk-fake-byok",
    })
    snap = _wait(runner, rid)
    assert snap["status"] == "error"
    assert snap.get("error_code") == "owner_only"


def test_runner_gate_allows_owner_claude_cli(tmp_path):
    """Dono (allow_server_key=True) passa o gate e a run conclui (grafo fake, sem rede)."""
    runner = _make_runner(tmp_path)
    rid = runner.start("AAPL", "2026-08-22", overrides={
        "provider": "claude-cli", "allow_server_key": True,
    })
    snap = _wait(runner, rid)
    assert snap["status"] == "done", snap.get("error")


# ---------------------------------------------------------------- live smoke -
_LIVE = os.environ.get("RUN_CLAUDE_CLI_LIVE") == "1" and load_oauth_token()[0] is not None


@pytest.mark.skipif(not _LIVE, reason="RUN_CLAUDE_CLI_LIVE!=1 ou sem credencial da assinatura")
def test_live_subscription_roundtrip():
    """Ponta a ponta REAL: o factory claude-cli → proxy → assinatura responde, $0/token
    (service_tier=standard). Uma única chamada barata (Haiku) pra poupar a cota."""
    import anthropic

    from tradingagents.llm_clients import create_llm_client

    with _ProxyServer() as srv:
        os.environ["CLAUDE_CLI_PROXY_URL"] = f"http://127.0.0.1:{srv.port}"
        try:
            client = create_llm_client(provider="claude-cli",
                                       model="claude-haiku-4-5", max_tokens=16)
            llm = client.get_llm()
            out = llm.invoke("Reply with exactly: PONG")
            assert "PONG" in (out.content or "")
            # auth por Bearer (assinatura), nunca x-api-key paga
            c = anthropic.Anthropic(api_key="dummy", base_url=f"http://127.0.0.1:{srv.port}")
            m = c.messages.create(model="claude-haiku-4-5", max_tokens=8,
                                  messages=[{"role": "user", "content": "say PONG"}])
            assert m.usage.output_tokens > 0
        finally:
            os.environ.pop("CLAUDE_CLI_PROXY_URL", None)

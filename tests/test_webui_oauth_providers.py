"""Registro OAuth por-provedor da assinatura (task 020 — generaliza a 019).

Cobre o módulo PURO (:mod:`tradingagents.webui.oauth_providers`): as URLs de
autorização VERBATIM dos três CLIs (openai/anthropic/google), a troca de code por
token (com/sem client_secret), o segredo (verifier/client_secret) nunca na URL, e o
cofre de fluxos pendentes que carrega o provider.
"""

import json
import urllib.parse

import pytest

from tradingagents.webui import oauth_providers as op


# --------------------------------------------------------- authorize URLs ------
def _q(url):
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)


def test_openai_matches_codex_shape():
    """openai delega à 019 — mesma URL do ``codex login`` (byte-a-byte)."""
    url = op.get("openai").build_authorize_url(state="ST8", code_challenge="CH9")
    p = urllib.parse.urlparse(url)
    q = _q(url)
    assert p.netloc == "auth.openai.com" and p.path == "/oauth/authorize"
    assert q["client_id"] == ["app_EMoamEEZ73f0CkXaXp7hrann"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["codex_cli_simplified_flow"] == ["true"]
    assert q["originator"] == ["codex_cli_rs"]


def test_anthropic_authorize_url_verbatim():
    url = op.get("anthropic").build_authorize_url(state="S", code_challenge="C")
    p = urllib.parse.urlparse(url)
    q = _q(url)
    assert p.netloc == "claude.ai" and p.path == "/oauth/authorize"
    assert q["client_id"] == ["9d1c250a-e61b-44d9-88ed-5944d1962f5e"]
    assert q["scope"] == ["org:create_api_key user:profile user:inference"]
    assert q["code_challenge"] == ["C"] and q["code_challenge_method"] == ["S256"]
    assert q["redirect_uri"] == ["https://console.anthropic.com/oauth/code/callback"]


def test_google_authorize_url_verbatim_and_no_secret_leak():
    prov = op.get("google")
    url = prov.build_authorize_url(state="S", code_challenge="C")
    p = urllib.parse.urlparse(url)
    q = _q(url)
    assert p.netloc == "accounts.google.com" and p.path == "/o/oauth2/v2/auth"
    assert q["client_id"] == ["681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com"]
    assert "https://www.googleapis.com/auth/cloud-platform" in q["scope"][0]
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    # o client_secret (installed app, resolvido em runtime) JAMAIS entra na URL
    secret = prov.resolve_client_secret()
    if secret:
        assert secret not in url


def test_verifier_never_in_any_authorize_url():
    v = op.new_verifier()
    ch = op.challenge_for(v)
    for key in op.PROVIDER_ORDER:
        url = op.get(key).build_authorize_url(state=op.new_state(), code_challenge=ch)
        assert v not in url            # segredo fica server-side
        assert "code_verifier" not in _q(url)


def test_custom_redirect_is_honored():
    url = op.get("anthropic").build_authorize_url(
        state="s", code_challenge="c", redirect_uri="http://127.0.0.1:9/cb")
    assert _q(url)["redirect_uri"] == ["http://127.0.0.1:9/cb"]


def test_get_unknown_provider_raises():
    with pytest.raises(ValueError):
        op.get("groky")


# ----------------------------------------------------------- exchange_code -----
def test_exchange_includes_client_secret_for_google(monkeypatch):
    # o secret é resolvido em RUNTIME (fora do repo); aqui injetamos por env.
    monkeypatch.setenv("TRADINGDEGENS_GEMINI_CLIENT_SECRET", "SECRET-DE-TESTE")
    seen = {}

    def fake(url, body, headers):
        seen["url"] = url
        seen["form"] = urllib.parse.parse_qs(body.decode())
        return json.dumps({"access_token": "AT"}).encode()

    out = op.get("google").exchange_code("CODE", "VER", opener=fake)
    assert out["access_token"] == "AT"
    assert seen["url"] == "https://oauth2.googleapis.com/token"
    f = seen["form"]
    assert f["grant_type"] == ["authorization_code"]
    assert f["code"] == ["CODE"] and f["code_verifier"] == ["VER"]
    # installed app: o secret (resolvido do env) entra SÓ na troca de token (não na URL)
    assert f["client_secret"] == ["SECRET-DE-TESTE"]


def test_google_client_secret_never_in_source_or_url():
    """Guardrail anti-secret-scanning: o literal do secret não fica versionado, e o
    secret (venha de onde vier) nunca entra na URL de autorização."""
    prov = op.get("google")
    assert prov.client_secret is None            # nada estático no registro
    url = prov.build_authorize_url(state="s", code_challenge="c")
    resolved = prov.resolve_client_secret()      # runtime (env/CLI da box) ou None
    if resolved:
        assert resolved not in url               # jamais na URL, resolvido ou não


def test_exchange_omits_client_secret_for_anthropic():
    seen = {}

    def fake(url, body, headers):
        seen["form"] = urllib.parse.parse_qs(body.decode())
        return json.dumps({"access_token": "AT"}).encode()

    op.get("anthropic").exchange_code("C", "V", opener=fake)
    assert "client_secret" not in seen["form"]         # public client, PKCE puro


def test_exchange_rejects_response_without_access_token():
    with pytest.raises(ValueError):
        op.get("anthropic").exchange_code(
            "c", "v", opener=lambda u, b, h: json.dumps({"error": "bad"}).encode())


# ----------------------------------------------------------- PendingOAuth ------
def test_pending_oauth_carries_provider_single_use():
    pf = op.PendingOAuth()
    pf.create("st", "verifier-secret", "anthropic")
    assert pf.take("st") == ("verifier-secret", "anthropic")
    assert pf.take("st") is None            # uso único
    assert pf.take("desconhecido") is None
    assert pf.take(None) is None


def test_pending_oauth_expires():
    pf = op.PendingOAuth(ttl_seconds=-1)
    pf.create("st", "v", "google")
    assert pf.take("st") is None

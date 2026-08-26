"""OAuth ChatGPT/OpenAI da assinatura — conectar por LINK (task 019, refina a 017).

Cobre o módulo PURO (:mod:`tradingagents.webui.oauth_codex`): PKCE S256, a URL de
autorização verbatim do ``codex login``, a troca do code por token, a ponte pro
codex-proxy e o cofre de fluxos pendentes. Nenhum segredo em texto de erro/log.
"""

import base64
import hashlib
import json
import urllib.parse

import pytest

from tradingagents.webui import oauth_codex as oc


# --------------------------------------------------------------- PKCE / URL ----
def test_pkce_challenge_is_s256_of_verifier():
    v = oc.new_verifier()
    expected = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")
    assert oc.challenge_for(v) == expected
    # base64url sem padding (nada de '=' / '+' / '/')
    assert "=" not in v and "+" not in v and "/" not in v


def test_verifier_and_state_are_unique_and_long():
    assert oc.new_verifier() != oc.new_verifier()
    assert oc.new_state() != oc.new_state()
    assert len(oc.new_verifier()) >= 43  # mínimo do PKCE


def test_authorize_url_matches_codex_shape():
    url = oc.build_authorize_url(state="ST8", code_challenge="CH9")
    p = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qs(p.query)
    assert p.scheme == "https" and p.netloc == "auth.openai.com" and p.path == "/oauth/authorize"
    assert q["response_type"] == ["code"]
    assert q["client_id"] == ["app_EMoamEEZ73f0CkXaXp7hrann"]
    assert q["redirect_uri"] == ["http://localhost:1455/auth/callback"]
    assert q["scope"] == ["openid profile email offline_access api.connectors.read api.connectors.invoke"]
    assert q["code_challenge"] == ["CH9"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["id_token_add_organizations"] == ["true"]
    assert q["codex_cli_simplified_flow"] == ["true"]
    assert q["state"] == ["ST8"]
    assert q["originator"] == ["codex_cli_rs"]
    # o verifier (segredo) NUNCA entra na URL — só o challenge público
    assert "code_verifier" not in q


def test_authorize_url_honors_custom_redirect():
    url = oc.build_authorize_url(state="s", code_challenge="c",
                                 redirect_uri="http://127.0.0.1:8080/api/subscription/oauth/callback")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["redirect_uri"] == ["http://127.0.0.1:8080/api/subscription/oauth/callback"]


# ------------------------------------------------------------- exchange_code ---
def test_exchange_code_posts_correct_form_and_parses():
    seen = {}

    def fake_opener(url, body, headers):
        seen["url"] = url
        seen["headers"] = headers
        seen["form"] = urllib.parse.parse_qs(body.decode())
        return json.dumps({"access_token": "at-1", "refresh_token": "rt-1",
                           "expires_in": 3600, "id_token": "x"}).encode()

    out = oc.exchange_code("CODE7", "VER8", redirect_uri="http://localhost:1455/auth/callback",
                           opener=fake_opener)
    assert out["access_token"] == "at-1"
    assert seen["url"] == "https://auth.openai.com/oauth/token"
    assert seen["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    f = seen["form"]
    assert f["grant_type"] == ["authorization_code"]
    assert f["code"] == ["CODE7"]
    assert f["code_verifier"] == ["VER8"]
    assert f["client_id"] == ["app_EMoamEEZ73f0CkXaXp7hrann"]
    assert f["redirect_uri"] == ["http://localhost:1455/auth/callback"]


def test_exchange_code_rejects_response_without_access_token():
    with pytest.raises(ValueError):
        oc.exchange_code("c", "v", opener=lambda u, b, h: json.dumps({"error": "bad"}).encode())


# ------------------------------------------------------ id_token / bridge ------
def _jwt(payload: dict) -> str:
    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{seg({'alg': 'none'})}.{seg(payload)}.sig"


def test_account_id_from_nested_claim():
    tok = _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acc-123"}})
    assert oc.account_id_from_id_token(tok) == "acc-123"


def test_account_id_falls_back_and_tolerates_garbage():
    assert oc.account_id_from_id_token(_jwt({"account_id": "top-9"})) == "top-9"
    assert oc.account_id_from_id_token("not-a-jwt") is None
    assert oc.account_id_from_id_token(None) is None


def test_bridge_record_shapes_codex_proxy_format():
    resp = {"access_token": "AAA", "refresh_token": "RRR", "expires_in": 100,
            "id_token": _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acc-x"}})}
    rec = oc.bridge_record(resp, now_ms=1_000_000)
    assert rec == {"type": "oauth", "refresh": "RRR", "access": "AAA",
                   "expires": 1_000_000 + 100_000, "accountId": "acc-x"}


def test_bridge_record_preserves_previous_when_missing():
    resp = {"access_token": "NEW"}  # sem refresh/id_token
    rec = oc.bridge_record(resp, now_ms=0,
                           previous={"refresh": "OLD-R", "accountId": "OLD-A"})
    assert rec["refresh"] == "OLD-R"
    assert rec["accountId"] == "OLD-A"
    assert rec["access"] == "NEW"


# -------------------------------------------------------------- PendingFlows ---
def test_pending_flows_single_use():
    pf = oc.PendingFlows()
    pf.create("st", "verifier-secret")
    assert pf.take("st") == "verifier-secret"
    assert pf.take("st") is None        # uso único (já consumido)
    assert pf.take("desconhecido") is None
    assert pf.take(None) is None


def test_pending_flows_expires():
    pf = oc.PendingFlows(ttl_seconds=-1)   # qualquer idade já estourou o TTL
    pf.create("st", "v")
    assert pf.take("st") is None

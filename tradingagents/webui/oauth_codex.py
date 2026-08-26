"""OAuth ChatGPT/OpenAI (assinatura do dono) — conectar por LINK, não colar token (task 019).

Refinamento da 017. A "assinatura" é o ChatGPT via Codex; o login é OAuth **PKCE S256**
contra ``auth.openai.com`` com o client público do Codex CLI. As constantes aqui foram
conferidas EXERCITANDO o ``codex login`` (0.141.0) e lendo a URL de autorização que ele
emite — são verbatim, não chutadas:

    https://auth.openai.com/oauth/authorize?response_type=code
      &client_id=app_EMoamEEZ73f0CkXaXp7hrann
      &redirect_uri=http://localhost:1455/auth/callback
      &scope=openid profile email offline_access api.connectors.read api.connectors.invoke
      &code_challenge=<S256>&code_challenge_method=S256
      &id_token_add_organizations=true&codex_cli_simplified_flow=true
      &state=<nonce>&originator=codex_cli_rs

Este módulo é **puro e testável**: monta a URL de autorização e troca o ``code`` por
token. NUNCA loga segredo. O ``code_verifier`` (PKCE) é SEGREDO e fica server-side
(:class:`PendingFlows`), nunca vai ao cliente.

Ponte com o consumo real: os tokens frescos alimentam o ``codex-proxy`` (:4001), que
lê ``~/.local/share/opencode/auth.json`` (chave ``openai``) e serve o ChatGPT backend;
o litellm (:4000) roteia o ``gpt-5.3-codex`` por ali. Por isso :func:`bridge_record`
devolve o registro no formato que o codex-proxy espera — é o elo que faz o modelo do
dono voltar a responder depois de conectar.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Callable

# Client público do Codex CLI (mesmo id usado pelo codex-proxy no refresh).
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{ISSUER}/oauth/authorize"
TOKEN_URL = f"{ISSUER}/oauth/token"
# redirect_uri REGISTRADO desse client público: loopback fixo :1455 (o codex login
# sobe um servidor local nessa porta pra capturar o code). É o único aceito pela
# OpenAI aqui — daí o "gap" do fluxo 100% remoto (ver server + relatório da 019).
DEFAULT_REDIRECT = "http://localhost:1455/auth/callback"
SCOPE = "openid profile email offline_access api.connectors.read api.connectors.invoke"
ORIGINATOR = "codex_cli_rs"


def _b64url(raw: bytes) -> str:
    """base64url SEM padding (formato do code_verifier/challenge/state PKCE)."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def new_verifier() -> str:
    """``code_verifier`` PKCE (base64url de 64 bytes → 86 chars). SEGREDO server-side."""
    return _b64url(secrets.token_bytes(64))


def challenge_for(verifier: str) -> str:
    """``code_challenge`` = base64url(sha256(verifier)) — método S256."""
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def new_state() -> str:
    """Nonce ``state`` anti-CSRF (base64url de 32 bytes)."""
    return _b64url(secrets.token_bytes(32))


def build_authorize_url(*, state: str, code_challenge: str,
                        redirect_uri: str = DEFAULT_REDIRECT) -> str:
    """URL de autorização idêntica à do ``codex login`` (parâmetros verbatim).

    Encoda com :func:`urllib.parse.quote` (espaço → ``%20``; ``:`` e ``/`` do
    redirect → ``%3A``/``%2F``), como o codex. Nenhum segredo entra aqui — só o
    ``code_challenge`` público (o ``verifier`` fica no servidor)."""
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "state": state,
        "originator": ORIGINATOR,
    }
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


def _http_post(url: str, body: bytes, headers: dict[str, str]) -> bytes:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (host fixo do issuer)
        return resp.read()


def exchange_code(code: str, verifier: str, *, redirect_uri: str = DEFAULT_REDIRECT,
                  opener: Callable[[str, bytes, dict[str, str]], bytes] | None = None
                  ) -> dict[str, Any]:
    """Troca o ``code`` de autorização por tokens (grant_type=authorization_code, PKCE).

    Devolve o JSON da OpenAI (``access_token``/``refresh_token``/``expires_in``/
    ``id_token``). Corpo é ``application/x-www-form-urlencoded`` (como o codex CLI faz
    no exchange). ``opener`` é injetável pra teste; o default faz o POST real.
    Levanta ``ValueError`` se a resposta não trouxer ``access_token``."""
    form = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "client_id": CLIENT_ID,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
    }).encode("ascii")
    headers = {"Content-Type": "application/x-www-form-urlencoded",
               "Accept": "application/json"}
    raw = (opener or _http_post)(TOKEN_URL, form, headers)
    data = json.loads(raw)
    if not isinstance(data, dict) or not data.get("access_token"):
        raise ValueError("token exchange sem access_token")
    return data


def account_id_from_id_token(id_token: str | None) -> str | None:
    """Extrai o ``chatgpt_account_id`` do ``id_token`` (JWT), best-effort.

    Lê a claim aninhada ``https://api.openai.com/auth`` (onde o backend do ChatGPT
    guarda o account id); fallback pra ``account_id`` no topo. Nunca valida assinatura
    (só decodifica o payload) — o token já veio direto do issuer no exchange."""
    if not id_token or id_token.count(".") < 2:
        return None
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # repõe o padding base64url
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:  # noqa: BLE001 — JWT malformado: sem account id, segue
        return None
    auth = claims.get("https://api.openai.com/auth") or {}
    if isinstance(auth, dict) and auth.get("chatgpt_account_id"):
        return auth["chatgpt_account_id"]
    return claims.get("account_id") or None


def bridge_record(token_resp: dict[str, Any], *, now_ms: int,
                  previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Registro no formato que o ``codex-proxy`` lê (auth.json → chave ``openai``):
    ``{type, refresh, access, expires(ms), accountId}``.

    Preserva ``refresh``/``accountId`` anteriores quando o novo exchange não os traz
    (o refresh grant, por ex., às vezes não reemite o refresh_token). É o elo que faz
    o ``gpt-5.3-codex`` responder de novo — sem ele, só o store 017 guarda o token."""
    prev = previous or {}
    account = account_id_from_id_token(token_resp.get("id_token")) or prev.get("accountId")
    expires_in = int(token_resp.get("expires_in") or 0)
    return {
        "type": "oauth",
        "refresh": token_resp.get("refresh_token") or prev.get("refresh"),
        "access": token_resp["access_token"],
        "expires": now_ms + expires_in * 1000,
        "accountId": account,
    }


class PendingFlows:
    """Fluxos OAuth em andamento (``state`` → ``code_verifier``), owner-side, em memória.

    O ``verifier`` é SEGREDO e nunca sai daqui pro cliente; o ``state`` é o único
    identificador que trafega. Uso único (``take`` remove) + expiração por TTL —
    protege o callback contra replay/CSRF sem persistir nada em disco."""

    def __init__(self, ttl_seconds: int = 600):
        self._flows: dict[str, tuple[str, float]] = {}
        self._ttl = ttl_seconds
        self._lock = threading.Lock()

    def create(self, state: str, verifier: str) -> None:
        with self._lock:
            self._flows[state] = (verifier, time.monotonic())

    def take(self, state: str | None) -> str | None:
        """Consome (uso único) o verifier do ``state``; None se ausente/expirado."""
        if not state:
            return None
        with self._lock:
            item = self._flows.pop(state, None)
        if not item:
            return None
        verifier, born = item
        if time.monotonic() - born > self._ttl:
            return None
        return verifier

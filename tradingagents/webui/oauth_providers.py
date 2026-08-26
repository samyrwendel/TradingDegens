"""Registro OAuth por-provedor da assinatura do dono (task 020 — generaliza a 019).

A 019 conectou o ChatGPT/OpenAI por LINK (PKCE S256) usando o client público do
Codex CLI. Esta é a generalização: um :class:`Provider` por assinatura suportada
(``openai``/``anthropic``/``google``), cada um com seu ``client_id``, URL de
autorização, endpoint de token, escopo e ``redirect_uri`` — todos **verbatim** dos
CLIs oficiais instalados na box (extraídos por leitura, não chutados):

- **openai** (ChatGPT via Codex CLI 0.141): delega ao :mod:`oauth_codex` da 019
  (constantes conferidas exercitando ``codex login``).
- **anthropic** (Claude via claude-code): client ``9d1c250a-…``, authorize em
  ``claude.ai/oauth/authorize``, token em ``console.anthropic.com/v1/oauth/token``,
  escopo ``org:create_api_key user:profile user:inference`` — lidos do binário do
  ``claude`` CLI instalado.
- **google** (Gemini via gemini-cli 0.29 / Code Assist): client
  ``681255809395-…apps.googleusercontent.com``, authorize em
  ``accounts.google.com/o/oauth2/v2/auth`` — lidos de ``code_assist/oauth2.js``.
  O ``client_secret`` do gemini-cli é público por design (installed app), mas NÃO
  fica versionado (secret scanning) — é resolvido em runtime (env/CLI da box) só na
  troca de token; ver :func:`_resolve_google_client_secret`.

O módulo é **puro e testável**: monta a URL de autorização (só o ``code_challenge``
público entra; o ``verifier`` é SEGREDO server-side) e troca o ``code`` por token.
NUNCA loga segredo.

Gap do loopback (herdado da 019): os três providers usam ``redirect_uri`` que cai na
máquina do BROWSER (loopback :PORT) ou numa página de "cole o código" — nenhum volta
ao NOSSO servidor. Por isso a conexão de fato do dono vem da DETECÇÃO do login do CLI
da box (:mod:`server_login`), não do round-trip; o botão OAuth fica pra reconectar.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from tradingagents.webui import oauth_codex

# Helpers PKCE genéricos (reuso do módulo da 019 — mesma semântica pros 3 providers).
new_verifier = oauth_codex.new_verifier
challenge_for = oauth_codex.challenge_for
new_state = oauth_codex.new_state


@dataclass(frozen=True)
class Provider:
    """Um provedor de assinatura OAuth (client_id + URLs + escopo, verbatim do CLI).

    ``extra_params`` são parâmetros de autorização específicos do provedor (ex.: os
    ``id_token_add_organizations``/``codex_cli_simplified_flow`` do codex, ou o
    ``access_type=offline`` do Google). ``client_secret`` só existe pra installed
    apps públicos (Google) e entra apenas na troca de token, nunca na URL. Os
    hooks ``_build`` / ``_exchange`` deixam o openai delegar 1:1 ao :mod:`oauth_codex`
    (mantém o fluxo da 019 byte-a-byte)."""

    key: str
    label: str
    cta: str
    client_id: str
    authorize_url: str
    token_url: str
    scope: str
    default_redirect: str
    extra_params: dict[str, str] = field(default_factory=dict)
    client_secret: str | None = None
    # resolver LAZY do client_secret (installed apps): mantém o segredo FORA do repo
    # (o secret scanning do GitHub barra o literal do secret Google), resolvido em runtime.
    secret_resolver: Callable[[], str | None] | None = None
    _build: Callable[..., str] | None = None
    _exchange: Callable[..., dict[str, Any]] | None = None

    def resolve_client_secret(self) -> str | None:
        """client_secret efetivo: o estático, senão o resolver lazy (env/CLI da box)."""
        if self.client_secret:
            return self.client_secret
        return self.secret_resolver() if self.secret_resolver else None

    # -- authorize -----------------------------------------------------------
    def build_authorize_url(self, *, state: str, code_challenge: str,
                            redirect_uri: str | None = None) -> str:
        """URL de autorização do provedor (PKCE S256). Só o ``code_challenge``
        público entra; o ``verifier`` fica server-side."""
        redirect = redirect_uri or self.default_redirect
        if self._build is not None:
            return self._build(state=state, code_challenge=code_challenge,
                               redirect_uri=redirect)
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect,
            "scope": self.scope,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
            **self.extra_params,
        }
        return self.authorize_url + "?" + urllib.parse.urlencode(
            params, quote_via=urllib.parse.quote)

    # -- token exchange ------------------------------------------------------
    def exchange_code(self, code: str, verifier: str, *,
                      redirect_uri: str | None = None,
                      opener: Callable[[str, bytes, dict[str, str]], bytes] | None = None
                      ) -> dict[str, Any]:
        """Troca o ``code`` por token (grant_type=authorization_code, PKCE).

        ``opener`` é injetável pra teste. Levanta ``ValueError`` sem ``access_token``.
        (Na prática o round-trip só fecha pro provider cujo redirect volta ao servidor;
        os demais dependem da detecção — ver módulo.)"""
        redirect = redirect_uri or self.default_redirect
        if self._exchange is not None:
            return self._exchange(code, verifier, redirect_uri=redirect, opener=opener)
        form: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client_id,
            "code_verifier": verifier,
            "redirect_uri": redirect,
        }
        secret = self.resolve_client_secret()
        if secret:
            form["client_secret"] = secret
        body = urllib.parse.urlencode(form).encode("ascii")
        headers = {"Content-Type": "application/x-www-form-urlencoded",
                   "Accept": "application/json"}
        raw = (opener or _http_post)(self.token_url, body, headers)
        data = json.loads(raw)
        if not isinstance(data, dict) or not data.get("access_token"):
            raise ValueError("token exchange sem access_token")
        return data


def _http_post(url: str, body: bytes, headers: dict[str, str]) -> bytes:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (host fixo do issuer)
        return resp.read()


def _resolve_google_client_secret() -> str | None:
    """client_secret do gemini-cli (installed app) — resolvido em RUNTIME, nunca no repo.

    O gemini-cli embute um ``client_secret`` que, por ser um *installed application*, é
    público por design (docs do Google). Ainda assim NÃO fica versionado aqui: o secret
    scanning do GitHub barra o padrão do secret Google. Resolve por ordem: env
    ``TRADINGDEGENS_GEMINI_CLIENT_SECRET`` → leitura read-only do ``oauth2.js`` do
    gemini-cli instalado na box. Na prática só importaria se o round-trip fechasse
    (não fecha — loopback; a conexão real vem da detecção), então None é aceitável."""
    env = (os.getenv("TRADINGDEGENS_GEMINI_CLIENT_SECRET") or "").strip()
    if env:
        return env
    exe = shutil.which("gemini") or os.path.expanduser("~/.npm-global/bin/gemini")
    try:
        real = os.path.realpath(exe)
        pkg_root = os.path.dirname(os.path.dirname(real))  # …/@google/gemini-cli
        candidate = os.path.join(
            pkg_root, "node_modules", "@google", "gemini-cli-core",
            "dist", "src", "code_assist", "oauth2.js")
        for path in (candidate,):
            if os.path.isfile(path):
                with open(path, encoding="utf-8", errors="ignore") as fh:
                    m = re.search(r"OAUTH_CLIENT_SECRET\s*=\s*'([^']+)'", fh.read())
                    if m:
                        return m.group(1)
    except OSError:
        pass
    return None


# --- Registro (constantes verbatim dos CLIs da box) ---------------------------
PROVIDERS: dict[str, Provider] = {
    # ChatGPT/OpenAI — delega à 019 (constantes do codex login, byte-a-byte).
    "openai": Provider(
        key="openai",
        label="ChatGPT",
        cta="Conectar com ChatGPT",
        client_id=oauth_codex.CLIENT_ID,
        authorize_url=oauth_codex.AUTHORIZE_URL,
        token_url=oauth_codex.TOKEN_URL,
        scope=oauth_codex.SCOPE,
        default_redirect=oauth_codex.DEFAULT_REDIRECT,
        # late-bound via o módulo (não a ref capturada) — assim o build/exchange do
        # openai segue a 019 byte-a-byte E honra monkeypatch nos testes.
        _build=lambda *, state, code_challenge, redirect_uri: oauth_codex.build_authorize_url(
            state=state, code_challenge=code_challenge, redirect_uri=redirect_uri),
        _exchange=lambda code, verifier, *, redirect_uri, opener=None: oauth_codex.exchange_code(
            code, verifier, redirect_uri=redirect_uri, opener=opener),
    ),
    # Claude/Anthropic — claude-code CLI (client + escopo lidos do binário).
    "anthropic": Provider(
        key="anthropic",
        label="Claude",
        cta="Conectar com Claude",
        client_id="9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        authorize_url="https://claude.ai/oauth/authorize",
        token_url="https://console.anthropic.com/v1/oauth/token",
        scope="org:create_api_key user:profile user:inference",
        # fluxo "cole o código": o issuer mostra o code numa página do console
        # (não volta ao nosso server) — a conexão real vem da detecção do CLI.
        default_redirect="https://console.anthropic.com/oauth/code/callback",
        extra_params={"code": "true"},
    ),
    # Gemini/Google — gemini-cli 0.29 / Code Assist (installed app, secret público).
    "google": Provider(
        key="google",
        label="Gemini",
        cta="Conectar com Google",
        client_id="681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scope=("https://www.googleapis.com/auth/cloud-platform "
               "https://www.googleapis.com/auth/userinfo.email "
               "https://www.googleapis.com/auth/userinfo.profile"),
        # loopback dinâmico do gemini-cli: cai na máquina do browser, não no server.
        default_redirect="http://localhost:8123/oauth2callback",
        extra_params={"access_type": "offline", "prompt": "consent"},
        # secret público do installed app — resolvido em runtime, FORA do repo.
        secret_resolver=_resolve_google_client_secret,
    ),
}

PROVIDER_ORDER = ("openai", "anthropic", "google")


def get(provider: str | None) -> Provider:
    """Provider pelo key; default ``openai``. ``ValueError`` se desconhecido."""
    key = (provider or "openai").strip().lower()
    if key not in PROVIDERS:
        raise ValueError(f"provedor desconhecido: {key!r}")
    return PROVIDERS[key]


class PendingOAuth:
    """Fluxos OAuth em andamento (``state`` → ``(verifier, provider)``), owner-side.

    Igual ao :class:`oauth_codex.PendingFlows`, mas carrega também o PROVIDER do
    fluxo — o callback precisa saber qual assinatura fechar. O ``verifier`` é SEGREDO
    e nunca sai daqui; só o ``state`` trafega. Uso único (``take`` remove) + TTL."""

    def __init__(self, ttl_seconds: int = 600):
        self._flows: dict[str, tuple[str, str, float]] = {}
        self._ttl = ttl_seconds
        self._lock = threading.Lock()

    def create(self, state: str, verifier: str, provider: str) -> None:
        with self._lock:
            self._flows[state] = (verifier, provider, time.monotonic())

    def take(self, state: str | None) -> tuple[str, str] | None:
        """Consome (uso único) ``(verifier, provider)`` do ``state``; None se
        ausente/expirado."""
        if not state:
            return None
        with self._lock:
            item = self._flows.pop(state, None)
        if not item:
            return None
        verifier, provider, born = item
        if time.monotonic() - born > self._ttl:
            return None
        return verifier, provider

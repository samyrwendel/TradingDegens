"""Login do DONO (server-side) que destrava a chave de API do servidor.

Modelo (decidido pelo Samyr): o PÚBLICO usa a própria chave (BYOK, no navegador);
só o DONO logado usa a chave env do servidor, sem colar nada. Este módulo cuida da
autenticação do dono — nada de fachada client-side:

- O segredo do dono vive em ``TRADINGDEGENS_OWNER_TOKEN`` (env do servidor), NUNCA
  vai pro cliente.
- ``POST /api/login`` compara a senha em tempo-constante e cria uma sessão opaca;
  o id de sessão volta num cookie ``HttpOnly`` (JS não lê — resistente a XSS).
- Cada requisição traz o cookie; :meth:`is_valid` diz se é o dono.
- Sessões vivem em memória (processo único stdlib); um restart derruba todas (o
  dono reloga). Sem persistir segredo em disco além da env.

A chave do servidor em si nunca passa por aqui — o gating (só dono usa a env) é
aplicado no server/runner a partir do booleano ``is_owner``."""

from __future__ import annotations

import hmac
import os
import secrets
import threading
import time

_DEFAULT_TTL_SECONDS = 30 * 24 * 3600  # 30 dias
_SESSION_COOKIE = "td_session"


class OwnerAuth:
    """Verifica a senha do dono e mantém as sessões válidas em memória."""

    cookie_name = _SESSION_COOKIE

    def __init__(self, token_env: str = "TRADINGDEGENS_OWNER_TOKEN",
                 ttl_seconds: int = _DEFAULT_TTL_SECONDS):
        raw = os.environ.get(token_env)
        # senha vazia/'—' desabilita login (ninguém vira dono → todos BYOK).
        self._secret = raw if (raw and raw.strip()) else None
        self._ttl = ttl_seconds
        self._sessions: dict[str, float] = {}
        self._lock = threading.Lock()

    def enabled(self) -> bool:
        """Há senha do dono configurada no servidor?"""
        return self._secret is not None

    def verify_password(self, password: str | None) -> bool:
        """Compara a senha em tempo-constante (evita timing attack). Falso se o
        login não está configurado."""
        if self._secret is None or password is None:
            return False
        return hmac.compare_digest(str(password), self._secret)

    def create_session(self) -> str:
        """Cria uma sessão opaca e retorna o id (vai no cookie HttpOnly)."""
        sid = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[sid] = time.monotonic()
        return sid

    def is_valid(self, sid: str | None) -> bool:
        """A sessão existe e não expirou? (expira sozinha, sem varredura)."""
        if not sid:
            return False
        with self._lock:
            created = self._sessions.get(sid)
            if created is None:
                return False
            if time.monotonic() - created > self._ttl:
                self._sessions.pop(sid, None)
                return False
            return True

    def destroy(self, sid: str | None) -> None:
        """Encerra a sessão (logout)."""
        if not sid:
            return
        with self._lock:
            self._sessions.pop(sid, None)

"""Detecção READ-ONLY do login do CLI da box, por provedor (task 020, parte C).

A 019 abriu o OAuth mas o token 100% remoto esbarra no redirect loopback do CLI
(``localhost:PORT`` cai na máquina do BROWSER, não no servidor). Só que a box JÁ TEM
os três CLIs logados — então, pro DONO, refletimos "conectada (login do servidor)"
detectando esse login existente, sem round-trip nenhum:

- **openai**  → ``~/.local/share/opencode/auth.json`` (chave ``openai`` com ``access``)
- **anthropic** → ``~/.claude/.credentials.json`` (``claudeAiOauth.accessToken``)
- **google**  → ``~/.gemini/oauth_creds.json`` **ou** ``google_accounts.json`` com
  conta ``active`` não-nula

GUARDRAIL: a detecção é **estritamente read-only** e devolve só *existe?/quando?* —
NUNCA o conteúdo do token. Nada aqui escreve, move ou apaga essas credenciais (a box
depende delas; o mainbot roda em cima do claude CLI). O "Desconectar" da UI mexe só no
registro do APP (arquivo 0600 da 017), jamais nestes arquivos.

Caminhos são sobreponíveis por env (pros testes serem herméticos e não lerem a box):
``TRADINGDEGENS_CODEX_AUTH_FILE`` / ``TRADINGDEGENS_CLAUDE_CREDS_FILE`` /
``TRADINGDEGENS_GEMINI_DIR``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_NOT_CONNECTED: dict[str, Any] = {"connected": False, "detected_at": None}


def _mtime_iso(path: Path) -> str | None:
    """ISO 8601 (UTC) do mtime do arquivo — só "quando", nunca o conteúdo."""
    try:
        ts = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any] | None:
    """Lê JSON read-only; None se ausente/corrompido (fail-closed p/ detecção)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _codex_auth_path() -> Path:
    return Path(os.getenv("TRADINGDEGENS_CODEX_AUTH_FILE",
                          os.path.expanduser("~/.local/share/opencode/auth.json")))


def _claude_creds_path() -> Path:
    return Path(os.getenv("TRADINGDEGENS_CLAUDE_CREDS_FILE",
                          os.path.expanduser("~/.claude/.credentials.json")))


def _gemini_dir() -> Path:
    return Path(os.getenv("TRADINGDEGENS_GEMINI_DIR",
                          os.path.expanduser("~/.gemini")))


def detect_openai() -> dict[str, Any]:
    """Codex logado? ``auth.json`` com chave ``openai`` trazendo ``access``/``refresh``."""
    path = _codex_auth_path()
    data = _load_json(path)
    if not data:
        return dict(_NOT_CONNECTED)
    rec = data.get("openai")
    ok = isinstance(rec, dict) and bool(rec.get("access") or rec.get("refresh"))
    return {"connected": ok, "detected_at": _mtime_iso(path) if ok else None}


def detect_anthropic() -> dict[str, Any]:
    """Claude logado? ``.credentials.json`` com ``claudeAiOauth.accessToken``."""
    path = _claude_creds_path()
    data = _load_json(path)
    if not data:
        return dict(_NOT_CONNECTED)
    oauth = data.get("claudeAiOauth")
    ok = isinstance(oauth, dict) and bool(oauth.get("accessToken"))
    return {"connected": ok, "detected_at": _mtime_iso(path) if ok else None}


def detect_google() -> dict[str, Any]:
    """Gemini logado? ``oauth_creds.json`` com token, ou conta ``active`` não-nula."""
    base = _gemini_dir()
    creds = base / "oauth_creds.json"
    data = _load_json(creds)
    if isinstance(data, dict) and (data.get("access_token") or data.get("refresh_token")):
        return {"connected": True, "detected_at": _mtime_iso(creds)}
    accounts = base / "google_accounts.json"
    acc = _load_json(accounts)
    if isinstance(acc, dict) and (acc.get("active") or "").strip():
        return {"connected": True, "detected_at": _mtime_iso(accounts)}
    return dict(_NOT_CONNECTED)


_DETECTORS = {
    "openai": detect_openai,
    "anthropic": detect_anthropic,
    "google": detect_google,
}


def detect(provider: str) -> dict[str, Any]:
    """Detecção do login do CLI da box pro provedor. {connected, detected_at}.
    Provedor desconhecido → não conectada (fail-closed). NUNCA vaza o token."""
    fn = _DETECTORS.get((provider or "").strip().lower())
    return fn() if fn else dict(_NOT_CONNECTED)

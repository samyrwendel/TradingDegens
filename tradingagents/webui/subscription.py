"""Credencial da ASSINATURA do dono, conectada pela tela (task 017; multi-provedor 020).

Modelo (decidido pelo Samyr): SÓ o DONO logado (task 042) conecta a assinatura; o
token chega ao servidor por HEADER (nunca querystring/log), é guardado server-side e
JAMAIS volta ao cliente. Esta é a fase de LOGIN — a ligação com o litellm/proxy é a
fase seguinte, que lê ``token()`` daqui.

O token é um SEGREDO: fica num arquivo 0600 fora do repo (dir de dados do runtime),
escrito atômico. ``status()`` devolve só metadados (conectada?/quando), nunca o valor.

**Multi-provedor (task 020):** um registro por provedor (``openai``/``anthropic``/
``google``), cada um no seu arquivo 0600. Compat com a 017/019: o ``openai`` usa o
``path`` original; os demais viram irmãos ``<stem>-<provider><suffix>`` no mesmo dir.
Este é o "registro do APP" que o Desconectar remove — NUNCA as creds reais do CLI da
box (ver :mod:`server_login`, detecção read-only).
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from pathlib import Path
from typing import Any


class SubscriptionStore:
    """Guarda/lê a credencial da assinatura do dono, um arquivo 0600 por provedor.

    Fail-open na leitura (arquivo corrompido/ausente → 'não conectada'); a escrita é
    atômica (tmp + rename) e restringe a permissão a 0600 (só o dono do processo lê).
    """

    def __init__(self, path: str | os.PathLike):
        # ``path`` é o arquivo do provedor default (openai) — compat com a 017/019.
        self.path = Path(path)
        self._lock = threading.Lock()

    def _path_for(self, provider: str | None) -> Path:
        """Arquivo 0600 do provedor. ``openai`` = ``self.path`` (compat); os demais,
        irmãos ``<stem>-<provider><suffix>`` no mesmo diretório."""
        key = (provider or "openai").strip().lower()
        if key in ("", "openai"):
            return self.path
        return self.path.with_name(f"{self.path.stem}-{key}{self.path.suffix}")

    def connect(self, token: str, *, kind: str | None = None, provider: str = "openai",
                connected_at: str | None = None) -> dict[str, Any]:
        """Grava o token da assinatura do provedor (0600) e devolve o STATUS (sem o token).

        ``token`` vazio é rejeitado (ValueError) — nunca grava credencial em branco.
        """
        tok = (token or "").strip()
        if not tok:
            raise ValueError("token da assinatura vazio")
        provider = (provider or "openai").strip().lower() or "openai"
        record = {"access_token": tok, "kind": kind or provider,
                  "connected_at": connected_at or ""}
        target = self._path_for(provider)
        with self._lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp")
            # cria já com 0600 (evita janela onde o segredo fica legível a outros)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(record, fh)
            except Exception:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
            os.replace(tmp, target)
            with contextlib.suppress(OSError):
                os.chmod(target, 0o600)
            return self._status_locked(provider)

    def _read(self, provider: str | None = "openai") -> dict[str, Any] | None:
        path = self._path_for(provider)
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def _status_locked(self, provider: str | None = "openai") -> dict[str, Any]:
        data = self._read(provider) or {}
        tok = (data.get("access_token") or "").strip()
        return {
            "connected": bool(tok),
            "kind": data.get("kind") or None,
            "connected_at": data.get("connected_at") or None,
        }

    def status(self, provider: str = "openai") -> dict[str, Any]:
        """Metadados da conexão — NUNCA o token. {connected, kind, connected_at}."""
        with self._lock:
            return self._status_locked(provider)

    def token(self, provider: str = "openai") -> str | None:
        """O token em si — uso SERVER-SIDE apenas (fase seguinte: litellm/proxy)."""
        with self._lock:
            data = self._read(provider) or {}
            tok = (data.get("access_token") or "").strip()
            return tok or None

    def disconnect(self, provider: str = "openai") -> dict[str, Any]:
        """Remove o REGISTRO DO APP do provedor (desconecta a assinatura).

        Mexe só neste arquivo 0600 — jamais nas creds reais do CLI da box (essas são
        detectadas read-only e o mainbot depende delas)."""
        with self._lock:
            with contextlib.suppress(OSError):
                self._path_for(provider).unlink()
            return self._status_locked(provider)

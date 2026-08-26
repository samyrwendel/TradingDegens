"""Credencial da ASSINATURA do dono (OpenAI/ChatGPT), conectada pela tela (task 017).

Modelo (decidido pelo Samyr): SÓ o DONO logado (task 042) conecta a assinatura; o
token chega ao servidor por HEADER (nunca querystring/log), é guardado server-side e
JAMAIS volta ao cliente. Esta é a fase de LOGIN — a ligação com o litellm/proxy é a
fase seguinte, que lê ``token()`` daqui.

O token é um SEGREDO: fica num arquivo 0600 fora do repo (dir de dados do runtime),
escrito atômico. ``status()`` devolve só metadados (conectada?/quando), nunca o valor.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


class SubscriptionStore:
    """Guarda/lê a credencial da assinatura do dono num arquivo 0600.

    Fail-open na leitura (arquivo corrompido/ausente → 'não conectada'); a escrita é
    atômica (tmp + rename) e restringe a permissão a 0600 (só o dono do processo lê).
    """

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._lock = threading.Lock()

    def connect(self, token: str, *, kind: str = "openai", connected_at: str | None = None) -> dict[str, Any]:
        """Grava o token da assinatura (0600) e devolve o STATUS (sem o token).

        ``token`` vazio é rejeitado (ValueError) — nunca grava credencial em branco.
        """
        tok = (token or "").strip()
        if not tok:
            raise ValueError("token da assinatura vazio")
        record = {"access_token": tok, "kind": kind, "connected_at": connected_at or ""}
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            # cria já com 0600 (evita janela onde o segredo fica legível a outros)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(record, fh)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            os.replace(tmp, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        return self._status_locked()

    def _read(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def _status_locked(self) -> dict[str, Any]:
        data = self._read() or {}
        tok = (data.get("access_token") or "").strip()
        return {
            "connected": bool(tok),
            "kind": data.get("kind") or None,
            "connected_at": data.get("connected_at") or None,
        }

    def status(self) -> dict[str, Any]:
        """Metadados da conexão — NUNCA o token. {connected, kind, connected_at}."""
        with self._lock:
            return self._status_locked()

    def token(self) -> str | None:
        """O token em si — uso SERVER-SIDE apenas (fase seguinte: litellm/proxy)."""
        with self._lock:
            data = self._read() or {}
            tok = (data.get("access_token") or "").strip()
            return tok or None

    def disconnect(self) -> dict[str, Any]:
        """Remove a credencial (desconecta a assinatura)."""
        with self._lock:
            try:
                self.path.unlink()
            except OSError:
                pass
            return self._status_locked()

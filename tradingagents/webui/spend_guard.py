"""Teto de gasto declarado nas rotas pagas do web UI (porta 6 / item 3, DA-189).

Só a run que roda na CHAVE DO SERVIDOR (dono logado, sem BYOK) consome deste teto —
BYOK gasta a chave do PRÓPRIO usuário e não conta aqui. O teto é DIÁRIO (bucket por
dia UTC), lido de ``TRADINGDEGENS_SPEND_CAP_USD``. Estourou → recusa EXPLÍCITA (o
servidor responde 402 ``spend_cap``), NUNCA degrada em silêncio (não cai pra modelo
barato, não pula etapa — para e avisa).

``cap_usd <= 0`` = DESLIGADO. Nesse caso o servidor não impõe teto, mas AVISA alto no
boot (``main`` loga o aviso) — o "sem teto" é declarado, nunca silencioso. O valor
em dólar é decisão de produto do Samyr (registrar em DA quando definido); o mecanismo
e a recusa já ficam prontos e testados.

O ledger é um JSON pequeno ``{dia: usd}``, escrito de forma atômica. Persistir (em vez
de só contar em memória) faz o teto sobreviver ao restart — senão um serviço que
reinicia zeraria o gasto do dia e o teto viraria decoração.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class SpendGuard:
    """Acumulador de gasto na chave do servidor, com teto diário e recusa explícita."""

    def __init__(self, *, cap_usd: float,
                 ledger_path: str | os.PathLike | None = None,
                 clock=time.time) -> None:
        self.cap_usd = float(cap_usd)
        self._path = Path(ledger_path) if ledger_path else None
        self._clock = clock
        self._lock = threading.Lock()

    # -- estado ---------------------------------------------------------------
    def enabled(self) -> bool:
        """Teto ligado? (``cap_usd`` positivo)."""
        return self.cap_usd > 0

    def _day(self) -> str:
        return datetime.fromtimestamp(self._clock(), tz=timezone.utc).strftime("%Y-%m-%d")

    def _load(self) -> dict[str, Any]:
        if not self._path or not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def spent_today(self) -> float:
        with self._lock:
            try:
                return float(self._load().get(self._day(), 0.0) or 0.0)
            except (TypeError, ValueError):
                return 0.0

    def exceeded(self) -> bool:
        """Já bateu o teto do dia? (falso quando desligado)."""
        if not self.enabled():
            return False
        return self.spent_today() >= self.cap_usd

    def remaining(self) -> float:
        if not self.enabled():
            return float("inf")
        return max(0.0, self.cap_usd - self.spent_today())

    # -- mutação --------------------------------------------------------------
    def record(self, usd: float) -> None:
        """Soma ``usd`` ao gasto de HOJE (no-op se <= 0 ou sem ledger em disco)."""
        try:
            valor = float(usd)
        except (TypeError, ValueError):
            return
        if valor <= 0 or not self._path:
            return
        with self._lock:
            data = self._load()
            day = self._day()
            data[day] = float(data.get(day, 0.0) or 0.0) + valor
            # Poda dias velhos (mantém ~14) pra o arquivo não crescer sem fim.
            if len(data) > 14:
                for k in sorted(data)[:-14]:
                    data.pop(k, None)
            self._atomic_write(data)

    def refusal_message(self) -> str:
        return (f"Teto de gasto diário do servidor atingido "
                f"(US$ {self.cap_usd:.2f}/dia). Rode com a sua própria chave (BYOK) "
                f"ou aguarde o próximo dia — a chave do servidor está pausada.")

    # -- interno --------------------------------------------------------------
    def _atomic_write(self, data: dict[str, Any]) -> None:
        assert self._path is not None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(data), "utf-8")
        os.replace(tmp, self._path)


def _cap_from_env() -> float:
    raw = os.getenv("TRADINGDEGENS_SPEND_CAP_USD")
    if raw is None or not raw.strip():
        return 0.0  # desligado por default — o valor em $ é decisão do Samyr
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


def _ledger_path_from_env() -> str:
    p = os.getenv("TRADINGDEGENS_SPEND_LEDGER")
    if p:
        return p
    base = os.getenv("TRADINGDEGENS_STATE_DIR") or os.path.expanduser(
        "~/.local/state/tradingdegens")
    return os.path.join(base, "spend_ledger.json")


# Singleton de processo: o servidor (gate) e o runner (record) usam a MESMA
# instância pra ler/escrever o mesmo ledger. Testes trocam ``GUARD`` por um guard
# apontando pra um tmp (ver test_webui_ratelimit.py).
GUARD = SpendGuard(cap_usd=_cap_from_env(), ledger_path=_ledger_path_from_env())

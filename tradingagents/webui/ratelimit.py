"""Rate limiting pro web UI — guarda de força-bruta e de rajada (porta 6, DA-189).

Thread-safe: o servidor é ``ThreadingHTTPServer`` e cada requisição roda numa
thread própria — toda leitura/escrita do estado passa por um ``Lock``.

Dois consumidores hoje:
  - **login do dono**: poucas tentativas por IP numa janela, com LOCKOUT
    PROGRESSIVO (cada estouro seguido dobra o castigo, até um teto). A senha do
    dono (``TRADINGDEGENS_OWNER_TOKEN``) é humana e curta — sem isto é
    força-bruta viável de verdade.
  - **rotas caras** (analyze/compare/ask): teto de RAJADA por PRINCIPAL (a sessão
    do dono, ou o IP anônimo) numa rota que gasta token de LLM.

Não vaza identidade (porta 5 NÃO regride): o 429 é idêntico pra senha/chave
existente ou não — o limiter age sobre a CHAVE de rate (IP/sessão), nunca sobre o
resultado da autenticação. E a chave do login é o IP, não a senha: o mesmo 429
sai pra tentativa com senha errada ou ausente.
"""

from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass, field


@dataclass
class Decision:
    """Resultado de um ``hit``: passou? e, se não, em quantos segundos libera."""

    allowed: bool
    retry_after: int = 0


@dataclass
class _Bucket:
    hits: list[float] = field(default_factory=list)
    blocked_until: float = 0.0
    violations: int = 0
    last_seen: float = 0.0


def _ceil(x: float) -> int:
    return int(math.ceil(max(0.0, x)))


class RateLimiter:
    """Janela deslizante + lockout progressivo, por chave.

    ``max_hits`` requisições por ``window_s`` segundos são livres; a que passaria
    do teto é barrada. Com ``block_base_s > 0``, o estouro vira um BLOQUEIO
    temporizado que dobra a cada reincidência (``block_base_s * 2**(violações-1)``,
    limitado a ``block_max_s``) — o lockout progressivo do login. Com
    ``block_base_s == 0``, não há bloqueio fixo: a requisição excedente só espera a
    janela deslizar (teto de rajada das rotas caras).
    """

    def __init__(self, *, max_hits: int, window_s: float,
                 block_base_s: float = 0.0, block_max_s: float = 3600.0,
                 clock=time.monotonic) -> None:
        if max_hits < 1:
            raise ValueError("max_hits precisa ser >= 1")
        if window_s <= 0:
            raise ValueError("window_s precisa ser > 0")
        self.max_hits = int(max_hits)
        self.window_s = float(window_s)
        self.block_base_s = float(block_base_s)
        self.block_max_s = float(block_max_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}

    def hit(self, key: str) -> Decision:
        """Registra e avalia uma tentativa na ``key``. Só a tentativa PERMITIDA é
        contada — a barrada não empurra a janela (nem infla a memória)."""
        now = self._clock()
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                b = _Bucket()
                self._buckets[key] = b
            b.last_seen = now

            # Bloqueio ativo: nega sem recontar (evita esticar o castigo a cada hit).
            if b.blocked_until > now:
                return Decision(False, _ceil(b.blocked_until - now))

            floor = now - self.window_s
            b.hits = [t for t in b.hits if t > floor]

            if len(b.hits) < self.max_hits:
                b.hits.append(now)
                self._maybe_gc(now)
                return Decision(True)

            # Estourou o teto da janela.
            b.violations += 1
            if self.block_base_s > 0:
                castigo = min(self.block_max_s,
                              self.block_base_s * (2 ** (b.violations - 1)))
                b.blocked_until = now + castigo
                b.hits.clear()
                self._maybe_gc(now)
                return Decision(False, _ceil(castigo))

            # Sem lockout fixo: espera o hit mais antigo sair da janela.
            oldest = min(b.hits)
            self._maybe_gc(now)
            return Decision(False, max(1, _ceil(oldest + self.window_s - now)))

    def reset(self, key: str) -> None:
        """Zera a chave (ex.: login CORRETO libera o IP na hora)."""
        with self._lock:
            self._buckets.pop(key, None)

    def _maybe_gc(self, now: float) -> None:
        # Poda preguiçosa: só varre quando o mapa cresce, some com chave inerte
        # (sem bloqueio ativo e sem atividade recente). Chamado já sob o lock.
        if len(self._buckets) <= 4096:
            return
        horizon = now - max(self.window_s, self.block_max_s)
        dead = [k for k, b in self._buckets.items()
                if b.blocked_until <= now and b.last_seen < horizon]
        for k in dead:
            self._buckets.pop(k, None)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return v if v >= 1 else default


def login_limiter_from_env() -> RateLimiter:
    """Limiter do ``/api/login``: 5 tentativas / 5 min por IP, lockout de 60s que
    dobra a cada reincidência até 1h. Todos os valores são env-tunáveis."""
    return RateLimiter(
        max_hits=_env_int("TRADINGDEGENS_LOGIN_MAX", 5),
        window_s=_env_int("TRADINGDEGENS_LOGIN_WINDOW_S", 300),
        block_base_s=_env_int("TRADINGDEGENS_LOGIN_LOCKOUT_S", 60),
        block_max_s=_env_int("TRADINGDEGENS_LOGIN_LOCKOUT_MAX_S", 3600),
    )


def expensive_limiter_from_env() -> RateLimiter:
    """Limiter das rotas caras (analyze/compare/ask): 20 por minuto por princípio
    (sessão do dono ou IP). Teto de rajada, sem lockout fixo. Env-tunável."""
    return RateLimiter(
        max_hits=_env_int("TRADINGDEGENS_EXPENSIVE_MAX", 20),
        window_s=_env_int("TRADINGDEGENS_EXPENSIVE_WINDOW_S", 60),
    )

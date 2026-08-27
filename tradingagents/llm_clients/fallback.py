"""Cadeia de fallback TRANSPARENTE e AUTOMÁTICA entre provedores LLM (task 027-fallback).

Quando uma chamada LLM falha por ESTADO do provedor — 429 (rate limit), 401/402
(sem crédito/auth), 5xx ou timeout — o motor NÃO para a análise: troca sozinho pro
próximo provedor saudável da cadeia daquele nível e CONTINUA. Cada troca é REGISTRADA
(transparente, não silenciosa) num :class:`FallbackTracker` que o runner surfa por-etapa,
pra o dono SABER que houve o desvio ("fallback: claude-cli → openai, motivo 429").

Não-erro-de-provedor (bug de código, cancelamento) NÃO dispara fallback — propaga direto,
sem loop. A classificação reusa :func:`tradingagents.webui.errors.classify_provider_error`
— o MESMO 429/401/... que humaniza o erro final da UI —, então o gatilho do fallback e a
mensagem honesta de erro falam a mesma língua.

**Ponto de integração — nível do CLIENTE LLM.** :class:`FallbackRunnable` embrulha uma
lista ORDENADA de membros (cada um um chat model real, ou o ``bind_tools`` /
``with_structured_output`` dele) e delega ``.invoke`` ao primeiro; no erro-de-provedor,
anda pro próximo. Como cada membro carrega os MESMOS callbacks (custo, progresso,
atribuição por-etapa 024P1, cancelamento) e o ``config`` do nó é repassado intacto, o
membro que de fato roda dispara os callbacks com o modelo REAL — a atribuição por etapa
já mostra o provedor do fallback, sem inventar. E como o LangGraph faz checkpoint por-nó
(022), os nós já concluídos não re-rodam: a troca acontece só na etapa que caiu, e a
análise segue do ponto em que estava.

Bounded: a cadeia é montada com teto de saltos (``1 + max_hops``); aqui o laço é finito
(uma tentativa por membro), então cadeia inteira caída → erro honesto do último provedor,
nunca um loop.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from langchain_core.runnables import Runnable

from tradingagents.webui.errors import classify_provider_error

logger = logging.getLogger(__name__)

# Motivo curto (pt-BR) por código de erro de provedor, pro marcador visível na etapa.
_REASON_PT = {
    "rate_limit": "limite/429",
    "no_credit": "sem crédito/402",
    "invalid_key": "auth/401",
    "unavailable": "indisponível/5xx",
}


def provider_error_code(exc: BaseException) -> str | None:
    """Código estável se ``exc`` é erro de ESTADO do provedor (dispara fallback), ou
    ``None`` se não é (bug de código / cancelamento → propaga sem fallback).

    Reusa a mesma classificação por texto que humaniza o erro final da UI
    (``no_credit`` | ``invalid_key`` | ``rate_limit`` | ``unavailable``)."""
    return classify_provider_error(f"{type(exc).__name__}: {exc}")


class FallbackTracker:
    """Registro thread-safe das trocas de provedor por etapa (nó do grafo).

    O :class:`FallbackRunnable` escreve na thread do grafo; o snapshot é lido na
    thread HTTP. Só EXPÕE o que de fato aconteceu (uma troca real), keyed pelo nó —
    nunca inventa um desvio que não houve."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hops: dict[str, list[dict[str, Any]]] = {}

    def record(self, node: str | None, frm: dict[str, Any], to: dict[str, Any],
               code: str | None) -> None:
        """Registra uma troca ``frm`` → ``to`` no ``node``, com o ``code`` do motivo."""
        key = node or "—"
        hop = {
            "node": node,
            "from_provider": frm.get("provider"), "from_model": frm.get("model"),
            "to_provider": to.get("provider"), "to_model": to.get("model"),
            "code": code, "reason": _REASON_PT.get(code or "", code or "erro"),
        }
        with self._lock:
            self._hops.setdefault(key, []).append(hop)

    def any(self) -> bool:
        with self._lock:
            return bool(self._hops)

    def snapshot(self) -> list[dict[str, Any]]:
        """Lista plana de todas as trocas (ordem de registro), pro runner surfar."""
        with self._lock:
            rows: list[dict[str, Any]] = []
            for hops in self._hops.values():
                rows.extend(hops)
            return rows


class FallbackRunnable(Runnable):
    """Runnable que tenta uma CADEIA ordenada de membros LLM, caindo pro próximo só em
    erro de ESTADO do provedor (429/401/402/5xx/timeout) — e registrando cada troca.

    ``members``: lista de ``{"provider", "model", "llm"}`` onde ``llm`` é um Runnable
    (chat model real, ou o resultado de ``bind_tools`` / ``with_structured_output`` dele).
    A cadeia já vem LIMITADA na construção (``1 + max_hops``); aqui só se itera o que veio.
    ``bind_tools`` / ``with_structured_output`` fazem fan-out mantendo a cadeia e o tracker,
    pra que o padrão ``prompt | llm.bind_tools(tools)`` dos analistas continue funcionando.
    """

    def __init__(self, members: list[dict[str, Any]], tracker: FallbackTracker | None = None,
                 level: str = "", max_hops: int = 2) -> None:
        self.members = list(members)
        self.tracker = tracker
        self.level = level
        self.max_hops = max_hops

    # -- fan-out do binding (mantém a cadeia inteira) --------------------------
    def bind_tools(self, tools: Any, **kwargs: Any) -> FallbackRunnable:
        return FallbackRunnable(
            [{**m, "llm": m["llm"].bind_tools(tools, **kwargs)} for m in self.members],
            tracker=self.tracker, level=self.level, max_hops=self.max_hops,
        )

    def with_structured_output(self, schema: Any, **kwargs: Any) -> FallbackRunnable:
        return FallbackRunnable(
            [{**m, "llm": m["llm"].with_structured_output(schema, **kwargs)}
             for m in self.members],
            tracker=self.tracker, level=self.level, max_hops=self.max_hops,
        )

    @staticmethod
    def _node(config: Any) -> str | None:
        """Nó LangGraph desta chamada (do metadata do config), pra keyar a troca."""
        try:
            return ((config or {}).get("metadata") or {}).get("langgraph_node")
        except Exception:  # noqa: BLE001
            return None

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        node = self._node(config)
        n = len(self.members)
        last_exc: Exception | None = None
        for i, m in enumerate(self.members):
            try:
                # config repassado intacto → o membro carrega o langgraph_node e os
                # callbacks do run (custo/progresso/atribuição/cancelamento) disparam
                # com o modelo REAL que rodou.
                return m["llm"].invoke(input, config, **kwargs)
            except Exception as exc:  # noqa: BLE001 — reclassificado abaixo
                code = provider_error_code(exc)
                # Bug de código / cancelamento (não classificado) → NÃO é estado de
                # provedor: propaga já, sem fallback, sem loop.
                if code is None:
                    raise
                last_exc = exc
                nxt = self.members[i + 1] if i + 1 < n else None
                if nxt is None:
                    # Cadeia inteira caiu → erro honesto do último provedor.
                    raise
                # Troca TRANSPARENTE: registra o desvio (do membro i pro i+1).
                if self.tracker is not None:
                    self.tracker.record(node, m, nxt, code)
                logger.warning(
                    "fallback %s: %s → %s (nó %s, motivo %s)",
                    self.level or "?", m.get("provider"), nxt.get("provider"),
                    node or "—", code,
                )
                # segue o laço pro próximo membro da cadeia
        # Sem membros (não deveria ocorrer): erro claro em vez de retorno silencioso.
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("FallbackRunnable sem membros para invocar")

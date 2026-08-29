"""Avisos de QUALIDADE DE DADO da camada de fetch — o cano até ``degraded_sources``.

A camada de dataflows não conhece o estado do grafo, então uma degradação que ela
comete (servir cache OHLCV vencido porque o download falhou) morria num
``logger.warning`` e o relatório saía com número velho e cara de número novo. Foi
exatamente o bug L2: a série diária do MCD terminava em 24/08, a queda real de
-4,6% virou -1,3%, o ``drop_nature`` leu ``indefinido`` — e nada disso chegou ao
leitor.

Este módulo é o cano. O fetch **registra**; o runner **drena** no fim da run e
junta em ``degraded_sources``, o mesmo canal que a UI já sabe nomear (task
20260828-003/004). Por thread, porque cada run roda na sua — um worker nunca herda
o aviso do outro.

``kind="suspect"``: o dado ESTÁ na análise, só não é confiável. Não é ``missing``
(a fonte não sumiu) e chamar de ausente seria mentira na direção oposta.
"""

from __future__ import annotations

import threading
from typing import Any

_local = threading.local()


def _bucket() -> list[dict[str, str]]:
    got = getattr(_local, "avisos", None)
    if got is None:
        got = []
        _local.avisos = got
    return got


def reset() -> None:
    """Zera os avisos da thread — chamado no INÍCIO de cada run."""
    _local.avisos = []


def record(label: str, reason: str, *, kind: str = "suspect",
           report_key: str = "") -> None:
    """Registra um aviso de qualidade de dado (dedup por label+reason).

    Dedup porque a mesma série degradada é lida várias vezes na mesma run (vários
    indicadores, vários frames) — o leitor precisa saber UMA vez, não quinze.
    """
    entry = {"label": str(label), "report_key": str(report_key),
             "reason": str(reason), "kind": str(kind)}
    bucket = _bucket()
    if entry not in bucket:
        bucket.append(entry)


def drain() -> list[dict[str, str]]:
    """Devolve os avisos acumulados e limpa — chamado no FIM da run."""
    got = _bucket()
    _local.avisos = []
    return got


def snapshot() -> list[dict[str, str]]:
    """Lê sem limpar (pra teste e pra inspeção no meio do caminho)."""
    return list(_bucket())


def merge_into(state: Any) -> Any:
    """Junta os avisos drenados no ``degraded_sources`` de um estado final.

    Fail-open: um aviso perdido nunca pode derrubar uma análise que rodou.
    """
    avisos = drain()
    if not avisos or not isinstance(state, dict):
        return state
    atual = state.get("degraded_sources")
    state["degraded_sources"] = list(atual or []) + avisos
    return state

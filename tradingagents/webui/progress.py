"""Live pipeline progress for the web UI.

The engine runs ~100s across four analysts → research debate → trader → risk
debate → final judge. A screen frozen for 100s reads as broken, so we surface
which stage is running. We get that from LangGraph, which stamps every node's
callbacks with ``metadata["langgraph_node"]`` (confirmed in
``langgraph/pregel/_messages.py``). :class:`ProgressCallbackHandler` reads that
node name off each LLM/tool/chain start and advances a :class:`ProgressTracker`.

No node name is invented: an unrecognised node is ignored rather than shown as a
bogus stage.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

# node name -> (order, pt-BR label, phase). Order drives the progress bar; the
# tool nodes (tools_market, ...) and "Msg Clear *" nodes are intentionally left
# out — a tool call belongs to the analyst that is already the current stage, so
# ignoring them keeps the bar from flickering backwards.
_STAGE_MAP: dict[str, tuple[int, str, str]] = {
    "Market Analyst":       (10, "Analista de Mercado — preço, múltiplos tempos gráficos, derivativos", "Analistas"),
    "Sentiment Analyst":    (20, "Analista de Sentimento", "Analistas"),
    "News Analyst":         (30, "Analista de Notícias — macro e mercados de previsão", "Analistas"),
    "Fundamentals Analyst": (40, "Analista Fundamentalista", "Analistas"),
    "Erick Analyst":        (45, "Método Erick — recuo à média, saída, peso do trade", "Analistas"),
    "Bull Researcher":      (50, "Pesquisador do bull case", "Debate"),
    "Bear Researcher":      (60, "Pesquisador do bear case", "Debate"),
    "Research Manager":     (70, "Gestor de Pesquisa — juiz do debate", "Debate"),
    "Trader":               (80, "Trader — plano de execução", "Execução"),
    "Aggressive Analyst":   (90, "Debate de Risco — Agressivo", "Risco"),
    "Conservative Analyst": (92, "Debate de Risco — Conservador", "Risco"),
    "Neutral Analyst":      (94, "Debate de Risco — Neutro", "Risco"),
    "Portfolio Manager":    (100, "Gestor de Portfólio — veredito final", "Risco"),
}

# Analyst wire-key -> its node name, so the plan reflects the run's actual
# analyst selection (crypto drops fundamentals).
_ANALYST_NODE = {
    "market": "Market Analyst",
    "social": "Sentiment Analyst",
    "news": "News Analyst",
    "fundamentals": "Fundamentals Analyst",
    "erick": "Erick Analyst",
}

# Fixed downstream nodes that always run after the analysts, in order.
_DOWNSTREAM_NODES = [
    "Bull Researcher",
    "Bear Researcher",
    "Research Manager",
    "Trader",
    "Aggressive Analyst",
    "Conservative Analyst",
    "Neutral Analyst",
    "Portfolio Manager",
]


def stage_for_node(node: str) -> tuple[int, str, str] | None:
    """Look up a node's (order, label, phase); ``None`` for unmapped nodes."""
    return _STAGE_MAP.get(node)


def build_plan(selected_analysts: list[str] | tuple[str, ...]) -> list[dict[str, Any]]:
    """Ordered stage plan for a run given its analyst selection.

    Used both to drive the progress bar denominator and to render the full
    step list up front, so the user sees where the pipeline is headed.
    """
    nodes = [_ANALYST_NODE[a] for a in selected_analysts if a in _ANALYST_NODE]
    nodes += _DOWNSTREAM_NODES
    plan = []
    for node in nodes:
        order, label, phase = _STAGE_MAP[node]
        plan.append({"node": node, "order": order, "label": label, "phase": phase})
    return plan


class ProgressTracker:
    """Thread-safe record of how far a run has advanced through its plan."""

    def __init__(self, selected_analysts: list[str] | tuple[str, ...]):
        self._lock = threading.Lock()
        self.plan = build_plan(selected_analysts)
        self._order_index = {p["order"]: i for i, p in enumerate(self.plan)}
        self._current_order = -1
        self._reached: list[dict[str, Any]] = []
        self._phase = "Inicializando"
        self._label = "Inicializando o motor…"
        self._started_at = time.monotonic()
        self._done = False

    def note_node(self, node: str) -> None:
        """Advance to ``node``'s stage if it is further along than the current one."""
        stage = stage_for_node(node)
        if stage is None:
            return
        order, label, phase = stage
        with self._lock:
            if order <= self._current_order:
                return
            self._current_order = order
            self._label = label
            self._phase = phase
            self._reached.append({"label": label, "phase": phase})

    def mark_done(self) -> None:
        with self._lock:
            self._done = True
            self._current_order = 10_000
            self._phase = "Concluído"
            self._label = "Análise concluída"

    def snapshot(self) -> dict[str, Any]:
        """JSON-serialisable progress view for the status endpoint."""
        with self._lock:
            total = len(self.plan)
            if self._done:
                idx = total
            elif self._current_order in self._order_index:
                idx = self._order_index[self._current_order] + 1
            else:
                idx = 0
            percent = int(round(100 * idx / total)) if total else 0
            return {
                "phase": self._phase,
                "label": self._label,
                "index": idx,
                "total": total,
                "percent": percent,
                "elapsed": round(time.monotonic() - self._started_at, 1),
                "plan": [{"label": p["label"], "phase": p["phase"]} for p in self.plan],
                "reached": list(self._reached),
            }


class ProgressCallbackHandler(BaseCallbackHandler):
    """Feeds the current LangGraph node into a :class:`ProgressTracker`.

    Reads ``metadata["langgraph_node"]`` off every LLM / chat / tool / chain
    start. Any exception here must never break the run, so lookups are defensive.
    """

    def __init__(self, tracker: ProgressTracker):
        super().__init__()
        self.tracker = tracker

    def _note(self, kwargs: dict[str, Any]) -> None:
        try:
            node = (kwargs.get("metadata") or {}).get("langgraph_node")
            if node:
                self.tracker.note_node(node)
        except Exception:
            pass

    def on_chat_model_start(self, serialized, messages, **kwargs) -> None:
        self._note(kwargs)

    def on_llm_start(self, serialized, prompts, **kwargs) -> None:
        self._note(kwargs)

    def on_tool_start(self, serialized, input_str, **kwargs) -> None:
        self._note(kwargs)

    def on_chain_start(self, serialized, inputs, **kwargs) -> None:
        self._note(kwargs)

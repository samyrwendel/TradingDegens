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


# ── Raciocínio AO VIVO (task 008) ───────────────────────────────────────────
# Uma análise leva 13-21min; ver só QUAL agente roda (a barra) é um black box. Os
# agentes JÁ produzem o texto do parecer — hoje só aparece no fim. Isto REVELA esse
# texto progressivamente, com CUSTO ZERO de LLM: captura a SAÍDA de cada nó direto
# do callback do LLM (o mesmo já anexado pra medir uso/progresso), sem re-perguntar
# nada ao modelo. Cada card mostra o que aquele agente está pensando/escrevendo.

# node LangGraph -> (rótulo pt-BR, fase, é_debate). Ordem = ordem do pipeline (a
# ordem em que o usuário assiste a análise ser construída). O debate alta×baixa e o
# de risco ganham destaque (is_debate) — é o mais interessante de acompanhar.
_THINKING_NODES: list[tuple[str, str, str, bool]] = [
    ("Market Analyst",       "📊 Mercado — preço e tempos gráficos",       "Analistas", False),
    ("Sentiment Analyst",    "💬 Sentimento",                              "Analistas", False),
    ("News Analyst",         "📰 Notícias — macro e mercados de previsão", "Analistas", False),
    ("Fundamentals Analyst", "📑 Fundamentos",                             "Analistas", False),
    ("Erick Analyst",        "🧭 Método Erick",                            "Analistas", False),
    ("Bull Researcher",      "🟢 Tese de Alta (bull)",                     "Debate",    True),
    ("Bear Researcher",      "🔴 Tese de Baixa (bear)",                    "Debate",    True),
    ("Research Manager",     "⚖️ Juiz do Debate",                          "Debate",    False),
    ("Trader",               "🎯 Plano do Trader",                         "Execução",  False),
    ("Aggressive Analyst",   "🔥 Risco — Agressivo",                       "Risco",     True),
    ("Conservative Analyst", "🛡️ Risco — Conservador",                     "Risco",     True),
    ("Neutral Analyst",      "⚖️ Risco — Neutro",                          "Risco",     True),
    ("Portfolio Manager",    "🛡️ Decisão de Risco (veredito)",             "Risco",     False),
]
_THINKING_INDEX = {
    node: (i, label, phase, debate)
    for i, (node, label, phase, debate) in enumerate(_THINKING_NODES)
}

# Cap por card: os pareceres cabem folgado; o teto evita despejar um payload gigante
# a cada poll (2s) e protege o mobile. O texto integral vem no resultado final.
_THINKING_CAP = 8000


def _text_from_llm_response(response: Any) -> str:
    """Texto plano da resposta do LLM (chat ou completions), tolerante a formatos.

    Cobre content string, blocos de conteúdo (lista de dicts com ``text``) e o
    ``.text`` das gerações antigas. Nunca levanta — no pior caso devolve "".
    """
    try:
        parts: list[str] = []
        for row in getattr(response, "generations", None) or []:
            for gen in row:
                msg = getattr(gen, "message", None)
                content = getattr(msg, "content", None) if msg is not None else getattr(gen, "text", "")
                if isinstance(content, list):   # provedores que devolvem blocos
                    content = "".join(
                        (b.get("text", "") if isinstance(b, dict) else str(b)) for b in content
                    )
                if content:
                    parts.append(content if isinstance(content, str) else str(content))
        return "\n".join(parts).strip()
    except Exception:
        return ""


def _model_from_kwargs(kwargs: dict[str, Any]) -> tuple[str | None, str | None]:
    """(provider, model) REAL de uma chamada de LLM, lido do callback.

    O langchain-core carimba ``metadata['ls_provider']`` e ``ls_model_name`` em toda
    chamada de chat/LLM (params padrão do LangSmith); é a fonte do que de fato rodou.
    Fallback nos ``invocation_params`` (``model``/``model_name``) pra clientes que não
    populam o metadata. Nunca levanta — no pior caso devolve (None, None)."""
    try:
        meta = kwargs.get("metadata") or {}
        provider = meta.get("ls_provider")
        model = meta.get("ls_model_name")
        if not model:
            inv = kwargs.get("invocation_params") or {}
            model = inv.get("model") or inv.get("model_name")
        return provider, model
    except Exception:
        return None, None


class ThinkingTracker:
    """Guarda o TEXTO produzido por cada nó, por agente, pra revelar o raciocínio ao
    vivo. Thread-safe: o callback escreve na thread do grafo, o snapshot é lido na
    thread HTTP. Só EXPÕE o que o grafo já gera — zero custo extra de LLM."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._texts: dict[str, str] = {}     # node -> último texto (recortado)
        # Atribuição por etapa (task 024, parte 1): node -> {provider, model} que
        # REALMENTE rodou aquela etapa, lido do callback do LLM (metadata ls_* do
        # langchain) — o que rodou, não o configurado. Responde "qual LLM fez cada
        # etapa" no card ao vivo e no rodapé de auditoria.
        self._models: dict[str, dict[str, str | None]] = {}

    def set_by_node(self, node: str, text: str) -> None:
        if node not in _THINKING_INDEX or not text:
            return
        text = text.strip()
        if len(text) < 8:            # descarta ruído trivial de meio de tool-loop
            return
        if len(text) > _THINKING_CAP:
            text = text[:_THINKING_CAP] + "…"
        with self._lock:
            self._texts[node] = text

    def set_model(self, node: str, provider: str | None, model: str | None) -> None:
        """Registra o provedor/modelo que rodou ``node`` (real, do callback). Só grava
        nós conhecidos e quando há ao menos o modelo — nunca inventa atribuição."""
        if node not in _THINKING_INDEX or not model:
            return
        with self._lock:
            self._models[node] = {"provider": provider, "model": model}

    def snapshot(self) -> list[dict[str, Any]]:
        """Cards com texto, na ordem do pipeline (estável — o front não reordena)."""
        with self._lock:
            items = []
            for node, txt in self._texts.items():
                order, label, phase, debate = _THINKING_INDEX[node]
                attr = self._models.get(node) or {}
                items.append({
                    "id": node, "label": label, "phase": phase, "debate": debate,
                    "order": order, "len": len(txt), "text": txt,
                    # atribuição por etapa: qual LLM rodou este card (None até o 1º start)
                    "provider": attr.get("provider"), "model": attr.get("model"),
                })
        items.sort(key=lambda it: it["order"])
        return items

    def models_snapshot(self) -> list[dict[str, Any]]:
        """Atribuição por etapa pra o rodapé de auditoria: lista ordenada de
        {node, label, phase, provider, model} SÓ dos nós que de fato rodaram (têm
        modelo capturado). É o registro auditável de qual LLM fez cada etapa."""
        with self._lock:
            rows = []
            for node, attr in self._models.items():
                if node not in _THINKING_INDEX:
                    continue
                order, label, phase, _debate = _THINKING_INDEX[node]
                rows.append({
                    "node": node, "label": label, "phase": phase, "order": order,
                    "provider": attr.get("provider"), "model": attr.get("model"),
                })
        rows.sort(key=lambda r: r["order"])
        return rows


class ThinkingCallbackHandler(BaseCallbackHandler):
    """Alimenta a :class:`ThinkingTracker` com a saída dos LLMs de cada nó.

    Os callbacks vivem no LLM (não no grafo), então correlacionamos por ``run_id``:
    no *start* guardamos o ``langgraph_node`` daquela chamada; no *end* atribuímos o
    texto ao nó. Se o provedor faz streaming, ``on_llm_new_token`` cresce o card em
    tempo real; senão, o texto aparece por-agente-concluído (resolve o essencial).
    Defensivo: qualquer erro aqui é engolido — nunca quebra a run.
    """

    def __init__(self, tracker: ThinkingTracker):
        super().__init__()
        self.tracker = tracker
        self._node_by_run: dict[Any, str] = {}    # run_id -> node
        self._partial: dict[Any, str] = {}        # run_id -> texto em streaming

    def _remember(self, kwargs: dict[str, Any]) -> None:
        try:
            meta = kwargs.get("metadata") or {}
            node = meta.get("langgraph_node")
            rid = kwargs.get("run_id")
            if node and rid is not None:
                self._node_by_run[rid] = node
            # Atribuição por etapa (task 024): o modelo REAL desta chamada vem do
            # metadata padrão do langchain (ls_model_name/ls_provider), com fallback
            # nos invocation_params. Registra qual LLM rodou este nó — o que rodou, não
            # o configurado. Defensivo: erro aqui nunca quebra a run.
            if node:
                provider, model = _model_from_kwargs(kwargs)
                if model:
                    self.tracker.set_model(node, provider, model)
        except Exception:
            pass

    def on_chat_model_start(self, serialized, messages, **kwargs) -> None:
        self._remember(kwargs)

    def on_llm_start(self, serialized, prompts, **kwargs) -> None:
        self._remember(kwargs)

    def on_llm_new_token(self, token: str, **kwargs) -> None:
        try:
            rid = kwargs.get("run_id")
            node = self._node_by_run.get(rid)
            if not node:
                return
            self._partial[rid] = self._partial.get(rid, "") + (token or "")
            self.tracker.set_by_node(node, self._partial[rid])
        except Exception:
            pass

    def on_llm_end(self, response, **kwargs) -> None:
        try:
            rid = kwargs.get("run_id")
            node = self._node_by_run.pop(rid, None)
            partial = self._partial.pop(rid, "")
            if not node:
                return
            text = _text_from_llm_response(response) or partial
            self.tracker.set_by_node(node, text)
        except Exception:
            pass

    # alguns wrappers de chat emitem on_chat_model_end em vez de on_llm_end
    def on_chat_model_end(self, response, **kwargs) -> None:   # pragma: no cover
        self.on_llm_end(response, **kwargs)

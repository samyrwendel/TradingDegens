"""Raciocínio AO VIVO (task 008): a saída de cada agente é capturada do callback
do LLM e exposta progressivamente, com custo zero de LLM.

Cobre a ThinkingTracker (armazenamento/ordem/cap) e o ThinkingCallbackHandler
(correlação run_id→nó no start, texto no end, streaming por token).
"""

from types import SimpleNamespace

import pytest

from tradingagents.webui.progress import (
    ThinkingCallbackHandler,
    ThinkingTracker,
)

pytestmark = pytest.mark.unit


def _resp(text):
    """Fake LLMResult: generations[0][0].message.content (caminho do chat model)."""
    gen = SimpleNamespace(message=SimpleNamespace(content=text), text=text)
    return SimpleNamespace(generations=[[gen]])


def test_tracker_stores_and_orders_by_pipeline():
    t = ThinkingTracker()
    # chega fora de ordem; o snapshot devolve na ordem do pipeline
    t.set_by_node("Bull Researcher", "argumento de alta bem fundamentado aqui")
    t.set_by_node("Market Analyst", "leitura técnica do mercado com detalhes")
    snap = t.snapshot()
    ids = [c["id"] for c in snap]
    assert ids == ["Market Analyst", "Bull Researcher"], ids
    assert snap[0]["phase"] == "Analistas"
    assert snap[1]["debate"] is True
    assert snap[0]["len"] == len(snap[0]["text"])


def test_tracker_ignores_unknown_node_and_trivial_text():
    t = ThinkingTracker()
    t.set_by_node("tools_market", "x" * 50)     # nó não mapeado
    t.set_by_node("Market Analyst", "curto")    # < 8 chars → ruído
    assert t.snapshot() == []


def test_tracker_caps_huge_text():
    t = ThinkingTracker()
    t.set_by_node("News Analyst", "N" * 20000)
    txt = t.snapshot()[0]["text"]
    assert len(txt) <= 8001 + 1        # cap + reticências
    assert txt.endswith("…")


def test_callback_correlates_run_id_to_node_on_end():
    t = ThinkingTracker()
    h = ThinkingCallbackHandler(t)
    # start: registra o nó daquela chamada; end: atribui o texto ao nó
    h.on_chat_model_start({}, [], metadata={"langgraph_node": "News Analyst"}, run_id="r1")
    h.on_llm_end(_resp("relatório de notícias com macro e mercados de previsão"), run_id="r1")
    snap = t.snapshot()
    assert len(snap) == 1
    assert snap[0]["id"] == "News Analyst"
    assert "notícias" in snap[0]["text"]


def test_callback_streaming_tokens_grow_the_card():
    t = ThinkingTracker()
    h = ThinkingCallbackHandler(t)
    h.on_llm_start({}, [], metadata={"langgraph_node": "Market Analyst"}, run_id="r2")
    for tok in ["parte um ", "parte dois ", "parte três"]:
        h.on_llm_new_token(tok, run_id="r2")
    # antes mesmo do end, o card já cresceu com o texto em streaming
    assert "parte três" in t.snapshot()[0]["text"]
    h.on_llm_end(_resp("parte um parte dois parte três (final)"), run_id="r2")
    assert "(final)" in t.snapshot()[0]["text"]


def test_callback_without_node_is_noop():
    t = ThinkingTracker()
    h = ThinkingCallbackHandler(t)
    # end sem start correspondente (run_id desconhecido) → não quebra, não grava
    h.on_llm_end(_resp("texto órfão sem nó associado"), run_id="ghost")
    assert t.snapshot() == []

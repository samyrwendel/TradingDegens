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


# ── Atribuição por etapa (task 024, parte 1): qual LLM rodou cada etapa ──────────
def test_callback_captures_real_model_from_metadata():
    """O modelo REAL de cada etapa vem do metadata padrão do langchain
    (ls_provider/ls_model_name) no start — o que rodou, não o configurado."""
    t = ThinkingTracker()
    h = ThinkingCallbackHandler(t)
    h.on_chat_model_start(
        {}, [],
        metadata={"langgraph_node": "Portfolio Manager",
                  "ls_provider": "anthropic", "ls_model_name": "claude-sonnet-5"},
        run_id="r1")
    h.on_llm_end(_resp("veredito final do gestor de portfólio com a decisão"), run_id="r1")
    card = t.snapshot()[0]
    assert card["provider"] == "anthropic" and card["model"] == "claude-sonnet-5"
    rows = t.models_snapshot()
    assert rows == [{
        "node": "Portfolio Manager", "label": rows[0]["label"], "phase": "Risco",
        "order": rows[0]["order"], "provider": "anthropic", "model": "claude-sonnet-5",
        # timeframe por etapa (task 009): Portfolio Manager não opera num tempo gráfico
        "timeframe": None,
    }]


def test_model_falls_back_to_invocation_params():
    """Sem ls_* no metadata, cai nos invocation_params (model/model_name)."""
    t = ThinkingTracker()
    h = ThinkingCallbackHandler(t)
    h.on_llm_start(
        {}, [],
        metadata={"langgraph_node": "Market Analyst"},
        invocation_params={"model": "gpt-5.4-mini"},
        run_id="r2")
    h.on_llm_end(_resp("leitura técnica do mercado com bastante detalhe"), run_id="r2")
    card = t.snapshot()[0]
    assert card["model"] == "gpt-5.4-mini" and card["provider"] is None


def test_models_snapshot_ordered_and_only_real():
    """models_snapshot vem na ordem do pipeline e só traz nós que de fato rodaram
    (têm modelo) — nunca inventa atribuição pra etapa que não correu."""
    t = ThinkingTracker()
    # cross-provider (prova o formato da parte 2): analista openai, juiz anthropic
    t.set_model("Portfolio Manager", "anthropic", "claude-sonnet-5")
    t.set_model("Market Analyst", "openai", "gpt-5.4-mini")
    t.set_model("tools_market", "openai", "gpt-5.4-mini")   # nó não mapeado → ignora
    t.set_model("News Analyst", "google", None)             # sem modelo → não grava
    rows = t.models_snapshot()
    assert [(r["node"], r["provider"], r["model"]) for r in rows] == [
        ("Market Analyst", "openai", "gpt-5.4-mini"),
        ("Portfolio Manager", "anthropic", "claude-sonnet-5"),
    ]


def test_no_attribution_until_a_start_reports_a_model():
    """Card sem atribuição ainda (só texto, sem start com modelo) → provider/model
    None e models_snapshot vazio (honesto: não mostra selo do que não sabe)."""
    t = ThinkingTracker()
    t.set_by_node("Trader", "plano de execução do trader detalhado")
    card = t.snapshot()[0]
    assert card["provider"] is None and card["model"] is None
    assert t.models_snapshot() == []


def test_step_timeframe_only_where_it_applies():
    """Task 009 — selo de timeframe por etapa: Mercado = semanal · diário, Erick =
    4h · 15m; os demais nós não operam num tempo gráfico → None (sem selo). O TF sai
    tanto no snapshot ao vivo quanto no models_snapshot de auditoria."""
    t = ThinkingTracker()
    for node in ("Market Analyst", "Erick Analyst", "News Analyst", "Portfolio Manager"):
        t.set_by_node(node, f"leitura detalhada do no {node} para o teste")
        t.set_model(node, "openai", "gpt-5.4")
    tf = {it["id"]: it["timeframe"] for it in t.snapshot()}
    assert tf["Market Analyst"] == "semanal · diário"
    assert tf["Erick Analyst"] == "4h · 15m"
    assert tf["News Analyst"] is None and tf["Portfolio Manager"] is None
    # o rodapé de auditoria carrega o mesmo TF
    mtf = {r["node"]: r["timeframe"] for r in t.models_snapshot()}
    assert mtf["Market Analyst"] == "semanal · diário" and mtf["Erick Analyst"] == "4h · 15m"


def test_market_timeframe_stamps_intraday_reference_frame():
    """Task 009 — numa run intradiária o Mercado ancora o timing no frame de
    referência (some ao selo: semanal · diário · 4h). Diária/semanal não acrescenta."""
    t = ThinkingTracker(timeframe="4h")
    t.set_by_node("Market Analyst", "leitura do mercado no frame intradiario 4h")
    assert t.snapshot()[0]["timeframe"] == "semanal · diário · 4h"
    t1d = ThinkingTracker(timeframe="1d")
    t1d.set_by_node("Market Analyst", "leitura do mercado no frame diario padrao")
    assert t1d.snapshot()[0]["timeframe"] == "semanal · diário"

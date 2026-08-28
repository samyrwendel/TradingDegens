"""Rebobinar UMA etapa do checkpoint — o motor do "atualizar" (task 002 / DA-062).

O usuário olha uma análise que continuou de onde parou e quer DADO NOVO em uma das
fases, sem pagar as outras de novo. Isso exige duas coisas do checkpoint, provadas
aqui contra o LangGraph de verdade (não um dublê):

  1. ``completed_nodes``/``completed_reports`` sabem, só olhando o checkpoint, quais
     etapas já estão prontas — é isso que deixa o stepper pintá-las de VERDE numa
     retomada (o motor não re-executa nó concluído, então nenhum callback dispara);
  2. ``rewind_checkpoint`` volta a thread pra ANTES de uma etapa: ela re-roda, as
     ANTERIORES continuam vindo prontas (custo zero) e as posteriores re-rodam junto
     — elas foram julgadas em cima do dado que o usuário acabou de trocar.
"""

import tempfile
from typing import TypedDict

from langgraph.graph import END, StateGraph

from tradingagents.graph.checkpointer import (
    checkpoint_step,
    completed_nodes,
    completed_reports,
    get_checkpointer,
    node_output_channels,
    rewind_before_node,
    rewind_checkpoint,
    thread_id,
)

TICKER = "TEST"
DATE = "2026-08-27"


class _State(TypedDict):
    market_report: str
    news_report: str
    final_trade_decision: str


def _build(ran: list[str], gen: list[int] | None = None):
    """Grafo mínimo com os MESMOS canais do pipeline real, pra o mapa nó→canal ser
    exercitado de verdade (e não uma versão de teste que só ele conhece).

    ``gen`` numera a GERAÇÃO do texto (não zera com ``ran``), então "esta etapa
    produziu conteúdo novo" vira asserção dura em vez de coincidência de string.
    """
    tag = f"v{(gen or [1])[0]}"

    def market(_s):
        ran.append("market")
        return {"market_report": f"mercado {tag}"}

    def news(_s):
        ran.append("news")
        return {"news_report": f"notícias {tag}"}

    def judge(_s):
        ran.append("judge")
        return {"final_trade_decision": f"veredito {tag}"}

    b = StateGraph(_State)
    b.add_node("Market Analyst", market)
    b.add_node("News Analyst", news)
    b.add_node("Portfolio Manager", judge)
    b.set_entry_point("Market Analyst")
    b.add_edge("Market Analyst", "News Analyst")
    b.add_edge("News Analyst", "Portfolio Manager")
    b.add_edge("Portfolio Manager", END)
    return b


def _run(builder, tmpdir, cfg, state=None):
    with get_checkpointer(tmpdir, TICKER) as saver:
        return builder.compile(checkpointer=saver).invoke(state, config=cfg)


def _fresh(ran):
    tmpdir = tempfile.mkdtemp()
    cfg = {"configurable": {"thread_id": thread_id(TICKER, DATE)}}
    out = _run(_build(ran), tmpdir, cfg,
               {"market_report": "", "news_report": "", "final_trade_decision": ""})
    return tmpdir, cfg, out


# ------------------------------------------------ etapas prontas no checkpoint ---
def test_completed_nodes_reads_the_finished_stages():
    """Depois de uma run inteira, o checkpoint declara as três etapas prontas — é o
    que faz a retomada aparecer VERDE em vez de cinza."""
    ran: list[str] = []
    tmpdir, _cfg, _out = _fresh(ran)
    done = completed_nodes(tmpdir, TICKER, DATE)
    assert done == ["Market Analyst", "News Analyst", "Portfolio Manager"]
    reports = completed_reports(tmpdir, TICKER, DATE)
    assert reports["Market Analyst"] == "mercado v1"
    assert reports["Portfolio Manager"] == "veredito v1"


def test_completed_nodes_empty_without_checkpoint():
    """Sem checkpoint não há etapa pronta — nunca inventa progresso."""
    assert completed_nodes(tempfile.mkdtemp(), TICKER, DATE) == []
    assert completed_reports(tempfile.mkdtemp(), TICKER, DATE) == {}


def test_empty_report_does_not_count_as_done():
    """Um analista em meio a tool-loop escreve o relatório VAZIO antes do real: canal
    presente mas vazio NÃO é etapa concluída."""
    ran: list[str] = []
    tmpdir = tempfile.mkdtemp()
    cfg = {"configurable": {"thread_id": thread_id(TICKER, DATE)}}

    def only_market(_s):
        ran.append("market")
        return {"market_report": ""}          # ainda chamando ferramenta

    b = StateGraph(_State)
    b.add_node("Market Analyst", only_market)
    b.set_entry_point("Market Analyst")
    b.add_edge("Market Analyst", END)
    _run(b, tmpdir, cfg,
         {"market_report": "", "news_report": "", "final_trade_decision": ""})
    assert completed_nodes(tmpdir, TICKER, DATE) == []


# ------------------------------------------------------------------ rebobinar ----
def test_rewind_reruns_only_the_stage_and_what_depends_on_it():
    """Rebobinar antes de Notícias: Mercado volta PRONTO (não re-roda, custo zero),
    Notícias re-roda com dado novo e o veredito re-roda porque dependia dele."""
    ran: list[str] = []
    tmpdir, cfg, first = _fresh(ran)
    assert ran == ["market", "news", "judge"]

    head = rewind_before_node(tmpdir, TICKER, DATE, node="News Analyst")
    assert head, "deveria ter rebobinado"
    # o checkpoint agora só conhece a etapa anterior
    assert completed_nodes(tmpdir, TICKER, DATE) == ["Market Analyst"]

    ran.clear()
    second = _run(_build(ran, [2]), tmpdir, cfg, None)   # None = continua a thread
    assert ran == ["news", "judge"]                 # mercado NÃO re-rodou
    assert second["market_report"] == first["market_report"]
    assert second["news_report"] != first["news_report"]
    assert second["final_trade_decision"] != first["final_trade_decision"]


def test_rewind_first_stage_reruns_everything():
    """Atualizar a PRIMEIRA etapa não tem nada a preservar — tudo re-roda, e é isso
    mesmo que o usuário pediu."""
    ran: list[str] = []
    tmpdir, cfg, _first = _fresh(ran)
    assert rewind_before_node(tmpdir, TICKER, DATE, node="Market Analyst")
    ran.clear()
    _run(_build(ran), tmpdir, cfg, None)
    assert ran == ["market", "news", "judge"]


def test_rewind_is_noop_for_a_stage_that_never_ran():
    """Etapa que nunca rodou nesta thread não rebobina nada — o checkpoint fica
    intacto (nada de destruir trabalho por um clique em etapa errada)."""
    ran: list[str] = []
    tmpdir, _cfg, _first = _fresh(ran)
    step_before = checkpoint_step(tmpdir, TICKER, DATE)
    assert rewind_before_node(tmpdir, TICKER, DATE, node="Erick Analyst") is None
    assert checkpoint_step(tmpdir, TICKER, DATE) == step_before
    assert len(completed_nodes(tmpdir, TICKER, DATE)) == 3


def test_rewind_without_checkpoint_or_channels_is_none():
    """Sem checkpoint, ou com nó desconhecido, o rebobinar recusa em vez de adivinhar."""
    assert rewind_before_node(tempfile.mkdtemp(), TICKER, DATE, node="Market Analyst") is None
    ran: list[str] = []
    tmpdir, _cfg, _ = _fresh(ran)
    assert rewind_checkpoint(tmpdir, TICKER, DATE, channels=[]) is None
    assert node_output_channels("Nó Inventado") == ()


def test_rewind_respects_the_graph_signature():
    """A assinatura (seleção de analistas/TF/profundidade) faz parte da thread: um
    rebobinar sob OUTRA forma de grafo não toca no checkpoint da run real."""
    ran: list[str] = []
    tmpdir, _cfg, _ = _fresh(ran)
    assert rewind_before_node(tmpdir, TICKER, DATE, "tf=4h", node="News Analyst") is None
    assert len(completed_nodes(tmpdir, TICKER, DATE)) == 3

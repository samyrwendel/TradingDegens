"""Progress tracking maps LangGraph nodes to ordered, non-regressing stages."""

from tradingagents.webui.progress import (
    ProgressCallbackHandler,
    ProgressTracker,
    build_plan,
    stage_for_node,
)


def test_build_plan_stock_has_four_analysts():
    plan = build_plan(["market", "social", "news", "fundamentals"])
    labels = [p["node"] for p in plan]
    assert labels[:4] == [
        "Market Analyst", "Sentiment Analyst", "News Analyst", "Fundamentals Analyst",
    ]
    assert labels[-1] == "Portfolio Manager"
    # orders strictly increase
    orders = [p["order"] for p in plan]
    assert orders == sorted(orders)


def test_build_plan_crypto_drops_fundamentals():
    plan = build_plan(["market", "social", "news"])
    nodes = [p["node"] for p in plan]
    assert "Fundamentals Analyst" not in nodes
    assert "Market Analyst" in nodes


def test_stage_for_node_unknown_returns_none():
    assert stage_for_node("tools_market") is None
    assert stage_for_node("Msg Clear Market") is None
    assert stage_for_node("Bull Researcher")[2] == "Debate"


def test_tracker_advances_and_never_regresses():
    t = ProgressTracker(["market", "social", "news", "fundamentals"])
    t.note_node("Market Analyst")
    first = t.snapshot()
    assert first["phase"] == "Analistas"
    assert first["percent"] > 0

    t.note_node("Bull Researcher")
    mid = t.snapshot()
    assert mid["index"] > first["index"]

    # a late tool call for an earlier node must not move the bar backwards
    t.note_node("Market Analyst")
    assert t.snapshot()["index"] == mid["index"]


def test_tracker_mark_done_is_full():
    t = ProgressTracker(["market", "news"])
    t.note_node("Market Analyst")
    t.mark_done()
    snap = t.snapshot()
    assert snap["percent"] == 100
    assert snap["phase"] == "Concluído"


def test_tracker_reached_accumulates_stages():
    t = ProgressTracker(["market", "news"])
    for node in ["Market Analyst", "News Analyst", "Bull Researcher"]:
        t.note_node(node)
    reached = [r["label"] for r in t.snapshot()["reached"]]
    assert len(reached) == 3


def test_callback_handler_reads_langgraph_node_metadata():
    t = ProgressTracker(["market", "news"])
    h = ProgressCallbackHandler(t)
    h.on_chat_model_start({}, [], metadata={"langgraph_node": "News Analyst"})
    assert "Notícias" in t.snapshot()["label"]


def test_callback_handler_swallows_missing_metadata():
    t = ProgressTracker(["market"])
    h = ProgressCallbackHandler(t)
    # must not raise even with no metadata at all
    h.on_tool_start({}, "x")
    h.on_llm_start({}, [], metadata=None)
    assert t.snapshot()["index"] == 0

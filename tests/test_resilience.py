"""Pipeline resilience: a failing phase degrades instead of killing the run (task 014).

Covers the three consumer-facing guarantees:
  * an analyst node that raises is retried once (transient failures recover);
  * if it still fails, it degrades to an "indisponível" report and the run goes on;
  * a bad/unsupported indicator never aborts — get_indicators returns the message.
"""

from langchain_core.messages import AIMessage

from tradingagents.agents.utils import technical_indicators_tools as tit
from tradingagents.graph.resilience import make_resilient_analyst, tool_error_message


def test_resilient_analyst_retries_once_then_succeeds():
    """First call raises (transient), the retry succeeds — the run gets the real
    result, proving retry 1x (criterion 3)."""
    calls = {"n": 0}

    def flaky(state):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient blip")
        return {"messages": [AIMessage(content="ok")], "market_report": "real report"}

    node = make_resilient_analyst(flaky, "market_report", "Market Analyst")
    out = node({"messages": []})
    assert calls["n"] == 2                      # retried exactly once
    assert out["market_report"] == "real report"


def test_resilient_analyst_degrades_after_all_attempts():
    """Every attempt fails → the analyst degrades to an 'indisponível' report and
    returns a tool-call-free message so the graph routes on (criterion 2)."""
    def always_fails(state):
        raise RuntimeError("boom")

    node = make_resilient_analyst(always_fails, "news_report", "News Analyst")
    out = node({"messages": []})
    assert "indisponível" in out["news_report"].lower()
    assert "News Analyst" in out["news_report"]
    # a valid, routable state update: an AIMessage with NO tool calls
    msg = out["messages"][0]
    assert isinstance(msg, AIMessage)
    assert not msg.tool_calls
    # structured entry so the UI can NAME the source + offer a re-eval (task 015)
    ds = out["degraded_sources"]
    assert len(ds) == 1
    assert ds[0]["label"] == "News Analyst"
    assert ds[0]["report_key"] == "news_report"
    assert "boom" in ds[0]["reason"]


def test_resilient_analyst_passes_through_success():
    def ok(state):
        return {"messages": [AIMessage(content="hi")], "market_report": "R"}

    node = make_resilient_analyst(ok, "market_report", "Market Analyst")
    assert node({"messages": []})["market_report"] == "R"


def test_degraded_sources_accumulate_through_a_real_graph():
    """End-to-end through LangGraph: a failing analyst degrades and its entry is
    merged onto the run state via the list-add reducer, while the run completes and
    a healthy analyst is untouched (task 015)."""
    from langgraph.graph import END, START, StateGraph

    from tradingagents.agents.utils.agent_states import AgentState

    def failing(state):
        raise RuntimeError("src down")

    def healthy(state):
        return {"market_report": "ok"}

    g = StateGraph(AgentState)
    g.add_node("A", make_resilient_analyst(failing, "news_report", "News Analyst"))
    g.add_node("B", make_resilient_analyst(healthy, "market_report", "Market Analyst"))
    g.add_edge(START, "A")
    g.add_edge("A", "B")
    g.add_edge("B", END)
    app = g.compile()

    out = app.invoke({"messages": [], "degraded_sources": []})
    labels = [d["label"] for d in out["degraded_sources"]]
    assert labels == ["News Analyst"]              # only the failing one, once
    assert out["news_report"].startswith("⚠️")     # degraded report is explicit
    assert out["market_report"] == "ok"            # healthy analyst untouched


def test_tool_error_message_is_a_string():
    msg = tool_error_message(ValueError("Indicator ema is not supported"))
    assert "indisponível" in msg.lower()
    assert "ema" in msg


def test_get_indicators_never_aborts_on_bad_indicator(monkeypatch):
    """A cache-wrapped RuntimeError (or any error) for one indicator must be
    swallowed into the returned text, never raised (criterion 1)."""
    def boom(method, symbol, ind, curr_date, look_back_days):
        raise RuntimeError("[ta_datacache cached failure: ValueError] Indicator ema is not supported")

    monkeypatch.setattr(tit, "route_to_vendor", boom)
    out = tit.get_indicators.invoke(
        {"symbol": "BTC-USD", "indicator": "ema", "curr_date": "2026-08-22"}
    )
    assert "indisponível" in out.lower()
    assert "ema" in out.lower()


def test_get_indicators_reports_each_of_multiple(monkeypatch):
    """One bad indicator in a comma list doesn't sink the others."""
    def selective(method, symbol, ind, curr_date, look_back_days):
        if ind == "ema":
            raise RuntimeError("Indicator ema is not supported")
        return f"{ind}: 42"

    monkeypatch.setattr(tit, "route_to_vendor", selective)
    out = tit.get_indicators.invoke(
        {"symbol": "AAPL", "indicator": "rsi, ema", "curr_date": "2026-08-22"}
    )
    assert "rsi: 42" in out
    assert "indisponível" in out.lower()

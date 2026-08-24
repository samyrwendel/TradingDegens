"""Verdict-per-timeframe wiring (task 012).

The reference timeframe must reach the MARKET analyst (and only it): the initial
state carries it, the price-structure coverage detects the setup on that frame,
and the market-analyst node forwards the state's timeframe to that coverage. The
fundamental/news/sentiment analysts stay timeframe-agnostic.
"""

from types import SimpleNamespace

from langchain_core.runnables import RunnableLambda

import tradingagents.agents.analysts.market_analyst as ma
from tradingagents.agents.utils import price_structure_coverage as psc
from tradingagents.graph.propagation import Propagator


def test_create_initial_state_carries_timeframe():
    st = Propagator().create_initial_state("BTC-USD", "2026-08-22",
                                           asset_type="crypto", timeframe="4h")
    assert st["timeframe"] == "4h"


def test_create_initial_state_defaults_daily():
    st = Propagator().create_initial_state("AAPL", "2026-08-22")
    assert st["timeframe"] == "1d"


def test_price_structure_coverage_forwards_timeframe(monkeypatch):
    seen = {}

    def _fake_section(symbol, curr_date, timeframe="1d"):
        seen["tf"] = timeframe
        return "## Estrutura de preço / setups — X\n\nok"

    monkeypatch.setattr(psc, "build_price_structure_section", _fake_section)
    out = psc.ensure_price_structure_coverage("corpo", "BTC-USD", "2026-08-22", "15m")
    assert seen["tf"] == "15m"
    assert "Estrutura de preço" in out


def test_market_analyst_forwards_state_timeframe(monkeypatch):
    """The node reads state['timeframe'] and hands it to the price-structure
    coverage — so the setup the debate sees is detected on the chosen frame."""
    captured = {}

    # keep the node hermetic: no network coverage calls, no real LLM
    monkeypatch.setattr(ma, "ensure_multi_timeframe_coverage", lambda r, s, d: r)

    def _rec(report, symbol, curr_date, timeframe="1d"):
        captured["tf"] = timeframe
        return report

    monkeypatch.setattr(ma, "ensure_price_structure_coverage", _rec)

    class _LLM:
        def bind_tools(self, tools):
            return RunnableLambda(
                lambda _msgs: SimpleNamespace(content="corpo do relatório", tool_calls=[])
            )

    node = ma.create_market_analyst(_LLM())
    state = {
        "trade_date": "2026-08-22",
        "company_of_interest": "AAPL",
        "asset_type": "stock",      # avoids the crypto-only coverage branch
        "timeframe": "1w",
        "instrument_context": "",
        "messages": [("human", "AAPL")],
    }
    out = node(state)
    assert captured["tf"] == "1w"
    assert "market_report" in out

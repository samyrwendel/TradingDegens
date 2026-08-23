"""Anti look-ahead date guard: a past-dated run must never pull posterior data.

The core acceptance test (fork brief #2): with the run's base date pinned to a
past date, a data tool asked for data "up to today" is clamped down to the base
date before the vendor ever sees it — so the vendor cannot return posterior data.
"""
from typing import Annotated

import pytest
from langchain_core.tools import tool

from tradingagents.agents.utils import macro_data_tools as mdt
from tradingagents.agents.utils import news_data_tools as ndt
from tradingagents.agents.utils.date_guard import (
    base_date,
    clamp,
    get_base_date,
    guard_dates,
)


# ------------------------------------------------------------------- clamp -----
@pytest.mark.unit
def test_clamp_is_noop_without_base():
    assert get_base_date() is None
    assert clamp("2099-01-01") == "2099-01-01"


@pytest.mark.unit
def test_clamp_within_base_context():
    with base_date("2021-01-05"):
        assert get_base_date().isoformat() == "2021-01-05"
        assert clamp("2026-08-23") == "2021-01-05"   # future -> clamped
        assert clamp("2020-06-06") == "2020-06-06"   # past -> unchanged
        assert clamp("2021-01-05") == "2021-01-05"   # equal -> unchanged
        assert clamp("2026/12/31") == "2021-01-05"   # loose format normalized+clamped
        assert clamp("garbage") == "garbage"          # unparseable -> left for vendor
    assert get_base_date() is None                     # context resets


# ------------------------------------------------------------- decorator -------
@pytest.mark.unit
def test_guard_dates_preserves_tool_schema_and_clamps():
    seen = {}

    @tool
    @guard_dates("start_date", "end_date")
    def fake_news(
        ticker: Annotated[str, "Ticker"],
        start_date: Annotated[str, "Start"],
        end_date: Annotated[str, "End"],
    ) -> str:
        """Fake."""
        seen["start"], seen["end"] = start_date, end_date
        return "ok"

    # LangChain still sees the original parameters.
    assert list(fake_news.args.keys()) == ["ticker", "start_date", "end_date"]

    with base_date("2021-03-15"):
        fake_news.invoke(
            {"ticker": "BE", "start_date": "2021-03-01", "end_date": "2026-08-23"}
        )
    assert seen["start"] == "2021-03-01"   # inside window, kept
    assert seen["end"] == "2021-03-15"     # look-ahead end clamped to base


# ----------------------------------------------------- real tools clamp --------
@pytest.mark.unit
def test_macro_tool_clamps_curr_date(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        mdt, "route_to_vendor",
        lambda method, *args, **kw: captured.update(method=method, args=args) or "ok",
    )
    with base_date("2021-03-15"):
        mdt.get_macro_indicators.invoke({"indicator": "cpi", "curr_date": "2026-08-23"})
    assert captured["args"][1] == "2021-03-15"   # no posterior macro data


@pytest.mark.unit
def test_news_tool_clamps_end_date(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        ndt, "route_to_vendor",
        lambda method, *args, **kw: captured.update(method=method, args=args) or "ok",
    )
    with base_date("2021-03-15"):
        ndt.get_news.invoke(
            {"ticker": "BE", "start_date": "2021-03-01", "end_date": "2026-08-23"}
        )
    assert captured["args"] == ("BE", "2021-03-01", "2021-03-15")


@pytest.mark.unit
def test_live_run_is_unaffected(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        mdt, "route_to_vendor",
        lambda method, *args, **kw: captured.update(args=args) or "ok",
    )
    # No base context (a live run pins base==today, which never clamps a
    # today-or-earlier request); here we assert the no-base path is a pure no-op.
    mdt.get_macro_indicators.invoke({"indicator": "cpi", "curr_date": "2026-08-23"})
    assert captured["args"][1] == "2026-08-23"

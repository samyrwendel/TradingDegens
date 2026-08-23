from typing import Annotated

from langchain_core.tools import tool

from tradingagents.agents.utils.date_guard import guard_dates
from tradingagents.dataflows.multi_timeframe import build_timeframe_summary


@tool
@guard_dates("curr_date")
def get_price_timeframes(
    symbol: Annotated[str, "ticker symbol of the instrument, e.g. AAPL or BTC-USD"],
    curr_date: Annotated[str, "the current trading date, YYYY-mm-dd"],
) -> str:
    """Weekly **and** daily trend read for the instrument, with a convergence verdict.

    Returns the higher-timeframe (weekly) trend and the lower-timeframe (daily)
    trend — each with last price, SMA stack, and momentum — plus an explicit
    statement of whether the two timeframes converge or diverge. Call this to
    ground any multi-timeframe claim: the weekly frame sets the dominant trend,
    the daily frame the timing, and a divergence between them is a signal.

    Args:
        symbol (str): Ticker symbol, e.g. AAPL, BTC-USD
        curr_date (str): The current trading date, YYYY-mm-dd

    Returns:
        str: A markdown weekly+daily trend report with a convergence verdict.
    """
    return build_timeframe_summary(symbol, curr_date)

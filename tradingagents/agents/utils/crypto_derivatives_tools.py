from typing import Annotated

from langchain_core.tools import tool

from tradingagents.agents.utils.date_guard import guard_dates
from tradingagents.dataflows.interface import route_to_vendor


@tool
@guard_dates("curr_date")
def get_crypto_derivatives(
    symbol: Annotated[str, "crypto ticker, e.g. BTC-USD, ETH-USD, LINK-USD"],
    curr_date: Annotated[str, "the current trading date, YYYY-mm-dd"],
) -> str:
    """Real crypto derivatives — funding rate, open interest, and liquidations.

    This is what yfinance cannot see and what actually moves a perpetual-driven
    crypto asset: the funding rate (crowd positioning / carry), open interest
    (leveraged money in the book), and recent liquidations (the fuel of violent
    moves). Every figure names its keyless public source (Hyperliquid, Binance
    USDⓈ-M, OKX); a source that is down is marked unavailable rather than
    fabricated. Call this for any crypto asset before judging momentum or risk.

    Args:
        symbol (str): Crypto ticker, e.g. BTC-USD, ETH-USD, LINK-USD
        curr_date (str): The current trading date, YYYY-mm-dd

    Returns:
        str: A markdown crypto-derivatives report (funding, OI, liquidations).
    """
    return route_to_vendor("get_crypto_derivatives", symbol, curr_date)

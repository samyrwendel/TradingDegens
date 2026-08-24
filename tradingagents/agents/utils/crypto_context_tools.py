from typing import Annotated

from langchain_core.tools import tool

from tradingagents.agents.utils.crypto_context_coverage import build_crypto_context
from tradingagents.agents.utils.date_guard import guard_dates


@tool
@guard_dates("curr_date")
def get_crypto_context(
    symbol: Annotated[str, "crypto ticker, e.g. BTC-USD, ETH-USD, SOL-USD"],
    curr_date: Annotated[str, "the current trading date, YYYY-mm-dd"],
) -> str:
    """Crypto network context yfinance can't see — on-chain, spot-ETF flows, Fear & Greed.

    Three feeds the modeled decision process treats as first-class for a crypto
    call: on-chain network health (hashrate, halving countdown, market
    dominance, stablecoin market cap — MVRV/cost-basis declared unavailable when
    it is paid-key-only, never proxied), spot BTC/ETH ETF daily net flow
    (Farside), and the crypto Fear & Greed index (alternative.me). Every figure
    names its keyless public source; a source that is down is marked unavailable
    rather than fabricated; a past date reads real history, never a look-ahead.
    Call this for any crypto asset before judging positioning or sentiment.

    Args:
        symbol (str): Crypto ticker, e.g. BTC-USD, ETH-USD
        curr_date (str): The current trading date, YYYY-mm-dd

    Returns:
        str: A markdown report with the on-chain, ETF-flow and Fear & Greed sections.
    """
    return build_crypto_context(symbol, curr_date)

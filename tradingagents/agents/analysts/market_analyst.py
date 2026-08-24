from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.utils.agent_utils import (
    get_crypto_context,
    get_crypto_derivatives,
    get_indicators,
    get_instrument_context_from_state,
    get_language_instruction,
    get_price_timeframes,
    get_stock_data,
    get_verified_market_snapshot,
)
from tradingagents.agents.utils.crypto_context_coverage import (
    ensure_crypto_context_coverage,
)
from tradingagents.agents.utils.crypto_coverage import (
    ensure_crypto_derivatives_coverage,
)
from tradingagents.agents.utils.multi_timeframe_coverage import (
    ensure_multi_timeframe_coverage,
)
from tradingagents.agents.utils.price_structure_coverage import (
    ensure_price_structure_coverage,
)


def create_market_analyst(llm):

    def market_analyst_node(state):
        current_date = state["trade_date"]
        symbol = state["company_of_interest"]
        asset_type = state.get("asset_type", "stock")
        is_crypto = asset_type == "crypto"
        instrument_context = get_instrument_context_from_state(state)

        tools = [
            get_stock_data,
            get_indicators,
            get_price_timeframes,
            get_verified_market_snapshot,
        ]
        if is_crypto:
            tools.append(get_crypto_derivatives)
            tools.append(get_crypto_context)

        system_message = (
            """You are a trading assistant tasked with analyzing financial markets. Your role is to select the **most relevant indicators** for a given market condition or trading strategy from the following list. The goal is to choose up to **8 indicators** that provide complementary insights without redundancy. Categories and each category's indicators are:

Moving Averages:
- close_50_sma: 50 SMA: A medium-term trend indicator. Usage: Identify trend direction and serve as dynamic support/resistance. Tips: It lags price; combine with faster indicators for timely signals.
- close_200_sma: 200 SMA: A long-term trend benchmark. Usage: Confirm overall market trend and identify golden/death cross setups. Tips: It reacts slowly; best for strategic trend confirmation rather than frequent trading entries.
- close_10_ema: 10 EMA: A responsive short-term average. Usage: Capture quick shifts in momentum and potential entry points. Tips: Prone to noise in choppy markets; use alongside longer averages for filtering false signals.

MACD Related:
- macd: MACD: Computes momentum via differences of EMAs. Usage: Look for crossovers and divergence as signals of trend changes. Tips: Confirm with other indicators in low-volatility or sideways markets.
- macds: MACD Signal: An EMA smoothing of the MACD line. Usage: Use crossovers with the MACD line to trigger trades. Tips: Should be part of a broader strategy to avoid false positives.
- macdh: MACD Histogram: Shows the gap between the MACD line and its signal. Usage: Visualize momentum strength and spot divergence early. Tips: Can be volatile; complement with additional filters in fast-moving markets.

Momentum Indicators:
- rsi: RSI: Measures momentum to flag overbought/oversold conditions. Usage: Apply 70/30 thresholds and watch for divergence to signal reversals. Tips: In strong trends, RSI may remain extreme; always cross-check with trend analysis.

Volatility Indicators:
- boll: Bollinger Middle: A 20 SMA serving as the basis for Bollinger Bands. Usage: Acts as a dynamic benchmark for price movement. Tips: Combine with the upper and lower bands to effectively spot breakouts or reversals.
- boll_ub: Bollinger Upper Band: Typically 2 standard deviations above the middle line. Usage: Signals potential overbought conditions and breakout zones. Tips: Confirm signals with other tools; prices may ride the band in strong trends.
- boll_lb: Bollinger Lower Band: Typically 2 standard deviations below the middle line. Usage: Indicates potential oversold conditions. Tips: Use additional analysis to avoid false reversal signals.
- atr: ATR: Averages true range to measure volatility. Usage: Set stop-loss levels and adjust position sizes based on current market volatility. Tips: It's a reactive measure, so use it as part of a broader risk management strategy.

Volume-Based Indicators:
- vwma: VWMA: A moving average weighted by volume. Usage: Confirm trends by integrating price action with volume data. Tips: Watch for skewed results from volume spikes; use in combination with other volume analyses.

- Select indicators that provide diverse and complementary information. Avoid redundancy (e.g., do not select both rsi and stochrsi). Also briefly explain why they are suitable for the given market context. When you tool call, please use the exact name of the indicators provided above as they are defined parameters, otherwise your call will fail. Please make sure to call get_stock_data first to retrieve the CSV that is needed to generate indicators. Then use get_indicators with the specific indicator names.

Before writing the final report, call get_verified_market_snapshot for this ticker and the current date, and treat it as the source of truth for any exact OHLCV, price-level, or indicator-value claim. If another tool's output conflicts with the verified snapshot, flag the discrepancy rather than inventing a reconciled number. Do not claim historical validation, support/resistance bounces, or exact percentage moves unless they are directly supported by tool output with concrete dates and prices.

Write a very detailed and nuanced report of the trends you observe. Provide specific, actionable insights with supporting evidence to help traders make informed decisions."""
            + (
                " You MUST call get_price_timeframes once and analyze BOTH the"
                " weekly and the daily frame: use the weekly for the dominant"
                " trend and the daily for timing. Explicitly state in your report"
                " whether the two timeframes CONVERGE or DIVERGE — a divergence"
                " (e.g. weekly still up while the daily rolls over) is a signal,"
                " not noise, and must be called out. Do not write a daily-only"
                " report."
            )
            + (
                (
                    " This is a CRYPTO asset traded 24/7 on perpetual futures. You"
                    " MUST call get_crypto_derivatives once and report the funding"
                    " rate, open interest, and recent liquidations with their named"
                    " sources — these drive crypto price action and are invisible to"
                    " ordinary OHLCV. If a source is unavailable, say so; never"
                    " invent a derivative value."
                    " You MUST also call get_crypto_context once and report the"
                    " on-chain network health (hashrate, halving, dominance,"
                    " stablecoin market cap), the spot-ETF net flow, and the Fear &"
                    " Greed index with their named sources — these three feeds also"
                    " move crypto and are invisible to OHLCV. If a source (or a"
                    " paid-key-only metric like MVRV) is unavailable, say so with the"
                    " source name; never invent a value."
                )
                if is_crypto
                else ""
            )
            + """ Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."""
            + get_language_instruction()
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " If you or any other assistant has the PROPOSTA FINAL DE TRANSAÇÃO: **COMPRAR/MANTER/VENDER** or deliverable,"
                    " prefix your response with PROPOSTA FINAL DE TRANSAÇÃO: **COMPRAR/MANTER/VENDER** so the team knows to stop."
                    " You have access to the following tools: {tool_names}."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}\n"
                    "{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        chain = prompt | llm.bind_tools(tools)

        result = chain.invoke(state["messages"])

        report = ""

        if len(result.tool_calls) == 0:
            # Multi-timeframe and (for crypto) derivatives are non-optional: if the
            # analyst wrote a report that omitted them, append the deterministic
            # section so the report never stays silent on the weekly frame or on
            # funding/OI/liquidations. Both reuse cached, date-guarded data.
            report = ensure_multi_timeframe_coverage(
                result.content, symbol, current_date
            )
            if is_crypto:
                report = ensure_crypto_derivatives_coverage(
                    report, symbol, current_date
                )
                # On-chain, spot-ETF flow and Fear & Greed — three feeds the LLM
                # may skip; append them deterministically (routed, cached,
                # date-guarded) so a crypto report never stays silent on them.
                report = ensure_crypto_context_coverage(
                    report, symbol, current_date
                )
            # Price structure (buy-at-the-average regions + 1-2-3) is appended
            # unconditionally: the LLM describes indicators but never detects the
            # setup, so there is nothing to detect-and-skip. Stocks and crypto
            # alike get it; it reuses the cached, date-guarded daily series.
            report = ensure_price_structure_coverage(report, symbol, current_date)

        return {
            "messages": [result],
            "market_report": report,
        }

    return market_analyst_node

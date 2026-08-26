from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
)
from tradingagents.agents.utils.debate_utils import (
    clip_report,
    degraded_note,
    invoke_debate_turn,
)
from tradingagents.dataflows.config import get_config


def create_bear_researcher(llm):
    def bear_node(state) -> dict:
        config = get_config()
        report_clip = config.get("debate_report_clip_chars", 0)
        history_clip = config.get("debate_history_clip_chars", 0)

        investment_debate_state = state["investment_debate_state"]
        history = clip_report(investment_debate_state.get("history", ""), history_clip)
        bear_history = investment_debate_state.get("bear_history", "")

        current_response = investment_debate_state.get("current_response", "")
        market_research_report = clip_report(state["market_report"], report_clip)
        sentiment_report = clip_report(state["sentiment_report"], report_clip)
        news_report = clip_report(state["news_report"], report_clip)
        fundamentals_report = clip_report(state["fundamentals_report"], report_clip)
        instrument_context = get_instrument_context_from_state(state)
        asset_type = state.get("asset_type", "stock")
        target_label = "stock" if asset_type == "stock" else "asset"
        fundamentals_label = (
            "Company fundamentals report"
            if asset_type == "stock"
            else "Asset fundamentals report (may be unavailable for crypto)"
        )

        prompt = f"""You are a Bear Analyst making the case against investing in the {target_label}. Your goal is to present a well-reasoned argument emphasizing risks, challenges, and negative indicators. Leverage the provided research and data to highlight potential downsides and counter bullish arguments effectively.

Key points to focus on:

- Risks and Challenges: Highlight factors like market saturation, financial instability, or macroeconomic threats that could hinder the stock's performance.
- Competitive Weaknesses: Emphasize vulnerabilities such as weaker market positioning, declining innovation, or threats from competitors.
- Negative Indicators: Use evidence from financial data, market trends, or recent adverse news to support your position.
- Bull Counterpoints: Critically analyze the bull argument with specific data and sound reasoning, exposing weaknesses or over-optimistic assumptions.
- Directness: Open with your strongest argument or the decisive data point. NO greeting, NO addressing the bull analyst by title, NO preamble or theatrical framing ("Dear colleagues", "my esteemed colleague", "opening the debate", "let me show you"). Rebut the bull's points with specific data and reasoning; make every sentence carry information (a number, a mechanism, a counter), not courtesy.

Resources available:

{instrument_context}
Market research report: {market_research_report}
Social media sentiment report: {sentiment_report}
Latest world affairs news: {news_report}
{fundamentals_label}: {fundamentals_report}
Conversation history of the debate: {history}
Last bull argument: {current_response}
Use this information to deliver a compelling bear argument and refute the bull's claims — straight to the substance, no rhetorical preamble or greeting.
""" + get_language_instruction()

        content, report = invoke_debate_turn(
            llm, prompt, speaker="Bear Researcher", config=config
        )

        argument = f"Analista de Baixa (bear): {content}"

        new_investment_debate_state = {
            "history": investment_debate_state.get("history", "") + "\n" + argument,
            "bear_history": bear_history + "\n" + argument,
            "bull_history": investment_debate_state.get("bull_history", ""),
            "current_response": argument,
            "count": investment_debate_state["count"] + 1,
        }

        result = {"investment_debate_state": new_investment_debate_state}
        note = degraded_note("Bear Researcher", report)
        if note:
            result["degraded_sources"] = [note]
        return result

    return bear_node

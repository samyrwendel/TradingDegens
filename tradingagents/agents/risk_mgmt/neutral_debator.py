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


def create_neutral_debator(llm):
    def neutral_node(state) -> dict:
        config = get_config()
        report_clip = config.get("debate_report_clip_chars", 0)
        history_clip = config.get("debate_history_clip_chars", 0)

        risk_debate_state = state["risk_debate_state"]
        history = clip_report(risk_debate_state.get("history", ""), history_clip)
        neutral_history = risk_debate_state.get("neutral_history", "")

        current_aggressive_response = risk_debate_state.get("current_aggressive_response", "")
        current_conservative_response = risk_debate_state.get("current_conservative_response", "")

        market_research_report = clip_report(state["market_report"], report_clip)
        sentiment_report = clip_report(state["sentiment_report"], report_clip)
        news_report = clip_report(state["news_report"], report_clip)
        fundamentals_report = clip_report(state["fundamentals_report"], report_clip)
        instrument_context = get_instrument_context_from_state(state)

        trader_decision = state["trader_investment_plan"]

        prompt = f"""As the Neutral Risk Analyst, your role is to provide a balanced perspective, weighing both the potential benefits and risks of the trader's decision or plan. You prioritize a well-rounded approach, evaluating the upsides and downsides while factoring in broader market trends, potential economic shifts, and diversification strategies.Here is the trader's decision:

{trader_decision}

Your task is to challenge both the Aggressive and Conservative Analysts, pointing out where each perspective may be overly optimistic or overly cautious. Use insights from the following data sources to support a moderate, sustainable strategy to adjust the trader's decision:

{instrument_context}
Market Research Report: {market_research_report}
Social Media Sentiment Report: {sentiment_report}
Latest World Affairs Report: {news_report}
Company Fundamentals Report: {fundamentals_report}
Here is the current conversation history: {history} Here is the last response from the aggressive analyst: {current_aggressive_response} Here is the last response from the conservative analyst: {current_conservative_response}. If there are no responses from the other viewpoints yet, present your own argument based on the available data.

Engage actively by analyzing both sides critically, addressing weaknesses in the aggressive and conservative arguments to advocate for a more balanced approach. Challenge each of their points with specific data to show why a moderate risk strategy offers growth potential while safeguarding against extreme volatility. Open directly with your argument or the decisive data — NO greeting, NO preamble, NO addressing the other analysts by title ("Dear colleagues", "my esteemed colleague", "opening the debate"). Write densely so every sentence carries information, without any special formatting.""" + get_language_instruction()

        content, report = invoke_debate_turn(
            llm, prompt, speaker="Neutral Analyst", config=config
        )

        argument = f"Neutral Analyst: {content}"

        new_risk_debate_state = {
            "history": risk_debate_state.get("history", "") + "\n" + argument,
            "aggressive_history": risk_debate_state.get("aggressive_history", ""),
            "conservative_history": risk_debate_state.get("conservative_history", ""),
            "neutral_history": neutral_history + "\n" + argument,
            "latest_speaker": "Neutral",
            "current_aggressive_response": risk_debate_state.get(
                "current_aggressive_response", ""
            ),
            "current_conservative_response": risk_debate_state.get("current_conservative_response", ""),
            "current_neutral_response": argument,
            "count": risk_debate_state["count"] + 1,
        }

        result = {"risk_debate_state": new_risk_debate_state}
        note = degraded_note("Neutral Analyst", report)
        if note:
            result["degraded_sources"] = [note]
        return result

    return neutral_node

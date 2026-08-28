from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
)
from tradingagents.agents.utils.debate_utils import (
    clip_report,
    degraded_entry,
    invoke_debate_turn,
)
from tradingagents.dataflows.config import get_config


def create_aggressive_debator(llm):
    def aggressive_node(state) -> dict:
        config = get_config()
        report_clip = config.get("debate_report_clip_chars", 0)
        history_clip = config.get("debate_history_clip_chars", 0)

        risk_debate_state = state["risk_debate_state"]
        history = clip_report(risk_debate_state.get("history", ""), history_clip)
        aggressive_history = risk_debate_state.get("aggressive_history", "")

        current_conservative_response = risk_debate_state.get("current_conservative_response", "")
        current_neutral_response = risk_debate_state.get("current_neutral_response", "")

        market_research_report = clip_report(state["market_report"], report_clip)
        sentiment_report = clip_report(state["sentiment_report"], report_clip)
        news_report = clip_report(state["news_report"], report_clip)
        fundamentals_report = clip_report(state["fundamentals_report"], report_clip)
        instrument_context = get_instrument_context_from_state(state)

        trader_decision = state["trader_investment_plan"]

        prompt = f"""As the Aggressive Risk Analyst, your role is to actively champion high-reward, high-risk opportunities, emphasizing bold strategies and competitive advantages. When evaluating the trader's decision or plan, focus intently on the potential upside, growth potential, and innovative benefits—even when these come with elevated risk. Use the provided market data and sentiment analysis to strengthen your arguments and challenge the opposing views. Specifically, respond directly to each point made by the conservative and neutral analysts, countering with data-driven rebuttals and persuasive reasoning. Highlight where their caution might miss critical opportunities or where their assumptions may be overly conservative. Here is the trader's decision:

{trader_decision}

Your task is to create a compelling case for the trader's decision by questioning and critiquing the conservative and neutral stances to demonstrate why your high-reward perspective offers the best path forward. Incorporate insights from the following sources into your arguments:

{instrument_context}
Market Research Report: {market_research_report}
Social Media Sentiment Report: {sentiment_report}
Latest World Affairs Report: {news_report}
Company Fundamentals Report: {fundamentals_report}
Here is the current conversation history: {history} Here are the last arguments from the conservative analyst: {current_conservative_response} Here are the last arguments from the neutral analyst: {current_neutral_response}. If there are no responses from the other viewpoints yet, present your own argument based on the available data.

Engage actively by addressing any specific concerns raised, refuting the weaknesses in their logic, and asserting the benefits of risk-taking to outpace market norms. Rebut each counterpoint with specific data to underscore why a high-risk approach is optimal. Open directly with your argument or the decisive data — NO greeting, NO preamble, NO addressing the other analysts by title ("Dear colleagues", "my esteemed colleague", "opening the debate"). Write densely so every sentence carries information, without any special formatting.""" + get_language_instruction()

        content, report = invoke_debate_turn(
            llm, prompt, speaker="Aggressive Analyst", config=config
        )

        argument = f"Aggressive Analyst: {content}"

        new_risk_debate_state = {
            "history": risk_debate_state.get("history", "") + "\n" + argument,
            "aggressive_history": aggressive_history + "\n" + argument,
            "conservative_history": risk_debate_state.get("conservative_history", ""),
            "neutral_history": risk_debate_state.get("neutral_history", ""),
            "latest_speaker": "Aggressive",
            "current_aggressive_response": argument,
            "current_conservative_response": risk_debate_state.get("current_conservative_response", ""),
            "current_neutral_response": risk_debate_state.get(
                "current_neutral_response", ""
            ),
            "count": risk_debate_state["count"] + 1,
        }

        result = {"risk_debate_state": new_risk_debate_state}
        entry = degraded_entry("Aggressive Analyst", report, report_key="risk_debate_state")
        if entry:
            result["degraded_sources"] = [entry]
        return result

    return aggressive_node

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


def create_bull_researcher(llm):
    def bull_node(state) -> dict:
        config = get_config()
        report_clip = config.get("debate_report_clip_chars", 0)
        history_clip = config.get("debate_history_clip_chars", 0)

        investment_debate_state = state["investment_debate_state"]
        history = clip_report(investment_debate_state.get("history", ""), history_clip)
        bull_history = investment_debate_state.get("bull_history", "")

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

        prompt = f"""You are a Bull Analyst advocating for investing in the {target_label}. Your task is to build a strong, evidence-based case emphasizing growth potential, competitive advantages, and positive market indicators. Leverage the provided research and data to address concerns and counter bearish arguments effectively.

Key points to focus on:
- Growth Potential: Highlight the company's market opportunities, revenue projections, and scalability.
- Competitive Advantages: Emphasize factors like unique products, strong branding, or dominant market positioning.
- Positive Indicators: Use financial health, industry trends, and recent positive news as evidence.
- Bear Counterpoints: Critically analyze the bear argument with specific data and sound reasoning, addressing concerns thoroughly and showing why the bull perspective holds stronger merit.
- Directness: Open with your strongest argument or the decisive data point. NO greeting, NO addressing the bear analyst by title, NO preamble or theatrical framing ("Dear colleagues", "my esteemed colleague", "opening the debate", "let me show you"). Rebut the bear's points with specific data and reasoning; make every sentence carry information (a number, a mechanism, a counter), not courtesy.

Resources available:
{instrument_context}
Market research report: {market_research_report}
Social media sentiment report: {sentiment_report}
Latest world affairs news: {news_report}
{fundamentals_label}: {fundamentals_report}
Conversation history of the debate: {history}
Last bear argument: {current_response}
Use this information to deliver a compelling bull argument and refute the bear's concerns — straight to the substance, no rhetorical preamble or greeting.
""" + get_language_instruction()

        content, report = invoke_debate_turn(
            llm, prompt, speaker="Bull Researcher", config=config
        )

        argument = f"Analista de Alta (bull): {content}"

        new_investment_debate_state = {
            "history": investment_debate_state.get("history", "") + "\n" + argument,
            "bull_history": bull_history + "\n" + argument,
            "bear_history": investment_debate_state.get("bear_history", ""),
            "current_response": argument,
            "count": investment_debate_state["count"] + 1,
        }

        result = {"investment_debate_state": new_investment_debate_state}
        note = degraded_note("Bull Researcher", report)
        if note:
            result["degraded_sources"] = [note]
        return result

    return bull_node

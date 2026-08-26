"""Research Manager: turns the bull/bear debate into a structured investment plan for the trader."""

from __future__ import annotations

from tradingagents.agents.schemas import ResearchPlan, render_research_plan
from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
)
from tradingagents.agents.utils.structured import (
    NO_EXTERNAL_TOOLS,
    bind_structured,
    invoke_structured_or_freetext,
)


def assert_debate_integrity(investment_debate_state: dict) -> None:
    """Refuse a one-sided debate before the judge rules on it (fork brief 23/08).

    When at least one full back-and-forth was supposed to happen (``count >= 2``),
    BOTH sides must have argued. The pt-BR speaker-label bug once routed the bull
    twice and the bear never, so ``bear_history`` came in empty and the judge
    decided with the confidence of having weighed two sides — a silent, corrupting
    failure. This raises LOUD instead, so the run surfaces as an error rather than
    shipping a fake balanced verdict.

    ``count < 2`` (e.g. ``max_debate_rounds=0``, which still runs the bull once via
    the unconditional entry edge) legitimately has only one speaker and is skipped,
    so this never false-positives on that config.
    """
    count = investment_debate_state.get("count", 0)
    bull = (investment_debate_state.get("bull_history", "") or "").strip()
    bear = (investment_debate_state.get("bear_history", "") or "").strip()
    if count >= 2 and not (bull and bear):
        raise RuntimeError(
            "Debate integrity: reached the Research Manager with count="
            f"{count} but bull_history={'set' if bull else 'EMPTY'} / "
            f"bear_history={'set' if bear else 'EMPTY'} — the bull/bear "
            "alternation broke and the judge would decide one-sided. "
            "Refusing to judge a broken debate (see conditional_logic."
            "should_continue_debate / the pt-BR speaker-label routing)."
        )


def create_research_manager(llm):
    structured_llm = bind_structured(llm, ResearchPlan, "Research Manager")

    def research_manager_node(state) -> dict:
        instrument_context = get_instrument_context_from_state(state)
        history = state["investment_debate_state"].get("history", "")

        investment_debate_state = state["investment_debate_state"]

        # Never let the judge rule on a one-sided debate (fork brief 23/08).
        assert_debate_integrity(investment_debate_state)

        # FRENTE 2 (task 016): validação de consistência ANTES do juiz. Roda os mesmos
        # checks determinísticos do checker pós-publicação sobre os relatórios + debate
        # e injeta um bloco DADOS VERIFICADOS (âncoras canônicas + inconsistências) no
        # contexto do juiz, pra a DECISÃO não pesar um número furado (ex.: FCF 1000×).
        # Import tardio (checker é lógica pura; evita acoplar o engine ao webui no load).
        from tradingagents.webui.contradiction_checker import build_verified_context

        verified_block, pre_judge_findings = build_verified_context({
            "fundamentals_report": state.get("fundamentals_report", "") or "",
            "market_report": state.get("market_report", "") or "",
            "bull": investment_debate_state.get("bull_history", "") or "",
            "bear": investment_debate_state.get("bear_history", "") or "",
        })

        prompt = f"""As the Research Manager and debate facilitator, your role is to critically evaluate this round of debate and deliver a clear, actionable investment plan for the trader.

{instrument_context}

{verified_block}

---

**Rating Scale** (use exactly one):
- **Buy**: Strong conviction in the bull thesis; recommend taking or growing the position
- **Overweight**: Constructive view; recommend gradually increasing exposure
- **Hold**: Balanced view; recommend maintaining the current position
- **Underweight**: Cautious view; recommend trimming exposure
- **Sell**: Strong conviction in the bear thesis; recommend exiting or avoiding the position

Commit to a clear stance whenever the debate's strongest arguments warrant one; reserve Hold for situations where the evidence on both sides is genuinely balanced.

---

**Debate History:**
{history}

{NO_EXTERNAL_TOOLS}""" + get_language_instruction()

        investment_plan = invoke_structured_or_freetext(
            structured_llm,
            llm,
            prompt,
            render_research_plan,
            "Research Manager",
        )

        new_investment_debate_state = {
            "judge_decision": investment_plan,
            "history": investment_debate_state.get("history", ""),
            "bear_history": investment_debate_state.get("bear_history", ""),
            "bull_history": investment_debate_state.get("bull_history", ""),
            "current_response": investment_plan,
            "count": investment_debate_state["count"],
        }

        return {
            "investment_debate_state": new_investment_debate_state,
            "investment_plan": investment_plan,
            # inconsistências detectadas nos insumos ANTES da decisão — o runner
            # carimba o veredito com elas (task 016).
            "pre_judge_findings": pre_judge_findings,
        }

    return research_manager_node

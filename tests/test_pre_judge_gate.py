"""Validação de consistência ANTES do juiz (FRENTE 2 / task 016).

O checker de contradições rodava só DEPOIS do juiz — o veredito já era decidido com o
dado furado. Aqui a MESMA lógica roda upstream: monta um bloco DADOS VERIFICADOS
(âncoras canônicas + inconsistências) e injeta no contexto do juiz; se sobra
inconsistência, o veredito sai carimbado. Reusa check_contradictions (não duplica).
"""

from types import SimpleNamespace

import pytest

from tradingagents.webui.contradiction_checker import (
    build_verified_context,
    format_verdict_caveat,
)

pytestmark = pytest.mark.unit

# Âncora determinística com o FCF CERTO (bilhões); um agente cita ERRADO (trilhões).
_FUNDAMENTALS = (
    "Fundamentos do ativo...\n\n"
    "## Âncoras determinísticas (preço de referência + agregados TTM)\n\n"
    "- **Preço de referência (as_of 2026-08-26)**: 313.44\n"
    "- **FCF TTM** (soma de Q1, Q2, Q3, Q4): US$ 136.68 bilhões\n\n"
    "## Outra seção qualquer\nresto do relatório")
_BULL_WRONG = "O FCF de US$ 136.68 trilhões TTM sustenta a alta."   # 1000× errado


def test_build_verified_context_extracts_anchor_and_flags_divergence():
    block, findings = build_verified_context({
        "fundamentals_report": _FUNDAMENTALS,
        "bull": _BULL_WRONG, "bear": "Bear: riscos macro."})
    # o bloco carrega o número CANÔNICO (bilhões) e a instrução de usar só ele
    assert "DADOS VERIFICADOS" in block
    assert "US$ 136.68 bilhões" in block
    assert "Outra seção" not in block            # recorta só a seção de âncoras
    assert "decida SÓ por estes" in block or "valor válido é ESTE" in block
    # e detecta a divergência de 1000× (reusa o mesmo checker)
    assert [f["code"] for f in findings] == ["fcf_ttm_divergente"]
    assert "inconsistência" in block.lower()


def test_build_verified_context_clean_when_consistent():
    block, findings = build_verified_context({
        "fundamentals_report": _FUNDAMENTALS,
        "bull": "O FCF de US$ 136.68 bilhões TTM sustenta a alta."})   # certo
    assert findings == []
    assert "US$ 136.68 bilhões" in block         # âncoras seguem no bloco
    assert "inconsistência" not in block.lower()  # sem alerta


def test_verified_context_failopen_on_missing_reports():
    block, findings = build_verified_context({})
    assert findings == []
    assert "DADOS VERIFICADOS" in block          # nunca levanta; bloco mínimo


def test_format_verdict_caveat():
    assert format_verdict_caveat([]) == ""
    assert format_verdict_caveat(None) == ""
    c = format_verdict_caveat([{"code": "fcf_ttm_divergente", "message": "x"}])
    assert "1 inconsistência" in c and "cautela" in c


class _CapturingLLM:
    def __init__(self):
        self.prompt = None

    def bind_tools(self, *a, **k):
        return self

    def with_structured_output(self, *a, **k):
        return self

    def invoke(self, prompt, *a, **k):
        if self.prompt is None:
            self.prompt = prompt if isinstance(prompt, str) else str(prompt)
        return SimpleNamespace(content="Plano: MANTER, com base nos dados verificados.")


def _judge_state():
    return {
        "investment_debate_state": {
            "history": "debate...", "bull_history": _BULL_WRONG,
            "bear_history": "Bear: riscos macro relevantes.", "count": 2,
            "current_response": _BULL_WRONG},
        "fundamentals_report": _FUNDAMENTALS, "market_report": "mercado...",
        "instrument_context": "AAPL — Apple Inc.",
    }


def test_research_manager_injects_verified_block_and_returns_findings():
    from tradingagents.agents.managers.research_manager import create_research_manager
    llm = _CapturingLLM()
    out = create_research_manager(llm)(_judge_state())
    p = llm.prompt
    assert "DADOS VERIFICADOS" in p                 # juiz recebe o bloco verificado
    assert "US$ 136.68 bilhões" in p                # com o valor CERTO (não trilhões)
    assert "fcf_ttm_divergente" in [f["code"] for f in out["pre_judge_findings"]]


def test_portfolio_manager_injects_verified_block():
    from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
    llm = _CapturingLLM()
    state = {
        "risk_debate_state": {
            "history": "risco...", "aggressive_history": "", "conservative_history": "",
            "neutral_history": "", "current_aggressive_response": "",
            "current_conservative_response": "", "current_neutral_response": "", "count": 2},
        "investment_plan": "Plano do juiz: manter.",
        "trader_investment_plan": "Trader: comprar no recuo.",
        "fundamentals_report": _FUNDAMENTALS, "market_report": "mercado...",
        "instrument_context": "AAPL — Apple Inc.", "past_context": "",
    }
    create_portfolio_manager(llm)(state)
    assert "DADOS VERIFICADOS" in llm.prompt
    assert "US$ 136.68 bilhões" in llm.prompt       # decisão FINAL também na verdade


def test_extract_result_carries_pre_judge_findings_and_caveat():
    from tradingagents.webui.runner import extract_result
    findings = [{"code": "fcf_ttm_divergente", "severity": "alta", "message": "FCF 1000×"}]
    r = extract_result({"pre_judge_findings": findings}, "Hold")
    assert r["pre_judge_findings"] == findings
    # o caveat é preenchido na finalização; extract_result inicia vazio
    assert r["verdict_caveat"] == ""
    assert format_verdict_caveat(r["pre_judge_findings"]).startswith("Decidido com")

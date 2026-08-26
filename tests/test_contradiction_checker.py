"""Pre-publication contradiction checker (spec item 7 — the keystone).

On the AAOI 25/08 shape BEFORE the 1-6 fixes the checker must LIST the real
inconsistencies (double decision, incoherent 91,50 1-2-3, market-cap price drift);
on the fixed shape the list is clean.
"""
import pytest

from tradingagents.webui.contradiction_checker import (
    check_contradictions,
    render_contradictions_section,
)


def _dirty() -> dict:
    """AAOI-shaped pre-fix result: competing final proposals, chart 1-2-3 trigger
    (91,50) disagreeing with the report text (160,87) and incoherently 'acionado' at
    price 113,15, and a market cap that implies ~119 vs the 113,15 reference."""
    return {
        "final_decision": "REDUZIR",
        "market_report": (
            "### Padrão 1-2-3 de venda\n"
            "- **Ponto 2** (repique / mínima): 2026-06-09 — 160.87\n"
            "- **Gatilho**: perda de 160.87 — **acionado** (perdeu a mínima do ponto 2)."
        ),
        "erick_report": "Leitura do método.\n\nPROPOSTA FINAL DE TRANSAÇÃO: MANTER (caixa / aguardar gatilho)",
        "trader_plan": "**Ação**: VENDER — Sell\n\nPROPOSTA FINAL DE TRANSAÇÃO: **VENDER**",
        "fundamentals_report": (
            "- **Capitalização de mercado:** US$ 9,56 bilhões\n"
            "- Preço implícito ~US$ 119 (com base em ~80,2 milhões de ações)"
        ),
        "actionable": {"price": 113.15},
        "price_chart": {
            "candles": [{"c": 113.15}],
            "markers": {"pattern_123": {"trigger": 91.5, "state": "acionado", "direction": "venda"}},
        },
    }


def _clean() -> dict:
    """The fixed shape: one canonical decision, chart 1-2-3 == report (160,87) and
    coherently 'acionado' (113,15 < 160,87), a single as_of price, and a cited FCF TTM
    that matches the deterministic anchor."""
    return {
        "final_decision": "REDUZIR",
        "market_report": (
            "### Padrão 1-2-3 de venda\n"
            "- **Gatilho**: perda de 160.87 — **acionado** (perdeu a mínima do ponto 2)."
        ),
        "erick_report": "Leitura do método: caixa, aguardar gatilho. (sem proposta final)",
        "trader_plan": "**Ação**: VENDER — Sell\n\nLEITURA DO TRADER (insumo, não é o veredito): **VENDER**",
        "fundamentals_report": (
            "## Âncoras determinísticas\n"
            "- **FCF TTM** (soma de 2026-06-30, 2026-03-31, 2025-12-31, 2025-09-30): -601,306,000\n"
            "O FCF TTM é -601M, refletindo queima de caixa."
        ),
        "as_of_price": 113.15,
        "actionable": {"price": 113.15},
        "price_chart": {
            "candles": [{"c": 113.15}],
            "markers": {"pattern_123": {"trigger": 160.87, "state": "acionado", "direction": "venda"}},
        },
    }


@pytest.mark.unit
def test_dirty_report_lists_real_inconsistencies():
    codes = {f["code"] for f in check_contradictions(_dirty())}
    assert "decisao_dupla" in codes            # erick=MANTER vs trader=VENDER
    assert "gatilho_123_divergente" in codes   # chart 91,50 vs texto 160,87
    assert "acionado_incoerente" in codes      # venda 'acionado' com 113,15 ≥ 91,50
    assert "preco_market_cap_divergente" in codes  # 9,56bi/80,2mi ≈ 119 vs 113,15


@pytest.mark.unit
def test_clean_report_has_no_findings():
    assert check_contradictions(_clean()) == []


@pytest.mark.unit
def test_pattern_trigger_match_is_not_flagged():
    r = _clean()
    findings = {f["code"] for f in check_contradictions(r)}
    assert "gatilho_123_divergente" not in findings
    assert "acionado_incoerente" not in findings


@pytest.mark.unit
def test_fcf_cited_diverges_from_anchor_is_flagged():
    """The AAOI FCF bug: a report whose anchor sums to -601M but whose prose still
    cites -887M TTM must be flagged."""
    r = _clean()
    r["bear"] = "O FCF é profundamente negativo: -US$ 887 milhões TTM."
    codes = {f["code"] for f in check_contradictions(r)}
    assert "fcf_ttm_divergente" in codes


@pytest.mark.unit
def test_price_drift_between_structured_fields_is_flagged():
    r = _clean()
    r["as_of_price"] = 113.15
    r["price_chart"]["candles"] = [{"c": 119.0}]  # chart close far from as_of
    codes = {f["code"] for f in check_contradictions(r)}
    assert "preco_divergente" in codes


@pytest.mark.unit
def test_render_section_clean_and_dirty():
    assert "Nenhuma inconsistência" in render_contradictions_section([])
    md = render_contradictions_section(check_contradictions(_dirty()))
    assert "Checagem de consistência" in md
    assert "decisao_dupla" in md


@pytest.mark.unit
def test_fail_open_on_bad_input():
    assert check_contradictions(None) == []
    assert check_contradictions({}) == []

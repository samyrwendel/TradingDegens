"""Prediction markets are non-optional in the news report (fork brief #4).

The analyst LLM tends to skip get_prediction_markets, so the news node appends a
deterministic section when the report doesn't already cover it: real data when a
market exists, or an explicit "no market" line otherwise. Never silence.
"""
import inspect

import pytest

import tradingagents.agents.analysts.news_analyst as na
from tradingagents.agents.utils import prediction_market_coverage as pmc


@pytest.mark.unit
def test_detects_existing_coverage():
    assert pmc.report_mentions_prediction_markets("... Polymarket says 40% ...")
    assert pmc.report_mentions_prediction_markets("see the prediction market odds")
    assert not pmc.report_mentions_prediction_markets("just some news, no odds")
    assert not pmc.report_mentions_prediction_markets("")


@pytest.mark.unit
def test_already_covered_report_is_untouched(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(pmc, "route_to_vendor", lambda *a, **k: calls.update(n=calls["n"] + 1) or "x")
    report = "News. ## Polymarket prediction markets: Fed ... Yes 40%"
    assert pmc.ensure_prediction_market_coverage(report, "BE") == report
    assert calls["n"] == 0  # no fetch when already covered


@pytest.mark.unit
def test_section_appended_with_market_data(monkeypatch):
    def fake_route(method, topic, limit, display=None):
        assert method == "get_prediction_markets"
        # The standing macro topics search in English but must DISPLAY a pt-BR
        # label — the report carries no stray English topic string.
        shown = display or topic
        return f'## Mercados de previsão Polymarket: "{shown}"\n- **Event?** — 62% ($1M volume)'

    monkeypatch.setattr(pmc, "route_to_vendor", fake_route)
    out = pmc.ensure_prediction_market_coverage("Body.", "BE")
    assert "## Mercados de Previsão" in out
    assert "62%" in out
    assert out.startswith("Body.")
    # The English search queries never surface; their pt-BR labels do.
    assert "Decisão de juros do Fed" in out
    assert "Recessão nos EUA" in out
    assert "Fed interest rate decision" not in out
    assert "US recession" not in out


@pytest.mark.unit
def test_no_market_yields_explicit_justification(monkeypatch):
    monkeypatch.setattr(
        pmc, "route_to_vendor",
        lambda method, topic, limit, display=None: (
            f'## Mercados de previsão Polymarket: "{display or topic}"\n'
            f"Nenhum mercado de previsão aberto casou com '{display or topic}'."
        ),
    )
    out = pmc.ensure_prediction_market_coverage("Body.", "BE")
    assert "## Mercados de Previsão" in out
    assert "nenhum mercado de previsão aberto para este tópico" in out.lower()


@pytest.mark.unit
def test_vendor_error_does_not_crash(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(pmc, "route_to_vendor", boom)
    out = pmc.ensure_prediction_market_coverage("Body.", "BE")
    assert "## Mercados de Previsão" in out
    assert "a consulta de mercado de previsão falhou" in out.lower()


@pytest.mark.unit
def test_news_node_wires_coverage_in():
    # Guard against the wiring being removed: the node must call the helper.
    src = inspect.getsource(na)
    assert "ensure_prediction_market_coverage" in src

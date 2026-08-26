"""Report sanitizer — no internal error/component string reaches the PDF (item 6d)."""
import pytest

from tradingagents.webui.report_sanitizer import (
    has_internal_leak,
    sanitize_report_text,
    sanitize_result,
)

# The exact ZEC leak from the spec.
_ZEC_LEAK = (
    "Análise on-chain: DATA_UNAVAILABLE: optional onchain_data could not be retrieved "
    "([ta_datacache cached failure: NoMarketDataError] No market data for 'ZEC-USD'). "
    "Proceed without it; do not fabricate values."
)


@pytest.mark.unit
def test_zec_leak_is_scrubbed_to_friendly_line():
    assert has_internal_leak(_ZEC_LEAK)
    out = sanitize_report_text(_ZEC_LEAK)
    assert not has_internal_leak(out)
    assert "on-chain" in out
    assert "indisponíveis" in out
    for internal in ("DATA_UNAVAILABLE", "ta_datacache", "NoMarketDataError"):
        assert internal not in out


@pytest.mark.unit
def test_no_data_available_and_bare_exception():
    t = "NO_DATA_AVAILABLE: No usable market data for 'FOO' from any vendor."
    assert not has_internal_leak(sanitize_report_text(t))
    oi = "- **Contratos em aberto** (open interest): indisponível (NoMarketDataError); sem valor."
    out = sanitize_report_text(oi)
    assert "NoMarketDataError" not in out
    assert "indisponível" in out


@pytest.mark.unit
def test_clean_text_is_unchanged_and_idempotent():
    clean = "O preço recuou até a MMS200 e reagiu. Sem novidade."
    assert sanitize_report_text(clean) == clean
    once = sanitize_report_text(_ZEC_LEAK)
    assert sanitize_report_text(once) == once  # idempotent


@pytest.mark.unit
def test_sanitize_result_scrubs_every_module_text():
    result = {
        "news_report": _ZEC_LEAK,
        "derivatives_report": "OI indisponível (ConnectionError); sem valor.",
        "market_report": "Leitura limpa.",
        "verdict": "Underweight",  # non-text field untouched
    }
    sanitize_result(result)
    assert not has_internal_leak(result["news_report"])
    assert "ConnectionError" not in result["derivatives_report"]
    assert result["market_report"] == "Leitura limpa."
    assert result["verdict"] == "Underweight"


@pytest.mark.unit
def test_fail_open_on_non_string():
    assert sanitize_report_text(None) == ""
    assert sanitize_result(None) is None
    assert sanitize_result({"market_report": 123}) == {"market_report": 123}

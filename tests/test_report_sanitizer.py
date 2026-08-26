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


@pytest.mark.unit
def test_long_whitespace_run_is_linear_not_redos():
    """Regressão (task 025): o passo 4 usava ``\\s*\\(`` — com o literal ``(`` falhando
    ao longo de uma corrida de espaços, o motor re-escaneava tudo a cada posição = O(n²).
    Um relatório degradado com ~100k espaços travava o ``re.sub`` por MINUTOS segurando o
    GIL → congelava o servidor HTTP. Agora é ``\\s?`` (linear). Prova: 200k espaços
    sanitizam numa fração de segundo (era >minutos)."""
    import time
    text = " " * 100_000 + "algo (KeyError) mais" + " " * 100_000
    start = time.monotonic()
    out = sanitize_report_text(text)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"sanitize levou {elapsed:.1f}s — ReDoS voltou?"
    assert "(KeyError)" not in out            # ainda remove o parentético interno
    assert "algo mais" in out                 # e o espaço extra é limpo (passo 5)

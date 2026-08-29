"""Deterministic fundamentals anchors — frozen reference price + TTM aggregates.

Pins the two anti-drift/anti-hallucination guarantees offline on synthetic data:
* TTM is exactly the four most-recent quarters summed from the table (the AAOI bug:
  "-887M TTM" when the four quarters add to ~-601M, and "-692M em 4 tri" that summed
  five);
* the reference price / market cap / 52-week low come off the date-guarded daily
  series, not yfinance's live fields, so every module can share ONE price.
"""
import pandas as pd
import pytest

from tradingagents.agents.utils.agent_utils import build_instrument_context
from tradingagents.dataflows import fundamentals_anchors as fa


def _quarterly_with_fifth_older() -> pd.DataFrame:
    """AAOI-shaped quarterly cash flow: the four most-recent FCF quarters sum to
    -601.3; a fifth, OLDER quarter (-999) must be excluded from the TTM."""
    cols = {
        pd.Timestamp("2026-06-30"): {"Free Cash Flow": -274.1, "Operating Cash Flow": -180.0, "Capital Expenditure": -94.1},
        pd.Timestamp("2026-03-31"): {"Free Cash Flow": -143.7, "Operating Cash Flow": -90.0, "Capital Expenditure": -53.7},
        pd.Timestamp("2025-12-31"): {"Free Cash Flow": -104.7, "Operating Cash Flow": -60.0, "Capital Expenditure": -44.7},
        pd.Timestamp("2025-09-30"): {"Free Cash Flow": -78.8, "Operating Cash Flow": -40.0, "Capital Expenditure": -38.8},
        pd.Timestamp("2025-06-30"): {"Free Cash Flow": -999.0, "Operating Cash Flow": -500.0, "Capital Expenditure": -499.0},
    }
    return pd.DataFrame(cols)


@pytest.mark.unit
def test_ttm_is_exactly_four_most_recent_quarters():
    q = _quarterly_with_fifth_older()
    fcf, quarters = fa.ttm_sum(q, fa._FCF_ALIASES)
    assert round(fcf, 1) == -601.3            # -274.1 -143.7 -104.7 -78.8
    assert len(quarters) == 4                 # never five
    assert "2025-06-30" not in quarters       # the older quarter is excluded
    assert "2026-06-30" in quarters and "2025-09-30" in quarters


@pytest.mark.unit
def test_ttm_refuses_partial_year():
    """Fewer than four quarters is NOT a TTM — return None rather than a partial sum
    that reads as a full trailing year."""
    q = _quarterly_with_fifth_older().iloc[:, :3]  # only three quarters
    fcf, quarters = fa.ttm_sum(q, fa._FCF_ALIASES)
    assert fcf is None
    assert len(quarters) == 3


@pytest.mark.unit
def test_compute_ttm_cashflow_all_rows():
    agg = fa.compute_ttm_cashflow(_quarterly_with_fifth_older())
    assert round(agg["fcf_ttm"], 1) == -601.3
    assert round(agg["ocf_ttm"], 1) == -370.0     # -180 -90 -60 -40
    assert round(agg["capex_ttm"], 1) == -231.3   # -94.1 -53.7 -44.7 -38.8
    assert len(agg["quarters"]) == 4


@pytest.mark.unit
def test_compute_ttm_missing_row_degrades_to_none():
    q = pd.DataFrame({pd.Timestamp("2026-06-30"): {"Revenue": 10.0}})
    agg = fa.compute_ttm_cashflow(q)
    assert agg["fcf_ttm"] is None and agg["ocf_ttm"] is None and agg["capex_ttm"] is None


def _daily(n: int = 300, last_close: float = 113.15) -> pd.DataFrame:
    dates = pd.bdate_range("2025-01-01", periods=n)
    close = [100.0 + (i % 20) for i in range(n)]
    close[-1] = last_close
    high = [c + 1 for c in close]
    low = [c - 1 for c in close]
    low[0] = 5.0        # OUTSIDE the 252-day window -> must NOT be the 52w low
    high[0] = 999.0     # OUTSIDE the window -> must NOT be the 52w high
    low[-1] = 90.0      # inside the window
    high[-2] = 210.0    # inside the window
    return pd.DataFrame({"Date": dates.strftime("%Y-%m-%d"), "Open": close,
                         "High": high, "Low": low, "Close": close, "Volume": [1] * n})


@pytest.mark.unit
def test_price_snapshot_uses_date_guarded_series():
    snap = fa.price_snapshot(_daily(), shares=80_200_000)
    assert snap["price"] == 113.15
    assert snap["as_of"] == "2026-02-24"                 # last bdate of 300 from 2025-01-01
    assert snap["market_cap"] == round(113.15 * 80_200_000)
    # 52w low/high come from the trailing 252 rows, NOT the far-past outliers
    assert snap["low_52w"] == 90.0
    assert snap["high_52w"] == 210.0
    assert snap["low_52w"] != 5.0 and snap["high_52w"] != 999.0


@pytest.mark.unit
def test_price_snapshot_no_shares_no_market_cap():
    snap = fa.price_snapshot(_daily(), shares=None)
    assert snap["price"] == 113.15
    assert snap["market_cap"] is None


@pytest.mark.unit
def test_price_snapshot_carries_canonical_50_200_averages():
    """Item 6: the 50d/200d averages come off the SAME date-guarded series the chart
    draws (one canonical value), not yfinance's live figures."""

    d = _daily(300)
    snap = fa.price_snapshot(d, shares=None)
    c = d["Close"].astype(float)
    assert snap["ma_50"] == round(float(c.rolling(50).mean().iloc[-1]), 2)
    assert snap["ma_200"] == round(float(c.rolling(200).mean().iloc[-1]), 2)


@pytest.mark.unit
def test_price_snapshot_averages_none_when_window_too_short():
    snap = fa.price_snapshot(_daily(40), shares=None)   # < 50 bars
    assert snap["ma_50"] is None and snap["ma_200"] is None


@pytest.mark.unit
def test_render_anchors_section_includes_moving_averages():
    snap = fa.price_snapshot(_daily(), shares=80_200_000)
    md = fa.render_anchors_section(snap, {})
    assert "MMS50" in md and "MMS200" in md


@pytest.mark.unit
def test_render_anchors_section_carries_numbers_and_rule():
    snap = fa.price_snapshot(_daily(), shares=80_200_000)
    agg = fa.compute_ttm_cashflow(_quarterly_with_fifth_older())
    md = fa.render_anchors_section(snap, agg)
    assert "Âncoras determinísticas" in md
    assert "TTM = soma dos 4 trimestres mais recentes" in md
    assert "113,15".replace(",", ".") in md or "113.15" in md
    # FCF TTM computado ≈ -601 (dados de teste pequenos), agora com prefixo US$
    # (bug 014); é o valor computado (-601), não -887.
    assert "-US$ 601" in md
    assert "Cite APENAS estes números" in md
    assert "PALAVRA DE MAGNITUDE" in md   # instrui o agente a citar a magnitude


@pytest.mark.unit
def test_aggregates_render_with_explicit_magnitude_word():
    """Bug 014: agregados grandes saem com a PALAVRA de magnitude (bilhões/milhões),
    não o número cru que fazia o agente escalar 1000× (136 bi → 136 tri)."""
    md = fa.render_anchors_section(
        {"price": 313.44, "market_cap": 3_500_000_000_000, "shares": 15_000_000_000},
        {"fcf_ttm": 136_683_000_000, "ocf_ttm": 150_000_000_000,
         "capex_ttm": -13_200_000_000, "quarters": ["Q1", "Q2", "Q3", "Q4"]})
    assert "US$ 136.68 bilhões" in md       # não "136,683,000,000" cru
    assert "-US$ 13.20 bilhões" in md       # capex negativo com magnitude
    assert "US$ 3.50 trilhões" in md        # market cap em trilhões
    # e o checker parseia o agregado da âncora de volta na mesma escala
    from tradingagents.webui.contradiction_checker import _anchor_fcf_ttm
    parsed = _anchor_fcf_ttm({"fundamentals_report": md})
    assert parsed is not None and abs(parsed - 136_683_000_000) / 136_683_000_000 < 0.01


@pytest.mark.unit
def test_render_anchors_section_none_when_empty():
    assert fa.render_anchors_section(None, {"fcf_ttm": None, "ocf_ttm": None, "capex_ttm": None}) is None


# --- E: one frozen reference price injected into the shared instrument context -----
@pytest.mark.unit
def test_instrument_context_injects_reference_price():
    ctx = build_instrument_context("AAOI", "stock", None, reference_price=113.15, as_of="2026-08-25")
    assert "Reference price for this run (as_of 2026-08-25): 113.15" in ctx
    assert "any current-price or market-cap statement" in ctx


@pytest.mark.unit
def test_instrument_context_backward_compatible_without_price():
    ctx = build_instrument_context("AAOI", "stock")
    assert "Reference price for this run" not in ctx
    assert "`AAOI`" in ctx


# ------------------------------------------ multi-fonte (2+ fontes por insumo) ----
def _av_payload(quarters: list[dict]) -> str:
    import json

    return json.dumps({"quarterlyReports": quarters})


def _av_quarter(end: str, ocf: float, capex: float) -> dict:
    return {"fiscalDateEnding": end, "operatingCashflow": str(ocf),
            "capitalExpenditures": str(capex)}


@pytest.mark.unit
def test_av_quarterly_to_frame_shapes_yfinance_equivalent():
    """O reshape do AV devolve o DataFrame que ttm_sum espera: row 'Free Cash
    Flow' = OCF − capex por trimestre, colunas = datas."""
    payload = _av_payload([
        _av_quarter("2026-06-30", 100.0, -30.0),
        _av_quarter("2026-03-31", 90.0, -20.0),
        _av_quarter("2025-12-31", 80.0, -10.0),
        _av_quarter("2025-09-30", 70.0, -40.0),
    ])
    frame = fa._av_quarterly_to_frame(payload, "2026-08-27")
    assert frame is not None
    fcf, quarters = fa.ttm_sum(frame, fa._FCF_ALIASES)
    # 100-30=70 · 90-20=70 · 80-10=70 · 70-40=30 → 240
    assert round(fcf, 1) == 240.0
    assert quarters == ["2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30"]


@pytest.mark.unit
def test_quarterly_cashflow_falls_back_to_alpha_vantage(monkeypatch):
    """yfinance morto + chave AV presente → tabela vem do AV com fonte nomeada."""
    import tradingagents.dataflows.alpha_vantage_common as avc
    import tradingagents.dataflows.alpha_vantage_fundamentals as avf

    monkeypatch.setattr(avc, "get_api_key", lambda: "k-av")

    def boom():
        raise RuntimeError("yfinance down")

    import yfinance as yf
    monkeypatch.setattr(yf, "Ticker", lambda s: (_ for _ in ()).throw(RuntimeError("yf down")))

    payload = _av_payload([
        _av_quarter("2026-06-30", 100.0, -30.0),
        _av_quarter("2026-03-31", 90.0, -20.0),
        _av_quarter("2025-12-31", 80.0, -10.0),
        _av_quarter("2025-09-30", 70.0, -40.0),
    ])
    monkeypatch.setattr(avf, "get_cashflow", lambda t, curr_date=None: payload)
    frame, fonte = fa._fetch_quarterly_cashflow("INTC", "2026-08-27")
    assert fonte == "alpha_vantage"
    fcf, _ = fa.ttm_sum(frame, fa._FCF_ALIASES)
    assert round(fcf, 1) == 240.0


@pytest.mark.unit
def test_quarterly_cashflow_without_av_key_stays_none(monkeypatch):
    """Sem chave AV e yfinance morto → (None, None): ausência declarada, nunca
    fonte inventada."""
    import yfinance as yf

    import tradingagents.dataflows.alpha_vantage_common as avc

    monkeypatch.setattr(avc, "get_api_key", lambda: None)
    monkeypatch.setattr(yf, "Ticker", lambda s: (_ for _ in ()).throw(RuntimeError("yf down")))
    assert fa._fetch_quarterly_cashflow("INTC", "2026-08-27") == (None, None)


@pytest.mark.unit
def test_shares_falls_back_to_finnhub(monkeypatch):
    import yfinance as yf

    import tradingagents.dataflows.finnhub_fundamentals as fh

    monkeypatch.setattr(yf, "Ticker", lambda s: (_ for _ in ()).throw(RuntimeError("yf down")))
    monkeypatch.setattr(fh, "get_shares", lambda s: 5_044_000_000.0)
    sh, fonte = fa._fetch_shares("INTC", "2026-08-27")
    assert fonte == "finnhub" and sh == pytest.approx(5_044_000_000.0)


@pytest.mark.unit
def test_fcf_crosscheck_renders_when_sources_diverge():
    """Divergência >15% → linha com os DOIS números e o limiar declarado."""
    line = fa._fcf_crosscheck_from(2.83e9, 4.54e9)
    assert line is not None
    assert "4.54 bilhões" in line and "2.83 bilhões" in line
    assert "limiar provisório" in line


@pytest.mark.unit
def test_fcf_crosscheck_silent_when_agreeing():
    """Divergência dentro do limiar → None (sem ruído em toda run)."""
    assert fa._fcf_crosscheck_from(2.83e9, 3.0e9) is None
    assert fa._fcf_crosscheck_from(2.83e9, None) is None


# ------------------- a conferência cruzada não roda em run histórica (A3) -------
@pytest.mark.unit
def test_run_historica_nao_chama_a_fonte_LIVE(monkeypatch):
    """``get_fcf_ttm`` é CORRENTE por definição. Numa run de data passada ele traria
    o número de HOJE pra dentro da seção que existe pra impedir look-ahead —
    estourando o limiar quase sempre e virando falso positivo que a LLM lê.

    A fonte live nem chega a ser consultada: se for, este teste explode.
    """
    import tradingagents.dataflows.finnhub_fundamentals as ff

    def nunca(symbol):
        raise AssertionError("a fonte LIVE foi consultada numa run histórica")

    monkeypatch.setattr(ff, "get_fcf_ttm", nunca)
    linha = fa._fcf_crosscheck("NVDA", 2.83e9, "2020-01-02")
    assert linha is not None                       # ausência DECLARADA, não silêncio
    assert "sem conferência cruzada" in linha
    assert "look-ahead" in linha


@pytest.mark.unit
def test_run_AO_VIVO_continua_conferindo(monkeypatch):
    """Contra-prova: fechar o look-ahead não pode matar a conferência de hoje."""
    from datetime import date

    import tradingagents.dataflows.finnhub_fundamentals as ff

    monkeypatch.setattr(ff, "get_fcf_ttm", lambda symbol: 4.54e9)
    linha = fa._fcf_crosscheck("NVDA", 2.83e9, date.today().isoformat())
    assert linha is not None and "4.54 bilhões" in linha


@pytest.mark.unit
def test_sem_data_o_comportamento_de_antes_e_preservado(monkeypatch):
    """``curr_date=None`` (chamada solta) mantém a semântica antiga — byte a byte."""
    import tradingagents.dataflows.finnhub_fundamentals as ff

    monkeypatch.setattr(ff, "get_fcf_ttm", lambda symbol: 4.54e9)
    assert fa._fcf_crosscheck("NVDA", 2.83e9) == fa._fcf_crosscheck_from(2.83e9, 4.54e9)


@pytest.mark.unit
def test_render_names_the_shares_source():
    """``shares_fonte`` era gravado no snapshot e nunca renderizado — o market cap
    saía com a contagem de ações e sem dizer de onde ela veio, justo o padrão que
    o FCF já corrigiu."""
    sec = fa.render_anchors_section(
        {"price": 100.0, "as_of": "2026-08-27", "shares": 5_044_000_000.0,
         "market_cap": 504_400_000_000.0, "shares_fonte": "finnhub"},
        {"fcf_ttm": 2.83e9, "quarters": ["2026-06-30"], "fonte": "yfinance"},
    )
    assert "fonte finnhub" in sec


@pytest.mark.unit
def test_render_sem_fonte_de_shares_nao_inventa():
    sec = fa.render_anchors_section(
        {"price": 100.0, "as_of": "2026-08-27", "shares": 5_044_000_000.0,
         "market_cap": 504_400_000_000.0},
        {"fcf_ttm": 2.83e9, "quarters": ["2026-06-30"], "fonte": "yfinance"},
    )
    assert "ações)" in sec and "fonte " not in sec.split("Market cap")[1].split("\n")[0]


@pytest.mark.unit
def test_render_names_the_fcf_source():
    """A seção cita de ONDE veio o FCF TTM — número sem fonte é o que escondeu
    o drift do info.freeCashflow até hoje."""
    sec = fa.render_anchors_section(
        {"price": 92.09, "as_of": "2026-08-27"},
        {"fcf_ttm": 2.83e9, "quarters": ["2026-06-30"], "fonte": "yfinance"},
    )
    assert "[fonte: yfinance]" in sec


@pytest.mark.unit
def test_render_without_source_stays_clean():
    sec = fa.render_anchors_section({"price": 92.09}, {"fcf_ttm": 2.83e9})
    assert "[fonte:" not in sec

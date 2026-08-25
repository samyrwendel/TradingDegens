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
def test_render_anchors_section_carries_numbers_and_rule():
    snap = fa.price_snapshot(_daily(), shares=80_200_000)
    agg = fa.compute_ttm_cashflow(_quarterly_with_fifth_older())
    md = fa.render_anchors_section(snap, agg)
    assert "Âncoras determinísticas" in md
    assert "TTM = soma dos 4 trimestres mais recentes" in md
    assert "113,15".replace(",", ".") in md or "113.15" in md
    assert "-601" in md              # the computed FCF TTM, not -887
    assert "Cite APENAS estes números" in md


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

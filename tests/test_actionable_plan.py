"""The verdict must be operable: price @ analysis, horizon, timeframe, and price
zones — or an explicit "sem nível definido" (fork brief 23/08).

The product owner's complaint: the verdict showed no asset price at analysis time,
no time horizon, no timeframe, and no buy/realize/pullback regions. These tests pin
the deterministic ``build_actionable_plan`` on a synthetic series with a KNOWN
structure, offline, and enforce the two hard rules: every level is a real number
from the series (a close, a moving average, or a prior swing high), and when there
is no basis the level is ``None`` — never a fabricated or vague value.
"""
import pandas as pd
import pytest

from tradingagents.dataflows import price_structure as ps


def _frame() -> pd.DataFrame:
    """Same known series as test_price_structure: uptrend, a pullback to MA20, a
    1-2-3, then a breakout that leaves price in new-high air at the end."""
    closes: list[float] = []
    closes += [100 + i for i in range(60)]
    closes += [156, 150, 146, 150, 156]
    closes += [162 + i for i in range(20)]
    closes += [175, 168, 160, 150, 145]
    closes += [150, 160, 172, 185, 190]
    closes += [182, 175, 168, 162, 166]
    closes += [192 + i for i in range(15)]
    dates = pd.bdate_range("2025-01-01", periods=len(closes))
    c = pd.Series(closes, dtype=float)
    return pd.DataFrame({
        "Date": dates.strftime("%Y-%m-%d"),
        "Open": c.shift(1).fillna(c).values,
        "High": (c * 1.01).values,
        "Low": (c * 0.99).values,
        "Close": c.values,
        "Volume": [1000] * len(c),
    })


CURR = "2025-12-31"


@pytest.fixture
def synth(monkeypatch):
    df = _frame()
    monkeypatch.setattr(ps, "load_ohlcv", lambda symbol, curr_date: df.copy())
    return df


@pytest.mark.unit
def test_price_and_date_present(synth):
    """Preço no momento da análise + data — the headline the owner said was missing."""
    p = ps.build_actionable_plan("SYN", CURR)
    assert p.price == round(float(synth["Close"].iloc[-1]), 2)
    assert p.as_of == synth["Date"].iloc[-1]
    # price is a real close of the series, not fabricated
    assert p.price in set(round(float(v), 2) for v in synth["Close"])


@pytest.mark.unit
def test_timeframe_declared(synth):
    p = ps.build_actionable_plan("SYN", CURR)
    assert "diário" in p.timeframe  # reference timeframe is stated, not left to guess


@pytest.mark.unit
def test_horizon_is_explicit_not_vague(synth):
    """Horizon must be in days/weeks/months, never the rejected 'médio prazo'."""
    p = ps.build_actionable_plan("SYN", CURR)
    assert p.horizon and "médio prazo" not in p.horizon.lower()
    assert any(w in p.horizon.lower() for w in ("dias", "semanas", "meses"))


@pytest.mark.unit
def test_active_setup_has_real_buy_zone(synth):
    """On this frame price sits on the rising MA20 → live setup, buy zone = that MA."""
    p = ps.build_actionable_plan("SYN", CURR)
    assert p.setup_state == "ativo"
    struct = ps.detect_price_structure("SYN", CURR)
    assert struct.active_region is not None
    assert p.buy_zone is not None
    assert p.buy_zone["price"] == struct.active_region.ma_value  # real MA, not invented
    # price is in new-high air here → nothing overhead → explicit "no level"
    assert p.realize_zone is None
    # live setup → no pullback to wait for
    assert p.pullback_zone is None


@pytest.mark.unit
def test_realize_zone_is_a_real_prior_high(monkeypatch):
    """Cut mid-pullback (price below a prior top): the realization region is a real
    swing high sitting overhead, taken from the series — never fabricated."""
    df = _frame()
    cut = df["Date"].iloc[89]  # price ~145, below earlier tops
    cut_df = df[df["Date"] <= cut].reset_index(drop=True)
    monkeypatch.setattr(ps, "load_ohlcv", lambda s, d: cut_df.copy())
    p = ps.build_actionable_plan("SYN", cut)
    assert p.realize_zone is not None
    assert p.realize_zone["price"] > p.price  # resistance is above current price
    highs = set(round(float(v), 2) for v in cut_df["High"])
    assert p.realize_zone["price"] in highs  # a real high, not invented
    assert p.as_of <= cut  # date guard: nothing past the cut


@pytest.mark.unit
def test_thin_series_is_honest_no_levels(monkeypatch):
    tiny = _frame().head(8).reset_index(drop=True)
    monkeypatch.setattr(ps, "load_ohlcv", lambda s, d: tiny.copy())
    p = ps.build_actionable_plan("SYN", CURR)
    assert p.setup_state == "sem_dado"
    assert p.buy_zone is None and p.realize_zone is None and p.pullback_zone is None
    assert "sem" in p.horizon.lower()  # no operable horizon claimed


@pytest.mark.unit
def test_data_failure_never_fabricates(monkeypatch):
    def boom(symbol, curr_date):
        raise RuntimeError("no data")
    monkeypatch.setattr(ps, "load_ohlcv", boom)
    d = ps.build_actionable_plan_dict("SYN", CURR)  # must not raise
    assert d["setup_state"] == "sem_dado"
    assert d["price"] is None
    assert d["buy_zone"] is None and d["realize_zone"] is None and d["pullback_zone"] is None


@pytest.mark.unit
def test_dict_is_json_serializable(synth):
    import json
    d = ps.build_actionable_plan_dict("SYN", CURR)
    json.dumps(d)  # must not raise
    assert set(d) == {
        "symbol", "as_of", "price", "timeframe", "horizon",
        "setup_state", "buy_zone", "realize_zone", "pullback_zone",
    }


@pytest.mark.unit
def test_no_fabricated_numbers_anywhere(synth):
    """Every non-null zone price traces to the series: buy/pullback are moving
    averages the detector produced; realize is a real swing high."""
    p = ps.build_actionable_plan("SYN", CURR)
    struct = ps.detect_price_structure("SYN", CURR)
    ma_values = set()
    if struct.active_region:
        ma_values.add(struct.active_region.ma_value)
    ma_values |= {r.ma_value for r in struct.buy_regions}
    highs = set(round(float(v), 2) for v in synth["High"])
    if p.buy_zone:
        assert p.buy_zone["price"] in ma_values
    if p.pullback_zone and p.setup_state == "aguardar_pullback":
        assert p.pullback_zone["price"] in ma_values
    if p.realize_zone:
        assert p.realize_zone["price"] in highs

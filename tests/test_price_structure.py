"""Deterministic price-structure / setup detection (fork brief 23/08).

The market analyst describes indicators but never identifies the setup the
product owner trades: a pullback-to-a-rising-average buy region and the 1-2-3
reversal. These tests pin the detector on a synthetic series with a KNOWN
structure, so they run offline and can't drift — and they enforce the two hard
rules: nothing is fabricated (every point comes from the series) and nothing sees
a future candle (date guard).
"""
import json

import pandas as pd
import pytest

from tradingagents.dataflows import price_structure as ps
from tradingagents.agents.utils.price_structure_coverage import (
    ensure_price_structure_coverage,
)


def _frame() -> pd.DataFrame:
    """A smooth uptrend with a shallow pullback to MA20 (a buy region) followed by
    a clean 1-2-3: low -> high -> higher-low -> breakout above the high."""
    closes: list[float] = []
    closes += [100 + i for i in range(60)]        # uptrend 100 -> 159
    closes += [156, 150, 146, 150, 156]           # pullback to ~MA20, bounce
    closes += [162 + i for i in range(20)]        # resume up 162 -> 181
    closes += [175, 168, 160, 150, 145]           # down to L1 (~145)
    closes += [150, 160, 172, 185, 190]           # up to H / P2 (~190)
    closes += [182, 175, 168, 162, 166]           # down to L3 (~162) > L1
    closes += [192 + i for i in range(15)]        # breakout above the high
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


def _top_frame() -> pd.DataFrame:
    """Vertical mirror of :func:`_frame` (closes reflected around 300): a smooth
    DOWNTREND with a clean 1-2-3 de venda — high -> low -> lower-high -> breakdown
    below point 2's low. The product owner trades short, so this must be detected
    just like the bottom reversal is."""
    buy: list[float] = []
    buy += [100 + i for i in range(60)]
    buy += [156, 150, 146, 150, 156]
    buy += [162 + i for i in range(20)]
    buy += [175, 168, 160, 150, 145]
    buy += [150, 160, 172, 185, 190]
    buy += [182, 175, 168, 162, 166]
    buy += [192 + i for i in range(15)]
    closes = [300 - c for c in buy]  # reflect: swing lows become swing highs
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


@pytest.fixture
def synth(monkeypatch):
    df = _frame()
    monkeypatch.setattr(ps, "load_ohlcv", lambda symbol, curr_date: df.copy())
    return df


@pytest.fixture
def top(monkeypatch):
    df = _top_frame()
    monkeypatch.setattr(ps, "load_ohlcv", lambda symbol, curr_date: df.copy())
    return df


CURR = "2025-12-31"  # after the whole frame; load_ohlcv (mocked) returns it all


@pytest.mark.unit
def test_detects_123_reversal(synth):
    s = ps.detect_price_structure("SYN", CURR)
    assert s.pattern is not None
    p = s.pattern
    # ascending bottom + trigger is point 2's high, and it broke out -> acionado
    assert p.direction == "compra"
    assert p.p3["price"] > p.p1["price"]
    assert p.trigger == p.p2["price"]
    assert p.state == "acionado"
    # points are ordered in time
    assert p.p1["date"] < p.p2["date"] < p.p3["date"]


@pytest.mark.unit
def test_detects_123_de_venda(top):
    """Descending-top 1-2-3: H -> L -> lower H, trigger = break BELOW point 2's low."""
    s = ps.detect_price_structure("SYN", CURR)
    assert s.pattern is not None
    p = s.pattern
    assert p.direction == "venda"
    # descending top: point 3's high sits BELOW point 1's high
    assert p.p3["price"] < p.p1["price"]
    # trigger is point 2's LOW (break below it), and the series broke down -> acionado
    assert p.trigger == p.p2["price"]
    assert p.state == "acionado"
    assert p.p1["date"] < p.p2["date"] < p.p3["date"]
    # nothing fabricated: every reported price is a real high/low of the series
    df = _top_frame()
    prices = set(round(float(v), 2) for v in df["High"]) | set(round(float(v), 2) for v in df["Low"])
    for pt in (p.p1, p.p2, p.p3):
        assert pt["price"] in prices
    assert p.trigger in prices


@pytest.mark.unit
def test_venda_section_and_plan_carry_direction(top):
    section = ps.build_price_structure_section("SYN", CURR)
    assert "Padrão 1-2-3 de venda" in section
    assert "topo descendente" in section
    plan = ps.build_actionable_plan("SYN", CURR)
    assert plan.pattern is not None and plan.pattern["direction"] == "venda"


@pytest.mark.unit
def test_detects_buy_region_at_average(synth):
    s = ps.detect_price_structure("SYN", CURR)
    assert s.buy_regions, "the pullback to MA20 must surface as a buy region"
    r = s.buy_regions[0]
    assert r.ma_label.startswith("MMS")
    # the region reacted up afterwards (the series bounces)
    assert r.reaction_pct is not None and r.reaction_pct > 0


@pytest.mark.unit
def test_nothing_is_fabricated(synth):
    """Every reported price is a real high/low of the series; every date is real."""
    df = _frame()
    dates = set(df["Date"])
    prices = set(round(float(v), 2) for v in df["High"]) | set(round(float(v), 2) for v in df["Low"])
    s = ps.detect_price_structure("SYN", CURR)
    for r in s.buy_regions:
        assert r.date in dates
        assert r.low in prices
    if s.pattern:
        for pt in (s.pattern.p1, s.pattern.p2, s.pattern.p3):
            assert pt["date"] in dates
            assert pt["price"] in prices
        assert s.pattern.trigger in prices


@pytest.mark.unit
def test_date_guard_no_future_point(monkeypatch):
    """Detection on a past date only sees bars up to it — the mocked load_ohlcv
    returns a frame cut at ``cut``; no reported date may exceed it."""
    df = _frame()
    cut = df["Date"].iloc[80]  # pretend "today" is bar 80
    cut_df = df[df["Date"] <= cut].reset_index(drop=True)
    monkeypatch.setattr(ps, "load_ohlcv", lambda symbol, curr_date: cut_df.copy())
    s = ps.detect_price_structure("SYN", cut)
    chart = ps.build_price_chart("SYN", cut)
    assert chart["candles"][-1]["d"] <= cut
    for r in s.buy_regions:
        assert r.date <= cut
    if s.pattern:
        for pt in (s.pattern.p1, s.pattern.p2, s.pattern.p3):
            assert pt["date"] <= cut


@pytest.mark.unit
def test_thin_series_reports_no_setup(monkeypatch):
    tiny = _frame().head(3).reset_index(drop=True)
    monkeypatch.setattr(ps, "load_ohlcv", lambda symbol, curr_date: tiny.copy())
    s = ps.detect_price_structure("SYN", CURR)
    assert not s.buy_regions and s.pattern is None
    section = ps.build_price_structure_section("SYN", CURR)
    assert "## Estrutura de preço / setups" in section
    assert "nenhum setup identificado" in section.lower()


@pytest.mark.unit
def test_section_data_failure_is_graceful(monkeypatch):
    def boom(symbol, curr_date):
        raise RuntimeError("no data")
    monkeypatch.setattr(ps, "load_ohlcv", boom)
    section = ps.build_price_structure_section("SYN", CURR)
    assert "## Estrutura de preço / setups" in section
    assert "indisponível" in section.lower()
    # the section is always shown (never omitted) — on a data failure the guard
    # appends the explicit "indisponível" note, not a fabricated read.
    out = ensure_price_structure_coverage("CORPO", "SYN", CURR)
    assert out.startswith("CORPO")
    assert "indisponível" in out.lower()


@pytest.mark.unit
def test_section_lists_region_and_pattern(synth):
    section = ps.build_price_structure_section("SYN", CURR)
    assert "Regiões de compra na média" in section
    assert "Padrão 1-2-3 de compra" in section
    assert "Ponto 1" in section and "Gatilho" in section


@pytest.mark.unit
def test_ensure_appends_section(synth):
    out = ensure_price_structure_coverage("Relatório do analista.", "SYN", CURR)
    assert out.startswith("Relatório do analista.")
    assert "## Estrutura de preço / setups" in out


@pytest.mark.unit
def test_chart_payload_is_serializable_and_aligned(synth):
    chart = ps.build_price_chart("SYN", CURR)
    json.dumps(chart)  # must not raise
    n = len(chart["candles"])
    assert n > 2
    for w in ("20", "50", "200"):
        assert len(chart["ma"][w]) == n
    markers = chart["markers"]
    assert set(markers) == {"buy_regions", "active_region", "pattern_123"}
    # markers reference candles inside the window
    window = {c["d"] for c in chart["candles"]}
    for r in markers["buy_regions"]:
        assert r["date"] in window

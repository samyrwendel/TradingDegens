"""Multi-timeframe trend read — weekly + daily, guarded, 24/7-safe.

Fork brief part 1: the market analyst only ever saw the daily frame, and the BE
backtest's one wrong Overweight bought the first candle of a top the weekly had
already started rolling over. These tests feed a synthetic cached series (no
network) and assert both frames are read, the convergence verdict is correct,
the coverage guard appends when the analyst omits a frame, the tool clamps
look-ahead dates, and a weekend analysis date is treated as a normal trading day.
"""
import pandas as pd
import pytest

from tradingagents.agents.utils import multi_timeframe_tools as mtt
from tradingagents.agents.utils.date_guard import base_date
from tradingagents.agents.utils.multi_timeframe_coverage import (
    ensure_multi_timeframe_coverage,
    report_covers_timeframes,
)
from tradingagents.dataflows import multi_timeframe as mtf


def _series(closes, start="2025-01-01"):
    dates = pd.date_range(start=start, periods=len(closes), freq="D")
    return pd.DataFrame(
        {
            "Date": dates,
            "Open": closes,
            "High": [c * 1.01 for c in closes],
            "Low": [c * 0.99 for c in closes],
            "Close": closes,
            "Volume": [1000] * len(closes),
        }
    )


def _uptrend(n=400):
    return _series([100 + i * 0.5 for i in range(n)])


def _downtrend(n=400):
    return _series([100 + (n - i) * 0.5 for i in range(n)])


# ----------------------------------------------------------- resample ----------
@pytest.mark.unit
def test_resample_weekly_aggregates_ohlc():
    daily = _series([10, 11, 12, 13, 14, 15, 16], start="2025-01-06")  # Mon..Sun
    weekly = mtf._resample_weekly(daily)
    assert len(weekly) == 1
    row = weekly.iloc[0]
    assert row["Open"] == 10 and row["Close"] == 16
    assert row["High"] == pytest.approx(16 * 1.01)
    assert row["Low"] == pytest.approx(10 * 0.99)
    assert row["Volume"] == 7000


# -------------------------------------------------------------- trend ----------
@pytest.mark.unit
def test_trend_read_labels_up_and_down():
    up = mtf._trend_read(_uptrend(), fast=10, slow=50, label="Daily")
    down = mtf._trend_read(_downtrend(), fast=10, slow=50, label="Daily")
    assert up["state"] == "uptrend"
    assert down["state"] == "downtrend"


# ------------------------------------------------------ summary + verdict ------
@pytest.mark.unit
def test_summary_cites_both_frames_and_converges(monkeypatch):
    monkeypatch.setattr(mtf, "load_ohlcv", lambda sym, cd: _uptrend())
    out = mtf.build_timeframe_summary("AAA", "2026-01-01")
    assert "Weekly" in out and "Daily" in out
    assert "Converge" in out


@pytest.mark.unit
def test_summary_flags_divergence(monkeypatch):
    # Force opposite frame states (weekly up, daily down) to assert the verdict
    # composition deterministically, without fighting SMA-stack arithmetic.
    monkeypatch.setattr(mtf, "load_ohlcv", lambda sym, cd: _uptrend())

    def fake_read(frame, fast, slow, label):
        state = "uptrend" if label == "Weekly" else "downtrend"
        return {"label": label, "state": state, "last": 100.0, "fast": 1.0,
                "slow": 1.0, "fast_n": fast, "slow_n": slow, "chg": 0.0, "bars": 400}

    monkeypatch.setattr(mtf, "_trend_read", fake_read)
    out = mtf.build_timeframe_summary("AAA", "2026-01-01")
    assert "Divergent" in out


# ----------------------------------------------------------- coverage ----------
@pytest.mark.unit
def test_report_covers_timeframes_detection():
    assert report_covers_timeframes("weekly trend up, daily pulling back")
    assert not report_covers_timeframes("daily RSI is 55")   # weekly missing
    assert not report_covers_timeframes("")


@pytest.mark.unit
def test_coverage_appends_when_missing(monkeypatch):
    monkeypatch.setattr(
        mtf, "load_ohlcv", lambda sym, cd: _uptrend()
    )
    report = "Daily RSI looks fine.\n"   # no 'weekly'
    out = ensure_multi_timeframe_coverage(report, "AAA", "2026-01-01")
    assert "Multi-Timeframe Trend" in out
    assert "Weekly" in out


@pytest.mark.unit
def test_coverage_noop_when_present():
    report = "Weekly is up, daily is up. All good."
    assert ensure_multi_timeframe_coverage(report, "AAA", "2026-01-01") == report


@pytest.mark.unit
def test_coverage_degrades_on_data_failure(monkeypatch):
    def boom(sym, cd):
        raise RuntimeError("no such symbol")

    monkeypatch.setattr(mtf, "load_ohlcv", boom)
    out = ensure_multi_timeframe_coverage("daily only", "BADSYM", "2026-01-01")
    assert "unavailable" in out.lower()
    assert "fabricat" in out.lower()


# ------------------------------------------------------ guard + 24/7 -----------
@pytest.mark.unit
def test_tool_clamps_curr_date(monkeypatch):
    captured = {}

    def fake_build(symbol, curr_date):
        captured["curr_date"] = curr_date
        return "ok"

    monkeypatch.setattr(mtt, "build_timeframe_summary", fake_build)
    with base_date("2026-06-01"):
        mtt.get_price_timeframes.invoke({"symbol": "AAA", "curr_date": "2026-08-23"})
    assert captured["curr_date"] == "2026-06-01"


@pytest.mark.unit
def test_weekend_date_is_a_normal_trading_day(monkeypatch):
    # 2026-01-03 is a Saturday. Crypto trades weekends; the read must still work
    # and must not claim "not a trading day".
    monkeypatch.setattr(mtf, "load_ohlcv", lambda sym, cd: _uptrend())
    out = mtf.build_timeframe_summary("BTC-USD", "2026-01-03")
    assert "not a trading day" not in out.lower()
    assert "Daily" in out and "Weekly" in out

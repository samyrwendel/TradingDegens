"""Keyless intraday (15m/1h/4h) OHLCV loader — the data foundation for the Erick
model (fork brief 24/08, extended 25/08 for equities).

Network-free: the two source seams (:func:`_binance_klines` for crypto,
:func:`_yf_intraday_download` for equity) are monkeypatched, so these pin the hard
rules without hitting any network:

* **two real keyless sources** — crypto reads native Binance klines; an equity
  reads yfinance intraday (15m/1h native, 4h resampled per trading session). A
  source that returns nothing declares intraday *unavailable*, never invents a bar;
* **no look-ahead** — a backtest date drops any bar past that day's end, so an
  intraday bar printed today cannot leak into a past-dated analysis;
* **cache like the daily path (DA-058)** — the repeat of a historical request is
  served from disk with zero network.
"""
from datetime import datetime, timezone

import pandas as pd
import pytest

from tradingagents.dataflows import intraday as it
from tradingagents.dataflows.intraday import (
    IntradayUnavailableError,
    load_intraday_ohlcv,
)


def _yf_frame(start: str, n: int, freq: str, base: float = 100.0, tz: str = "UTC") -> pd.DataFrame:
    """A yfinance-shaped intraday frame: tz-aware ``Datetime`` index + OHLCV cols.

    Mirrors what ``yf.download(interval=...)`` returns so ``_yf_intraday_to_df`` is
    exercised exactly as in production (tz-aware index normalised to naive UTC)."""
    idx = pd.date_range(start, periods=n, freq=freq, tz=tz)
    idx.name = "Datetime"
    close = [base + i for i in range(n)]
    return pd.DataFrame(
        {
            "Open": close,
            "High": [c + 1 for c in close],
            "Low": [c - 1 for c in close],
            "Close": close,
            "Volume": [10.0] * n,
        },
        index=idx,
    )


def _ms(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _kline(dt: datetime, close: float) -> list:
    """A minimal Binance spot kline row: [openTime, o, h, l, c, v, ...]."""
    o = close
    return [_ms(dt), f"{o:.2f}", f"{o + 1:.2f}", f"{o - 1:.2f}", f"{close:.2f}", "10.0", _ms(dt) + 1]


def _series(start: datetime, n: int, step_min: int, base: float = 100.0) -> list:
    """``n`` consecutive klines every ``step_min`` minutes from ``start``."""
    return [_kline(start + pd.Timedelta(minutes=step_min * i), base + i) for i in range(n)]


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    """Point the intraday file cache at a fresh temp dir."""
    monkeypatch.setattr(it, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    return tmp_path


# -------------------------------------------------------- source routing -------
@pytest.mark.unit
def test_equity_symbol_does_not_hit_the_crypto_seam(cache_dir, monkeypatch):
    """An equity must read yfinance, never the exchange kline endpoint."""
    monkeypatch.setattr(it, "_binance_klines", lambda *a, **k: pytest.fail("must not fetch crypto"))
    monkeypatch.setattr(it, "_yf_intraday_download",
                        lambda canonical, itv, start, end: _yf_frame("2026-08-20 13:30", 40, "15min"))
    df = load_intraday_ohlcv("MSFT", datetime.now(timezone.utc).strftime("%Y-%m-%d"), "15m")
    assert len(df) == 40


@pytest.mark.unit
def test_unsupported_interval_rejected(cache_dir):
    with pytest.raises(ValueError):
        load_intraday_ohlcv("BTC-USD", "2020-01-15", "3m")


@pytest.mark.unit
@pytest.mark.parametrize("interval,step", [("15m", 15), ("1h", 60), ("4h", 240)])
def test_crypto_intraday_returns_real_candles(cache_dir, monkeypatch, interval, step):
    """15m, 1h and 4h all parse into a clean OHLCV frame with a datetime Date."""
    start = datetime(2020, 1, 10, 0, 0)
    rows = _series(start, 40, step)
    monkeypatch.setattr(it, "_binance_klines", lambda base, itv, end_ms, limit=1000: rows)

    df = load_intraday_ohlcv("BTC-USD", "2020-01-15", interval)
    assert list(df.columns[:6]) == ["Date", "Open", "High", "Low", "Close", "Volume"]
    assert pd.api.types.is_datetime64_any_dtype(df["Date"])
    assert (df["High"] >= df["Low"]).all()
    assert len(df) > 10


@pytest.mark.unit
def test_4h_is_a_native_exchange_interval(cache_dir, monkeypatch):
    """4h is pulled straight from Binance's NATIVE ``4h`` kline, not resampled from
    1h — so the bar is aligned to the exchange's own 4h boundaries (fork brief
    24/08 task 005, decision documented in intraday.py)."""
    seen = {}
    rows = _series(datetime(2020, 1, 10, 0, 0), 40, 240)

    def capture(base, itv, end_ms, limit=1000):
        seen["interval"] = itv
        return rows

    monkeypatch.setattr(it, "_binance_klines", capture)
    load_intraday_ohlcv("BTC-USD", "2020-01-15", "4h")
    assert seen["interval"] == "4h", "4h must map to Binance's native 4h interval"


@pytest.mark.unit
def test_4h_backtest_drops_bars_after_requested_day(cache_dir, monkeypatch):
    """date_guard holds on 4h too: a 4h bar printed after the requested day's end
    cannot leak into a past-dated analysis (criterion 3)."""
    # 4h bars spanning 2020-01-15 into 2020-01-16; only D1 may survive.
    rows = _series(datetime(2020, 1, 15, 12, 0), 12, 240)  # 12:00 D1 → into D2/D3
    monkeypatch.setattr(it, "_binance_klines", lambda *a, **k: rows)

    df = load_intraday_ohlcv("BTC-USD", "2020-01-15", "4h")
    cutoff = pd.Timestamp("2020-01-15 23:59:59")
    assert df["Date"].max() <= cutoff
    assert (df["Date"].dt.date == pd.Timestamp("2020-01-15").date()).all()


# ------------------------------------------------------------- date guard ------
@pytest.mark.unit
def test_backtest_drops_bars_after_requested_day(cache_dir, monkeypatch):
    """A 15m bar printed AFTER the requested day must not leak backward.

    The feed (or a reused cache) hands back bars spanning the requested day and
    the next one; only bars on/before the requested day's end may survive.
    """
    # 15m bars across 2020-01-15 (kept) into 2020-01-16 (must be dropped).
    rows = _series(datetime(2020, 1, 15, 22, 0), 20, 15)  # 22:00 D1 → ~01:45 D2
    monkeypatch.setattr(it, "_binance_klines", lambda *a, **k: rows)

    df = load_intraday_ohlcv("BTC-USD", "2020-01-15", "15m")
    cutoff = pd.Timestamp("2020-01-15 23:59:59")
    assert df["Date"].max() <= cutoff
    assert (df["Date"].dt.date == pd.Timestamp("2020-01-15").date()).all()


@pytest.mark.unit
def test_live_run_keeps_recent_bars(cache_dir, monkeypatch):
    """A live (today/future) request keeps the recent bars, no backward clamp."""
    now = datetime.now(timezone.utc).replace(tzinfo=None, second=0, microsecond=0)
    rows = [_kline(now - pd.Timedelta(minutes=15 * i), 100 + i) for i in range(30)][::-1]
    monkeypatch.setattr(it, "_binance_klines", lambda *a, **k: rows)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    df = load_intraday_ohlcv("BTC-USD", today, "15m")
    assert len(df) == 30


# ----------------------------------------------------------------- cache -------
@pytest.mark.unit
def test_historical_repeat_is_zero_network(cache_dir, monkeypatch):
    """DA-058: the 2nd fetch of a past date is served from disk, no network."""
    calls = {"n": 0}
    rows = _series(datetime(2020, 1, 14, 0, 0), 40, 15)

    def counting(base, itv, end_ms, limit=1000):
        calls["n"] += 1
        return rows

    monkeypatch.setattr(it, "_binance_klines", counting)

    a = load_intraday_ohlcv("BTC-USD", "2020-01-15", "15m")
    b = load_intraday_ohlcv("BTC-USD", "2020-01-15", "15m")
    assert calls["n"] == 1, "historical repeat must hit the cache, not the network"
    pd.testing.assert_frame_equal(a.reset_index(drop=True), b.reset_index(drop=True))

    # A different historical date is a real miss (separate cache key). The rows
    # all fall on 2020-01-14, so request that day (a distinct key the bars cover).
    load_intraday_ohlcv("BTC-USD", "2020-01-14", "15m")
    assert calls["n"] == 2


@pytest.mark.unit
def test_empty_feed_raises_not_fabricates(cache_dir, monkeypatch):
    monkeypatch.setattr(it, "_binance_klines", lambda *a, **k: [])
    with pytest.raises(Exception):
        load_intraday_ohlcv("BTC-USD", "2020-01-15", "15m")


# ------------------------------------------------------- equity (yfinance) -----
@pytest.mark.unit
@pytest.mark.parametrize("interval,freq", [("15m", "15min"), ("1h", "60min")])
def test_equity_intraday_native_frames(cache_dir, monkeypatch, interval, freq):
    """15m and 1h are native yfinance intervals: they parse into the SAME naive-UTC
    OHLCV shape the crypto path yields, so the detector reads both identically."""
    monkeypatch.setattr(it, "_yf_intraday_download",
                        lambda canonical, itv, start, end: _yf_frame("2026-08-18 13:30", 60, freq, tz="America/New_York"))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    df = load_intraday_ohlcv("MSFT", today, interval)
    assert list(df.columns[:6]) == ["Date", "Open", "High", "Low", "Close", "Volume"]
    assert pd.api.types.is_datetime64_any_dtype(df["Date"])
    assert df["Date"].dt.tz is None, "intraday timestamps are normalised to naive UTC"
    assert (df["High"] >= df["Low"]).all()
    assert len(df) == 60


@pytest.mark.unit
def test_equity_1h_maps_to_yfinance_60m(cache_dir, monkeypatch):
    """1h must request yfinance's ``60m`` interval (its native hourly bar)."""
    seen = {}

    def capture(canonical, itv, start, end):
        seen["interval"] = itv
        return _yf_frame("2026-08-18 13:30", 40, "60min")

    monkeypatch.setattr(it, "_yf_intraday_download", capture)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    load_intraday_ohlcv("MSFT", today, "1h")
    assert seen["interval"] == "60m"


@pytest.mark.unit
def test_equity_4h_resampled_per_session_no_overnight_bar(cache_dir, monkeypatch):
    """4h is resampled from the 1h series PER TRADING DAY: a US session's 7 hourly
    bars chunk into 2 four-hour bars, and day 2's first bar opens on day 2 — the
    overnight gap is never merged into a fabricated bar (equity is not 24/7)."""
    # Two sessions of 7 hourly bars each (13:30..19:30 UTC — a US cash session).
    day1 = _yf_frame("2026-08-18 13:30", 7, "60min", base=100.0)
    day2 = _yf_frame("2026-08-19 13:30", 7, "60min", base=200.0)
    hourly = pd.concat([day1, day2])
    monkeypatch.setattr(it, "_yf_intraday_download", lambda *a, **k: hourly)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    df = load_intraday_ohlcv("MSFT", today, "4h")
    # 7 bars/day -> chunks of 4 -> 2 bars/day -> 4 bars total, none straddling days.
    assert len(df) == 4
    days = df["Date"].dt.date.tolist()
    assert days.count(pd.Timestamp("2026-08-18").date()) == 2
    assert days.count(pd.Timestamp("2026-08-19").date()) == 2
    # The first bar of day 2 opens at day 2's session start, not merged with day 1.
    day2_first = df[df["Date"].dt.date == pd.Timestamp("2026-08-19").date()]["Date"].min()
    assert day2_first == pd.Timestamp("2026-08-19 13:30")
    # OHLC of day 1's first 4h bar aggregates its first 4 hourly bars honestly.
    first = df.iloc[0]
    assert first["Open"] == 100.0 and first["High"] == 104.0  # base..base+3, +1 high
    assert first["Close"] == 103.0


@pytest.mark.unit
def test_equity_intraday_empty_declares_unavailable(cache_dir, monkeypatch):
    """A source that returns nothing (date beyond yfinance's intraday window, or a
    transient outage) declares intraday unavailable — never fabricates a bar."""
    monkeypatch.setattr(it, "_yf_intraday_download",
                        lambda *a, **k: pd.DataFrame())
    with pytest.raises(IntradayUnavailableError):
        load_intraday_ohlcv("MSFT", "2019-01-15", "15m")


@pytest.mark.unit
def test_equity_intraday_source_error_declares_unavailable(cache_dir, monkeypatch):
    """A raising source (yfinance error) is caught and re-declared as unavailable,
    so a vendor hiccup degrades honestly instead of crashing the caller."""
    def boom(*a, **k):
        raise RuntimeError("yfinance exploded")

    monkeypatch.setattr(it, "_yf_intraday_download", boom)
    with pytest.raises(IntradayUnavailableError):
        load_intraday_ohlcv("MSFT", datetime.now(timezone.utc).strftime("%Y-%m-%d"), "15m")


@pytest.mark.unit
def test_equity_intraday_backtest_drops_bars_after_requested_day(cache_dir, monkeypatch):
    """date_guard holds for equity too: bars printed after the requested day's end
    cannot leak into a past-dated analysis."""
    rows = _yf_frame("2026-08-20 13:30", 20, "60min")  # spills into 2026-08-21
    monkeypatch.setattr(it, "_yf_intraday_download", lambda *a, **k: rows)
    df = load_intraday_ohlcv("MSFT", "2026-08-20", "1h")
    cutoff = pd.Timestamp("2026-08-20 23:59:59")
    assert df["Date"].max() <= cutoff
    assert (df["Date"].dt.date == pd.Timestamp("2026-08-20").date()).all()


@pytest.mark.unit
def test_equity_historical_repeat_is_zero_network(cache_dir, monkeypatch):
    """DA-058 for equity: the 2nd fetch of a past date is served from disk."""
    calls = {"n": 0}
    rows = _yf_frame("2026-08-20 13:30", 8, "60min")

    def counting(canonical, itv, start, end):
        calls["n"] += 1
        return rows

    monkeypatch.setattr(it, "_yf_intraday_download", counting)
    a = load_intraday_ohlcv("MSFT", "2026-08-20", "1h")
    b = load_intraday_ohlcv("MSFT", "2026-08-20", "1h")
    assert calls["n"] == 1, "historical repeat must hit the cache, not the network"
    pd.testing.assert_frame_equal(a.reset_index(drop=True), b.reset_index(drop=True))

"""Weekly + daily trend read for the market analyst (fork addition).

The stock market analyst upstream only ever sees the **daily** frame. The BE
backtest exposed the cost: its single wrong ``Overweight`` (14 Aug) bought the
first candle of a top correction — fundamentals and macro are slow and miss the
inflection, but the weekly frame was already rolling over. Trend on the higher
timeframe, timing on the lower one; when the two disagree, that divergence is
signal, not noise.

This builds a compact, deterministic read of both frames from a single cached
daily series:

* The daily OHLCV comes from :func:`load_ohlcv`, which is already cached per
  symbol and **cut to ``<= curr_date``** — so nothing here can see the future.
* The weekly frame is *resampled in memory* from that same cut series (no extra
  network, no separate look-ahead surface). The last weekly bar may be a
  still-forming partial week; that is correct, since it only aggregates days up
  to ``curr_date``.

Works for stocks and crypto alike: crypto is 24/7, so the resample simply yields
7-day weekly bars with weekend days included — no business-day assumption.
"""
from __future__ import annotations

import logging

import pandas as pd

from .stockstats_utils import load_ohlcv

logger = logging.getLogger(__name__)

# Weekly bars close on Sunday so a 7-day crypto week and a Mon–Fri equity week
# both roll into one bar; the last (current) bar may be partial.
_WEEKLY_RULE = "W-SUN"


def _resample_weekly(daily: pd.DataFrame) -> pd.DataFrame:
    d = daily.copy()
    d["Date"] = pd.to_datetime(d["Date"], errors="coerce")
    d = d.dropna(subset=["Date"]).set_index("Date").sort_index()
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    if "Volume" in d.columns:
        agg["Volume"] = "sum"
    weekly = d.resample(_WEEKLY_RULE).agg(agg).dropna(subset=["Close"])
    return weekly.reset_index()


def _sma(series: pd.Series, window: int) -> float | None:
    if len(series) < window:
        return None
    return float(series.tail(window).mean())


def _trend_read(frame: pd.DataFrame, fast: int, slow: int, label: str) -> dict:
    """Direction + SMA alignment for one timeframe."""
    closes = frame["Close"].astype(float).reset_index(drop=True)
    if closes.empty:
        return {"label": label, "state": "no data", "bars": 0}
    last = float(closes.iloc[-1])
    fast_sma = _sma(closes, fast)
    slow_sma = _sma(closes, slow)

    # Momentum over the slow window: last close vs close `slow` bars back.
    if len(closes) > slow:
        ref = float(closes.iloc[-slow - 1])
        chg = (last / ref - 1) * 100 if ref else 0.0
    else:
        chg = None

    if fast_sma is not None and slow_sma is not None:
        if last > fast_sma > slow_sma:
            state = "uptrend"
        elif last < fast_sma < slow_sma:
            state = "downtrend"
        elif last > slow_sma:
            state = "up-biased / mixed"
        else:
            state = "down-biased / mixed"
    else:
        state = "insufficient history for full SMA stack"

    return {
        "label": label,
        "state": state,
        "last": last,
        "fast": fast_sma,
        "slow": slow_sma,
        "fast_n": fast,
        "slow_n": slow,
        "chg": chg,
        "bars": len(closes),
    }


def _direction(state: str) -> str:
    if "uptrend" in state or state.startswith("up"):
        return "up"
    if "downtrend" in state or state.startswith("down"):
        return "down"
    return "flat"


def _fmt_read(r: dict) -> str:
    if r.get("bars", 0) == 0:
        return f"- **{r['label']}**: no data."
    parts = [f"last {r['last']:,.2f}"]
    if r.get("fast") is not None:
        parts.append(f"SMA{r['fast_n']} {r['fast']:,.2f}")
    if r.get("slow") is not None:
        parts.append(f"SMA{r['slow_n']} {r['slow']:,.2f}")
    if r.get("chg") is not None:
        parts.append(f"{r['chg']:+.1f}% over last {r['slow_n']} bars")
    return f"- **{r['label']}** → _{r['state']}_ ({'; '.join(parts)})."


def build_timeframe_summary(symbol: str, curr_date: str) -> str:
    """Return a markdown weekly+daily trend read, with a convergence verdict.

    Raises nothing on a thin series — it reports what history allows and labels
    the rest ``insufficient history`` — but propagates a hard data failure
    (unknown symbol) from :func:`load_ohlcv` so the caller degrades cleanly.
    """
    daily = load_ohlcv(symbol, curr_date)
    weekly = _resample_weekly(daily)

    daily_read = _trend_read(daily, fast=10, slow=50, label="Daily")
    weekly_read = _trend_read(weekly, fast=10, slow=40, label="Weekly")

    wd, dd = _direction(weekly_read["state"]), _direction(daily_read["state"])
    if wd == dd and wd != "flat":
        verdict = f"**Converge** — weekly and daily both {wd}. Trend and timing agree."
    elif {wd, dd} == {"up", "down"}:
        verdict = (
            "**Divergent** — weekly and daily point opposite ways. Treat as a "
            "warning: the higher timeframe (weekly) sets the dominant trend, the "
            "daily is the timing/inflection signal."
        )
    else:
        verdict = (
            f"**Mixed** — weekly {wd}, daily {dd}. No clean multi-timeframe "
            "alignment; weight the weekly for trend, the daily for entry timing."
        )

    return "\n".join(
        [
            "## Multi-Timeframe Trend",
            "",
            "_Higher timeframe = dominant trend; lower timeframe = timing. "
            "Divergence between them is signal, not noise._",
            "",
            _fmt_read(weekly_read),
            _fmt_read(daily_read),
            "",
            verdict,
        ]
    )

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
            state = "tendência de alta"
        elif last < fast_sma < slow_sma:
            state = "tendência de baixa"
        elif last > slow_sma:
            state = "viés de alta / misto"
        else:
            state = "viés de baixa / misto"
    else:
        state = "histórico insuficiente para o empilhamento completo de médias"

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
    if "alta" in state:
        return "up"
    if "baixa" in state:
        return "down"
    return "flat"


# Direction key -> pt-BR word for display in the convergence verdict.
_DIR_PT = {"up": "alta", "down": "baixa", "flat": "lateralizado"}


def _fmt_read(r: dict) -> str:
    if r.get("bars", 0) == 0:
        return f"- **{r['label']}**: sem dados."
    parts = [f"último {r['last']:,.2f}"]
    if r.get("fast") is not None:
        parts.append(f"MMS{r['fast_n']} {r['fast']:,.2f}")
    if r.get("slow") is not None:
        parts.append(f"MMS{r['slow_n']} {r['slow']:,.2f}")
    if r.get("chg") is not None:
        parts.append(f"{r['chg']:+.1f}% nos últimos {r['slow_n']} períodos")
    return f"- **{r['label']}** → _{r['state']}_ ({'; '.join(parts)})."


def build_timeframe_summary(symbol: str, curr_date: str) -> str:
    """Return a markdown weekly+daily trend read, with a convergence verdict.

    Raises nothing on a thin series — it reports what history allows and labels
    the rest ``insufficient history`` — but propagates a hard data failure
    (unknown symbol) from :func:`load_ohlcv` so the caller degrades cleanly.
    """
    daily = load_ohlcv(symbol, curr_date)
    weekly = _resample_weekly(daily)

    daily_read = _trend_read(daily, fast=10, slow=50, label="Diário")
    weekly_read = _trend_read(weekly, fast=10, slow=40, label="Semanal")

    wd, dd = _direction(weekly_read["state"]), _direction(daily_read["state"])
    if wd == dd and wd != "flat":
        verdict = (
            f"**Convergência** — semanal e diário ambos em {_DIR_PT[wd]}. "
            "Tendência e momento de entrada concordam."
        )
    elif {wd, dd} == {"up", "down"}:
        verdict = (
            "**Divergência** — semanal e diário apontam em direções opostas. "
            "Trate como alerta: o tempo gráfico maior (semanal) define a "
            "tendência dominante; o diário é o sinal de momento/inflexão."
        )
    else:
        verdict = (
            f"**Misto** — semanal em {_DIR_PT[wd]}, diário em {_DIR_PT[dd]}. Sem "
            "alinhamento limpo entre tempos gráficos; priorize o semanal para a "
            "tendência e o diário para o momento de entrada."
        )

    return "\n".join(
        [
            "## Tendência multiperíodo (multi-timeframe)",
            "",
            "_Tempo gráfico maior = tendência dominante; tempo gráfico menor = "
            "momento de entrada. Divergência entre eles é sinal, não ruído._",
            "",
            _fmt_read(weekly_read),
            _fmt_read(daily_read),
            "",
            verdict,
        ]
    )

"""Deterministic price-structure / setup detection (fork addition).

The market analyst calculates *indicators* (RSI, MACD, moving averages,
Bollinger) but never looks for **price structure** — the setup the product owner
actually trades:

* **Região de compra na média** — the price pulls back to a rising moving
  average, touches it, and reacts up from there. Buy at the touch and hold; over
  days-to-months it tends to pay.
* **Padrão 1-2-3, both directions** — the classic reversal in either sense:
  * *de compra* (bottom): point 1 a swing low, point 2 the next swing high
    (repique), point 3 a HIGHER swing low (ascending bottom); trigger = break
    ABOVE point 2's high.
  * *de venda* (top): point 1 a swing high, point 2 the next swing low, point 3 a
    LOWER swing high (descending top); trigger = break BELOW point 2's low. The
    product owner trades short, so a top-reversal read is half the job.

Both are detected here from the SAME cached daily series the rest of the engine
uses (:func:`load_ohlcv`, already cut to ``<= curr_date``), so nothing can see a
future candle — a detection run on a past date only sees bars up to that date.
Every reported point carries a real date and price from the series; no number is
fabricated.

Operable levels are reported as a **band** (mín–máx), not a single price — a
centavo-exact "region" is false precision and inoperable. The band width is the
recent **ATR** (average true range of the last :data:`_ATR_PERIOD` bars), a
volatility measure read straight from the series; when there is no ATR basis a
level degrades to a point and says so, never a cosmetic percentage.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .intraday import (
    INTRADAY_INTERVALS,
    IntradayUnavailableError,
    load_intraday_ohlcv,
)
from .stockstats_utils import load_ohlcv

logger = logging.getLogger(__name__)

# Moving averages we scan for a pullback-to-support touch. 50 and 200 are the
# usual reference; 20 catches the shallower pullbacks of a steep trend.
_MA_WINDOWS = (20, 50, 200)

# Exponential moving averages the product owner actually reads off the screen —
# EMA 8/21 for timing, EMA 50 for the intermediate trend (fork brief 24/08). They
# are computed and charted ALONGSIDE the simple averages (``_MA_WINDOWS``); the
# pullback/1-2-3 detection still keys off the simple averages, so nothing that
# already worked changes — this only *adds* the exponential line to the picture.
_EMA_WINDOWS = (8, 21, 50)

# Timeframes this detector runs on. The daily/weekly frames come from the cached
# yfinance series; the intraday frames (15m/1h/4h) come from the keyless-exchange
# loader and only exist for crypto (see :mod:`.intraday`).
_DEFAULT_TIMEFRAME = "1d"

# Human labels for the timeframe stamped on sections/plans (pt-BR).
_TF_LABEL = {
    "15m": "15 minutos (intradiário)",
    "1h": "1 hora (intradiário)",
    "4h": "4 horas (intradiário)",
    "1d": "diário",
    "1w": "semanal",
}


def _is_intraday(timeframe: str) -> bool:
    return timeframe in INTRADAY_INTERVALS


def _date_fmt(timeframe: str) -> str:
    """Intraday points need the time-of-day; daily/weekly are date-only."""
    return "%Y-%m-%d %H:%M" if _is_intraday(timeframe) else "%Y-%m-%d"


def _tf_label(timeframe: str) -> str:
    return _TF_LABEL.get(timeframe, timeframe)

# Swing look-around: a bar is a swing low/high when its Low/High is the extreme
# of the [i-k, i+k] window. k bars must exist on BOTH sides, so the most recent k
# bars can't form a *confirmed* swing yet — which is correct, not a bug.
_SWING_K = 5

# A pullback "touches" a moving average when the swing low is within this band of
# it (fraction). Wide enough for a fast trend whose MA lags, tight enough that a
# knife-catch far below a falling MA doesn't qualify (the rising+was-above guards
# also gate that).
_TOUCH_TOL = 0.08

# Forward window (bars) over which a region's reaction (best close gain) is read.
_REACT_BARS = 40

# Volatility band for an operable zone. The width is ATR over the last
# ``_ATR_PERIOD`` bars (the standard 14) — a real reading off the series, not a
# guessed percentage — and a zone spans ``anchor ± _ZONE_HALF_ATR·ATR`` so its
# total width is ~one ATR: the distance price typically travels, which is what
# makes "compre na região" operable instead of "compre neste centavo".
_ATR_PERIOD = 14
_ZONE_HALF_ATR = 0.5
_BAND_BASIS = f"±{_ZONE_HALF_ATR:g}·ATR{_ATR_PERIOD}"

# Estrutura CIENTE DO MÉTODO (fork brief 24/08). O "recuo à média" que cada método
# opera é numa família de médias DIFERENTE, então a detecção passa a keyar na média
# do método — o confronto Padrão × Erick deixa de ser o mesmo overlay 2x:
#   • Padrão  → MÉDIAS SIMPLES (MMS 20/50/200), como sempre;
#   • Erick   → EMAs de timing (8/21), a média que o método realmente lê na tela.
# Muda a MÉDIA de referência do recuo (regiões de compra/ativa) e o horizonte de
# swing (Erick = timing mais curto → k menor → 1-2-3 e topo anterior mais recentes).
# O que continua lido do PREÇO (velas, swings) é o mesmo — não se fabrica diferença.
_METHOD_MAS: dict[str, tuple[tuple[str, str], ...]] = {
    "padrao": tuple((f"MMS{w}", f"MA{w}") for w in _MA_WINDOWS),
    "erick": tuple((f"EMA{w}", f"EMA{w}") for w in (8, 21)),
}
# Sensibilidade de swing por método: Erick opera timing mais curto (EMA 8/21), então
# enxerga reversões mais recentes/apertadas; Padrão usa a janela larga de sempre.
_METHOD_SWING_K = {"padrao": _SWING_K, "erick": 3}
_DEFAULT_METHOD = "padrao"


def _method_mas(method: str) -> tuple[tuple[str, str], ...]:
    """Família de médias (rótulo, coluna) que o método usa pro recuo à média."""
    return _METHOD_MAS.get(method or _DEFAULT_METHOD, _METHOD_MAS[_DEFAULT_METHOD])


def _method_k(method: str) -> int:
    """Janela de swing (look-around) do método."""
    return _METHOD_SWING_K.get(method or _DEFAULT_METHOD, _SWING_K)


def _atr(df: pd.DataFrame, period: int = _ATR_PERIOD) -> float | None:
    """Average true range of the last ``period`` bars, read from the real series.

    ``None`` when the series is too short for a full window (so the caller
    degrades a zone to a point rather than inventing a width)."""
    if len(df) < period + 1:
        return None
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    prev_close = df["Close"].astype(float).shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    if pd.isna(atr) or atr <= 0:
        return None
    return round(float(atr), 2)


def _band(anchor: float | None, atr: float | None) -> tuple[float | None, float | None]:
    """(low, high) = ``anchor ± _ZONE_HALF_ATR·atr``; (None, None) without a basis."""
    if anchor is None or atr is None:
        return None, None
    half = _ZONE_HALF_ATR * atr
    return round(anchor - half, 2), round(anchor + half, 2)


def _banded(zone: dict[str, Any] | None, atr: float | None) -> dict[str, Any] | None:
    """Attach a min–max band around ``zone['price']``. With no ATR basis the band
    is ``None`` and the UI renders the anchor as a point (and says it is a point)."""
    if zone is None:
        return None
    low, high = _band(zone.get("price"), atr)
    return {
        **zone,
        "low": low,
        "high": high,
        "band_basis": _BAND_BASIS if low is not None else None,
    }


@dataclass
class BuyRegion:
    date: str
    ma_label: str
    low: float
    ma_value: float
    distance_pct: float
    reaction_pct: float | None  # None when there is no forward data yet

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "ma_label": self.ma_label,
            "low": self.low,
            "ma_value": self.ma_value,
            "distance_pct": self.distance_pct,
            "reaction_pct": self.reaction_pct,
        }


@dataclass
class Pattern123:
    p1: dict[str, Any]  # {"date", "price"}
    p2: dict[str, Any]
    p3: dict[str, Any]
    trigger: float
    state: str  # "acionado" | "formando"
    direction: str  # "compra" (bottom) | "venda" (top)

    def as_dict(self) -> dict[str, Any]:
        return {
            "p1": self.p1, "p2": self.p2, "p3": self.p3,
            "trigger": self.trigger, "state": self.state,
            "direction": self.direction,
        }


@dataclass
class PriceStructure:
    symbol: str
    as_of: str
    buy_regions: list[BuyRegion] = field(default_factory=list)
    active_region: BuyRegion | None = None
    pattern: Pattern123 | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "as_of": self.as_of,
            "buy_regions": [r.as_dict() for r in self.buy_regions],
            "active_region": self.active_region.as_dict() if self.active_region else None,
            "pattern": self.pattern.as_dict() if self.pattern else None,
        }


# Weekly bars close on Sunday so a 7-day crypto week and a Mon–Fri equity week
# both roll into one bar (matches multi_timeframe.py's trend read).
_WEEKLY_RULE = "W-SUN"


def _resample_weekly(daily: pd.DataFrame, curr_date: str) -> pd.DataFrame:
    """Resample a date-guarded DAILY frame to weekly (Sun-closed) bars in memory.

    Reuses the daily series (already cached per symbol and cut to ``<= curr_date``
    by :func:`load_ohlcv`), so the weekly frame costs no extra network — the same
    DA-058 daily cache covers it; there is no separate look-ahead surface.

    date guard: the still-forming / future week is dropped — a weekly bar counts
    only once its week-ending Sunday has arrived (``<= curr_date``). Under
    ``W-SUN`` the bin label IS that ending Sunday, so any label after ``curr_date``
    is a week that has not closed yet and must never be shown as a completed candle.
    """
    d = daily.copy()
    d["Date"] = pd.to_datetime(d["Date"], errors="coerce")
    d = d.dropna(subset=["Date"]).set_index("Date").sort_index()
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    if "Volume" in d.columns:
        agg["Volume"] = "sum"
    weekly = d.resample(_WEEKLY_RULE).agg(agg).dropna(subset=["Close"])
    cutoff = pd.to_datetime(curr_date, errors="coerce")
    if pd.notna(cutoff):
        weekly = weekly[weekly.index <= cutoff]
    return weekly.reset_index()


def _load_frame(symbol: str, curr_date: str, timeframe: str) -> pd.DataFrame:
    """Load the date-guarded OHLCV for ``timeframe``.

    * daily — the cached yfinance series (:func:`load_ohlcv`);
    * weekly — that same daily series resampled in memory (:func:`_resample_weekly`),
      so it needs no separate feed and is operable for stocks and crypto alike;
    * intraday (15m/1h/4h) — the keyless-exchange loader, which raises
      :class:`IntradayUnavailableError` for a non-crypto symbol so the caller
      declares it unavailable instead of inventing a bar.
    """
    if _is_intraday(timeframe):
        return load_intraday_ohlcv(symbol, curr_date, timeframe)
    daily = load_ohlcv(symbol, curr_date)
    if timeframe == "1w":
        return _resample_weekly(daily, curr_date)
    return daily


def _prep(symbol: str, curr_date: str, timeframe: str = _DEFAULT_TIMEFRAME) -> pd.DataFrame:
    """Load the date-guarded series and attach the simple + exponential averages."""
    df = _load_frame(symbol, curr_date, timeframe).reset_index(drop=True)
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"]).reset_index(drop=True)
    close = df["Close"].astype(float)
    for w in _MA_WINDOWS:
        df[f"MA{w}"] = close.rolling(w).mean()
    for w in _EMA_WINDOWS:
        # adjust=False = the recursive EMA a charting platform draws (Quantfury/TV).
        df[f"EMA{w}"] = close.ewm(span=w, adjust=False).mean()
    return df


def _swings(df: pd.DataFrame, k: int = _SWING_K) -> tuple[list[int], list[int]]:
    lo = df["Low"].astype(float).values
    hi = df["High"].astype(float).values
    n = len(df)
    lows: list[int] = []
    highs: list[int] = []
    for i in range(k, n - k):
        win_lo = lo[i - k:i + k + 1]
        win_hi = hi[i - k:i + k + 1]
        if lo[i] == win_lo.min() and lo[i] < lo[i - 1] and lo[i] <= lo[i + 1]:
            lows.append(i)
        if hi[i] == win_hi.max() and hi[i] > hi[i - 1] and hi[i] >= hi[i + 1]:
            highs.append(i)
    return lows, highs


def _nearest_touched_ma(
    df: pd.DataFrame, i: int, mas: tuple[tuple[str, str], ...]
) -> tuple[str, float, float] | None:
    """Return (label, ma_value, distance) for the nearest RISING average (from the
    method's family ``mas`` — (rótulo, coluna)) the bar ``i`` pulled back into (was
    above it recently, low now within tolerance)."""
    low_i = float(df["Low"].iloc[i])
    best: tuple[str, float, float] | None = None
    for label, col in mas:
        ma = df[col].iloc[i]
        if pd.isna(ma) or i < 10 or pd.isna(df[col].iloc[i - 10]):
            continue
        rising = ma > df[col].iloc[i - 10]
        prior_close = df["Close"].iloc[max(0, i - 20):i]
        prior_ma = df[col].iloc[max(0, i - 20):i]
        was_above = (prior_close.astype(float) >= prior_ma).mean() > 0.5
        dist = (low_i - float(ma)) / float(ma)
        if rising and was_above and abs(dist) <= _TOUCH_TOL:
            if best is None or abs(dist) < abs(best[2]):
                best = (label, float(ma), dist)
    return best


def _buy_regions(
    df: pd.DataFrame, lows: list[int], mas: tuple[tuple[str, str], ...],
    fmt: str = "%Y-%m-%d",
) -> list[BuyRegion]:
    n = len(df)
    year_start = df["Date"].iloc[-1] - pd.Timedelta(days=365)
    out: list[BuyRegion] = []
    for i in lows:
        if df["Date"].iloc[i] < year_start:
            continue
        touch = _nearest_touched_ma(df, i, mas)
        if not touch:
            continue
        label, ma, dist = touch
        close_i = float(df["Close"].iloc[i])
        fwd = df["Close"].iloc[i + 1:min(n, i + 1 + _REACT_BARS)].astype(float)
        react = round((float(fwd.max()) / close_i - 1) * 100, 1) if len(fwd) else None
        out.append(BuyRegion(
            date=df["Date"].iloc[i].strftime(fmt),
            ma_label=label,
            low=round(float(df["Low"].iloc[i]), 2),
            ma_value=round(ma, 2),
            distance_pct=round(dist * 100, 1),
            reaction_pct=react,
        ))
    return out


def _active_region(
    df: pd.DataFrame, mas: tuple[tuple[str, str], ...], fmt: str = "%Y-%m-%d"
) -> BuyRegion | None:
    """Is price sitting on a rising average from the method's family *right now*
    (last bar)? That is the actionable, live buy region the UI highlights."""
    i = len(df) - 1
    touch = _nearest_touched_ma(df, i, mas)
    if not touch:
        return None
    label, ma, dist = touch
    return BuyRegion(
        date=df["Date"].iloc[i].strftime(fmt),
        ma_label=label,
        low=round(float(df["Low"].iloc[i]), 2),
        ma_value=round(ma, 2),
        distance_pct=round(dist * 100, 1),
        reaction_pct=None,
    )


def _alternating(df: pd.DataFrame, lows: list[int], highs: list[int]) -> list[tuple[int, str]]:
    """Collapse swings into a strictly alternating low/high sequence, keeping the
    more extreme point when two of the same kind are adjacent."""
    ev = sorted([(i, "L") for i in lows] + [(i, "H") for i in highs])
    seq: list[tuple[int, str]] = []
    for i, t in ev:
        if seq and seq[-1][1] == t:
            pi = seq[-1][0]
            if t == "L" and float(df["Low"].iloc[i]) < float(df["Low"].iloc[pi]):
                seq[-1] = (i, t)
            elif t == "H" and float(df["High"].iloc[i]) > float(df["High"].iloc[pi]):
                seq[-1] = (i, t)
        else:
            seq.append((i, t))
    return seq


def _pattern_123(
    df: pd.DataFrame, lows: list[int], highs: list[int], fmt: str = "%Y-%m-%d"
) -> Pattern123 | None:
    """Detect the most recent 1-2-3 reversal in EITHER direction.

    * compra (bottom): ``L → H → L`` with point 3's low ABOVE point 1's low
      (ascending bottom); trigger = break above point 2's high.
    * venda (top): ``H → L → H`` with point 3's high BELOW point 1's high
      (descending top); trigger = break below point 2's low.

    Scans left-to-right and keeps overwriting, so the most recent valid triple of
    either direction wins.
    """
    seq = _alternating(df, lows, highs)
    lo = df["Low"].astype(float)
    hi = df["High"].astype(float)
    best: tuple[int, int, int, str] | None = None
    for a in range(len(seq) - 2):
        kinds = (seq[a][1], seq[a + 1][1], seq[a + 2][1])
        p1, p2, p3 = seq[a][0], seq[a + 1][0], seq[a + 2][0]
        if kinds == ("L", "H", "L") and lo.iloc[p3] > lo.iloc[p1]:
            best = (p1, p2, p3, "compra")   # ascending bottom
        elif kinds == ("H", "L", "H") and hi.iloc[p3] < hi.iloc[p1]:
            best = (p1, p2, p3, "venda")     # descending top
    if not best:
        return None
    p1, p2, p3, direction = best

    if direction == "compra":
        trigger = round(float(hi.iloc[p2]), 2)                 # rompe a máxima do ponto 2
        state = "acionado" if (hi.iloc[p3 + 1:] > trigger).any() else "formando"
        pt_kinds = ("L", "H", "L")
    else:
        trigger = round(float(lo.iloc[p2]), 2)                 # perde a mínima do ponto 2
        state = "acionado" if (lo.iloc[p3 + 1:] < trigger).any() else "formando"
        pt_kinds = ("H", "L", "H")

    def pt(idx: int, kind: str) -> dict[str, Any]:
        price = df["Low"].iloc[idx] if kind == "L" else df["High"].iloc[idx]
        return {"date": df["Date"].iloc[idx].strftime(fmt), "price": round(float(price), 2)}

    return Pattern123(
        pt(p1, pt_kinds[0]), pt(p2, pt_kinds[1]), pt(p3, pt_kinds[2]),
        trigger, state, direction,
    )


def detect_price_structure(
    symbol: str, curr_date: str, timeframe: str = _DEFAULT_TIMEFRAME,
    method: str = _DEFAULT_METHOD,
) -> PriceStructure:
    """Detect buy regions and the 1-2-3 pattern on the date-guarded series.

    ``timeframe`` selects the frame: ``"1d"`` (default) from the cached daily
    series, or ``"15m"``/``"1h"`` intraday from the keyless exchange (crypto only).
    ``method`` selects the average family the "recuo à média" keys on (Padrão →
    MMS; Erick → EMA 8/21) and the swing horizon — so the Padrão and Erick columns
    of a confront draw genuinely different structures (fork brief 24/08).
    Propagates :class:`NoMarketDataError` (incl. :class:`IntradayUnavailableError`
    for a non-crypto intraday request) so the caller degrades to an explicit note.
    """
    fmt = _date_fmt(timeframe)
    mas = _method_mas(method)
    df = _prep(symbol, curr_date, timeframe)
    struct = PriceStructure(symbol=symbol, as_of=str(curr_date))
    if len(df) <= 2 * _SWING_K + 1:
        return struct  # too thin to have any confirmed swing
    lows, highs = _swings(df, _method_k(method))
    struct.buy_regions = _buy_regions(df, lows, mas, fmt)
    struct.active_region = _active_region(df, mas, fmt)
    struct.pattern = _pattern_123(df, lows, highs, fmt)
    return struct


# --------------------------------------------------------------- markdown ------
def _fmt_region(r: BuyRegion) -> str:
    if r.reaction_pct is None:
        reaction = "reação ainda em curso (sem candle suficiente à frente)"
    elif r.reaction_pct > 0:
        reaction = f"e reagiu **+{r.reaction_pct:.1f}%** nos ~{_REACT_BARS} pregões seguintes"
    else:
        reaction = f"e ainda cedeu {r.reaction_pct:.1f}% no período seguinte"
    side = "logo acima" if r.distance_pct >= 0 else "logo abaixo"
    return (
        f"- **{r.ma_label}** em **{r.date}**: preço recuou até a média "
        f"(mínima {r.low:,.2f}; média {r.ma_value:,.2f}; {abs(r.distance_pct):.1f}% "
        f"{side}) {reaction}."
    )


def build_price_structure_section(
    symbol: str, curr_date: str, timeframe: str = _DEFAULT_TIMEFRAME,
    method: str = _DEFAULT_METHOD,
) -> str:
    """Render the 'Estrutura de preço / setups' markdown section (pt-BR).

    Always returns a section — 'nenhum setup identificado' when nothing is found,
    an explicit note on a hard data failure. Never silence, never a fake number.
    On an intraday request for an asset with no keyless intraday candle (e.g. an
    equity) it declares "intradiário indisponível para ação" rather than invent.
    ``method`` picks the average family the structure keys on (Padrão MMS / Erick EMA).
    """
    tf = _tf_label(timeframe)
    heading = f"## Estrutura de preço / setups — {tf}"
    try:
        s = detect_price_structure(symbol, curr_date, timeframe, method)
    except IntradayUnavailableError as exc:
        logger.info("intraday unavailable for %s (%s): %s", symbol, timeframe, exc)
        return (
            f"{heading}\n\n"
            f"Intradiário indisponível para ação: não há candle {tf} keyless para "
            f"{symbol}. Nenhum valor inventado — o intradiário só é reproduzido "
            "onde existe candle real de exchange (cripto)."
        )
    except Exception as exc:  # noqa: BLE001 — never break the report over enrichment
        logger.warning("price-structure detection failed for %s: %s", symbol, exc)
        return (
            f"{heading}\n\n"
            f"Estrutura de preço indisponível ({type(exc).__name__}); "
            "nenhum valor inventado."
        )

    lines = [
        heading,
        "",
        f"_Detecção determinística sobre a série real de {tf} (até "
        f"{s.as_of}); cada ponto tem data e preço vindos da série, nada é "
        "inventado._",
        "",
    ]

    if s.active_region is not None:
        a = s.active_region
        lines += [
            f"🎯 **Setup ativo agora** — o preço está na {a.ma_label} "
            f"({abs(a.distance_pct):.1f}% "
            f"{'acima' if a.distance_pct >= 0 else 'abaixo'}, mínima {a.low:,.2f} / "
            f"média {a.ma_value:,.2f}): região de compra em formação.",
            "",
        ]

    lines.append("### Regiões de compra na média (último ano)")
    if s.buy_regions:
        # Most recent first; cap the list so the report stays readable.
        for r in reversed(s.buy_regions[-5:]):
            lines.append(_fmt_region(r))
        if len(s.buy_regions) > 5:
            lines.append(f"- _(+{len(s.buy_regions) - 5} outras regiões no período)_")
    else:
        lines.append("_Nenhuma região de compra na média identificada no último ano._")
    lines.append("")

    if s.pattern is not None:
        p = s.pattern
        if p.direction == "venda":
            lines.append("### Padrão 1-2-3 de venda")
            gatilho = "**acionado** (perdeu a mínima do ponto 2)" if p.state == "acionado" \
                else "**em formação** (ainda não perdeu a mínima do ponto 2)"
            lines += [
                f"- **Ponto 1** (topo): {p.p1['date']} — {p.p1['price']:,.2f}",
                f"- **Ponto 2** (repique / mínima): {p.p2['date']} — {p.p2['price']:,.2f}",
                f"- **Ponto 3** (topo descendente, abaixo do ponto 1): "
                f"{p.p3['date']} — {p.p3['price']:,.2f}",
                f"- **Gatilho**: perda de {p.trigger:,.2f} — {gatilho}.",
            ]
        else:
            lines.append("### Padrão 1-2-3 de compra")
            gatilho = "**acionado** (rompeu a máxima do ponto 2)" if p.state == "acionado" \
                else "**em formação** (ainda não rompeu a máxima do ponto 2)"
            lines += [
                f"- **Ponto 1** (fundo): {p.p1['date']} — {p.p1['price']:,.2f}",
                f"- **Ponto 2** (repique / máxima): {p.p2['date']} — {p.p2['price']:,.2f}",
                f"- **Ponto 3** (fundo ascendente, acima do ponto 1): "
                f"{p.p3['date']} — {p.p3['price']:,.2f}",
                f"- **Gatilho**: rompimento de {p.trigger:,.2f} — {gatilho}.",
            ]
    else:
        lines.append("### Padrão 1-2-3")
        lines.append("_Nenhum padrão 1-2-3 identificado no histórico disponível._")

    if not s.buy_regions and s.pattern is None and s.active_region is None:
        lines += ["", "**Nenhum setup identificado.**"]

    return "\n".join(lines)


# ----------------------------------------------------------------- chart -------
def build_price_chart(
    symbol: str, curr_date: str, bars: int = 260, timeframe: str = _DEFAULT_TIMEFRAME,
    method: str = _DEFAULT_METHOD,
) -> dict[str, Any]:
    """Compact candle + moving-average + setup-marker payload for the web UI.

    Returns the last ``bars`` candles of ``timeframe`` (date-guarded), the simple
    (``ma``) and exponential (``ema``) averages aligned to them (null where the
    window has no value yet), and the detected setup markers that fall inside the
    window. The candles and BOTH average families are always drawn; ``method`` only
    picks which family the setup MARKERS (buy regions, active region, 1-2-3) key on
    — so Padrão and Erick columns of a confront differ (fork brief 24/08). Fail-open:
    returns an empty payload on any error (including intraday unavailable) so a chart
    hiccup never blocks the analysis result.
    """
    fmt = _date_fmt(timeframe)
    try:
        df = _prep(symbol, curr_date, timeframe)
        struct = detect_price_structure(symbol, curr_date, timeframe, method)
    except Exception as exc:  # noqa: BLE001
        logger.warning("price-chart build failed for %s (%s): %s", symbol, timeframe, exc)
        return {"symbol": symbol, "timeframe": timeframe, "candles": [], "ma": {}, "ema": {}, "markers": {}}

    tail = df.tail(bars).reset_index(drop=True)

    def num(v: Any) -> float | None:
        return None if pd.isna(v) else round(float(v), 2)

    candles = [
        {
            "d": row["Date"].strftime(fmt),
            "o": num(row["Open"]), "h": num(row["High"]),
            "l": num(row["Low"]), "c": num(row["Close"]),
        }
        for _, row in tail.iterrows()
    ]
    ma = {str(w): [num(v) for v in tail[f"MA{w}"]] for w in _MA_WINDOWS}
    ema = {str(w): [num(v) for v in tail[f"EMA{w}"]] for w in _EMA_WINDOWS}
    window_dates = {c["d"] for c in candles}

    regions = [r.as_dict() for r in struct.buy_regions if r.date in window_dates]
    pattern = None
    if struct.pattern is not None:
        pts = [struct.pattern.p1, struct.pattern.p2, struct.pattern.p3]
        if all(p["date"] in window_dates for p in pts):
            pattern = struct.pattern.as_dict()

    return {
        "symbol": symbol,
        "as_of": str(curr_date),
        "timeframe": timeframe,
        "candles": candles,
        "ma": ma,
        "ma_windows": list(_MA_WINDOWS),
        "ema": ema,
        "ema_windows": list(_EMA_WINDOWS),
        "markers": {
            "buy_regions": regions,
            "active_region": struct.active_region.as_dict() if struct.active_region else None,
            "pattern_123": pattern,
        },
    }


# ------------------------------------------------------ actionable plan ---------
# The reference read is the DAILY series (what the detector runs on); the engine's
# weekly pass supplies the trend backdrop. Stated verbatim so the verdict always
# declares its timeframe instead of leaving the reader to guess (fork brief 23/08).
_TIMEFRAME_REF = "diário (referência) · semanal (tendência de fundo)"

# Horizon bands are the documented nature of these setups (pullback-to-a-rising-
# average / 1-2-3), which "over days-to-months tend to pay" — NOT a per-asset
# number invented for one chart. Keyed by the detected setup state.
_HORIZON = {
    "ativo": "dias para confirmar · semanas a meses para o alvo",
    "aguardar_pullback": "aguardar recuo à média (dias a semanas) · alvo em semanas a meses",
    "aguardar_rompimento": "aguardar rompimento (dias a semanas) · alvo em semanas a meses",
    "sem_setup": "sem gatilho de preço definido — sem horizonte operável",
    "sem_dado": "sem dado suficiente para definir horizonte",
    "intradiario_indisponivel": "intradiário indisponível para ação — sem candle real de exchange",
}


def _plan_timeframe_ref(timeframe: str) -> str:
    """Timeframe label stamped on the plan header. Intraday and the weekly frame
    name themselves ('4 horas (referência)', 'semanal (referência)'); the daily
    default keeps the documented 'diário (ref) · semanal (fundo)' phrasing."""
    if _is_intraday(timeframe) or timeframe == "1w":
        return f"{_tf_label(timeframe)} (referência)"
    return _TIMEFRAME_REF


@dataclass
class ActionablePlan:
    """Deterministic, operable read attached to the verdict header.

    Every price is a real level from the date-guarded series (last close, a rising
    moving average the detector already found, or a prior swing high). When there
    is no basis for a level it is ``None`` — the UI renders "sem nível definido"
    and NEVER a fabricated or rounded-to-look-precise number (fork brief 23/08).
    """

    # Each zone is {"label", "price", "low", "high", "band_basis"}: ``price`` is the
    # anchor (a real level), ``low``/``high`` a band ±0.5·ATR around it, ``band_basis``
    # names that criterion. Without an ATR basis ``low``/``high`` are None and the UI
    # shows the anchor as a point (fork brief 24/08). ``pattern`` is the detected
    # 1-2-3 (either direction) as a dict, or None.
    symbol: str
    as_of: str | None            # date of the last candle actually read
    price: float | None          # last close = "preço no momento da análise"
    timeframe: str
    horizon: str
    setup_state: str             # ativo | aguardar_pullback | aguardar_rompimento | sem_setup | sem_dado
    buy_zone: dict[str, Any] | None       # rising MA (MMS) — banded
    realize_zone: dict[str, Any] | None   # prior swing high overhead — banded
    pullback_zone: dict[str, Any] | None  # recuo to await / 1-2-3 trigger
    pattern: dict[str, Any] | None = None  # detected 1-2-3 (compra|venda) or None

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "as_of": self.as_of,
            "price": self.price,
            "timeframe": self.timeframe,
            "horizon": self.horizon,
            "setup_state": self.setup_state,
            "buy_zone": self.buy_zone,
            "realize_zone": self.realize_zone,
            "pullback_zone": self.pullback_zone,
            "pattern": self.pattern,
        }


def _nearest_overhead_high(
    df: pd.DataFrame, highs: list[int], price: float, fmt: str = "%Y-%m-%d"
):
    """Nearest prior swing high sitting ABOVE ``price`` — the region to realize
    into (resistance / topo anterior). ``None`` when price is in new-high air, so
    the caller reports "sem nível definido" rather than inventing a target."""
    best: tuple[str, float] | None = None
    for i in highs:
        h = float(df["High"].iloc[i])
        if h > price and (best is None or h < best[1]):
            best = (df["Date"].iloc[i].strftime(fmt), round(h, 2))
    if best is None:
        return None
    return {"label": f"topo anterior {best[0]}", "price": best[1]}


def build_actionable_plan(
    symbol: str, curr_date: str, timeframe: str = _DEFAULT_TIMEFRAME,
    method: str = _DEFAULT_METHOD,
) -> ActionablePlan:
    """Turn the cached series + detected structure into an operable plan.

    ``timeframe`` selects the frame (daily default, or ``"15m"``/``"1h"`` intraday
    for crypto). ``method`` picks the average family the recuo/regiões key on (Padrão
    MMS / Erick EMA 8/21) and the swing horizon — so a confront's two columns operate
    each method's own zones. Reuses :func:`detect_price_structure` (buy regions, live
    region, 1-2-3) and the same swings — nothing is recomputed from scratch and no
    number is fabricated. Propagates nothing: a data failure yields a ``sem_dado``
    plan with ``None`` levels; a non-crypto intraday request yields an explicit
    ``intradiario_indisponivel`` plan — never a fake read.
    """
    tf_ref = _plan_timeframe_ref(timeframe)
    fmt = _date_fmt(timeframe)
    try:
        df = _prep(symbol, curr_date, timeframe)
    except IntradayUnavailableError as exc:
        logger.info("actionable-plan intraday unavailable for %s (%s): %s", symbol, timeframe, exc)
        return ActionablePlan(
            symbol=symbol, as_of=None, price=None, timeframe=tf_ref,
            horizon=_HORIZON["intradiario_indisponivel"],
            setup_state="intradiario_indisponivel",
            buy_zone=None, realize_zone=None, pullback_zone=None,
        )
    except Exception as exc:  # noqa: BLE001 — enrichment must never break the run
        logger.warning("actionable-plan data load failed for %s: %s", symbol, exc)
        return ActionablePlan(
            symbol=symbol, as_of=None, price=None, timeframe=tf_ref,
            horizon=_HORIZON["sem_dado"], setup_state="sem_dado",
            buy_zone=None, realize_zone=None, pullback_zone=None,
        )

    if len(df) <= 2 * _SWING_K + 1:
        return ActionablePlan(
            symbol=symbol,
            as_of=df["Date"].iloc[-1].strftime(fmt) if len(df) else None,
            price=round(float(df["Close"].iloc[-1]), 2) if len(df) else None,
            timeframe=tf_ref, horizon=_HORIZON["sem_dado"],
            setup_state="sem_dado",
            buy_zone=None, realize_zone=None, pullback_zone=None,
        )

    price = round(float(df["Close"].iloc[-1]), 2)
    as_of = df["Date"].iloc[-1].strftime(fmt)

    struct = detect_price_structure(symbol, curr_date, timeframe, method)
    _lows, highs = _swings(df, _method_k(method))

    # Região de compra — a RISING moving average the detector already identified.
    buy_zone = None
    if struct.active_region is not None:
        a = struct.active_region
        buy_zone = {"label": f"{a.ma_label} — preço na média agora", "price": a.ma_value}
    elif struct.buy_regions:
        r = struct.buy_regions[-1]  # most recent
        buy_zone = {"label": f"{r.ma_label} — média onde reagiu em {r.date}", "price": r.ma_value}

    # Região de realização — nearest prior swing high overhead.
    realize_zone = _nearest_overhead_high(df, highs, price, fmt)

    # Pullback a aguardar + the resulting setup state / horizon:
    #  • already sitting on the rising MA  -> live setup, no pullback to await
    #  • a rising MA sits BELOW price       -> await a recuo down to it
    #  • a 1-2-3 is forming                 -> await the trigger (rompe/perde ponto 2)
    #  • nothing actionable                 -> no level, no operable horizon
    pullback_zone = None
    pullback_is_trigger = False
    if struct.active_region is not None:
        setup_state = "ativo"
    elif buy_zone is not None and buy_zone["price"] < price:
        setup_state = "aguardar_pullback"
        pullback_zone = {
            "label": f"recuo até {buy_zone['label'].split(' —')[0]} (média subindo)",
            "price": buy_zone["price"],
        }
    elif struct.pattern is not None and struct.pattern.state == "formando":
        setup_state = "aguardar_rompimento"
        pullback_is_trigger = True  # a trigger is a line, not a zone → stays a point
        if struct.pattern.direction == "venda":
            trig_label = "perda da mínima do ponto 2 (gatilho 1-2-3 de venda)"
        else:
            trig_label = "rompimento da máxima do ponto 2 (gatilho 1-2-3 de compra)"
        pullback_zone = {"label": trig_label, "price": struct.pattern.trigger}
    else:
        setup_state = "sem_setup"

    # Bands: buy/realize/recuo-pullback are genuine areas → ±0.5·ATR. A 1-2-3
    # trigger is a precise level, so it stays a point (band_basis None).
    atr = _atr(df)
    buy_zone = _banded(buy_zone, atr)
    realize_zone = _banded(realize_zone, atr)
    pullback_zone = _banded(pullback_zone, None if pullback_is_trigger else atr)

    return ActionablePlan(
        symbol=symbol, as_of=as_of, price=price, timeframe=tf_ref,
        horizon=_HORIZON[setup_state], setup_state=setup_state,
        buy_zone=buy_zone, realize_zone=realize_zone, pullback_zone=pullback_zone,
        pattern=struct.pattern.as_dict() if struct.pattern is not None else None,
    )


def build_actionable_plan_dict(
    symbol: str, curr_date: str, timeframe: str = _DEFAULT_TIMEFRAME,
    method: str = _DEFAULT_METHOD,
) -> dict[str, Any]:
    """UI-facing wrapper: always returns a JSON-serializable dict, never raises."""
    try:
        return build_actionable_plan(symbol, curr_date, timeframe, method).as_dict()
    except Exception as exc:  # noqa: BLE001
        logger.warning("actionable-plan build failed for %s: %s", symbol, exc)
        return ActionablePlan(
            symbol=symbol, as_of=None, price=None,
            timeframe=_plan_timeframe_ref(timeframe),
            horizon=_HORIZON["sem_dado"], setup_state="sem_dado",
            buy_zone=None, realize_zone=None, pullback_zone=None,
        ).as_dict()

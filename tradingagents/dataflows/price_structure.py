"""Deterministic price-structure / setup detection (fork addition).

The market analyst calculates *indicators* (RSI, MACD, moving averages,
Bollinger) but never looks for **price structure** — the setup the product owner
actually trades:

* **Região de recuo à média** — the price pulls back to a rising moving
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

# As duas médias do filtro ÉDEN DOS TRADERS (setup 1-2-3 Storm, lá embaixo).
# EXPONENCIAIS, como a spec escreve (MME = média móvel exponencial no jargão BR) —
# não a simples, que seria a conveniente porque já existe em ``_MA_WINDOWS``. A 8 é
# o viés de curto prazo; a 80 é a tendência principal. Ficam aqui em cima porque
# ``_prep`` calcula a coluna da lenta junto com as outras EMAs.
_STORM_EMA_RAPIDA = 8
_STORM_EMA_LENTA = 80

# Timeframes this detector runs on. The daily/weekly frames come from the cached
# yfinance series; the intraday frames (15m/1h/4h) come from the keyless intraday
# loader — the exchange for crypto, yfinance for an equity (see :mod:`.intraday`) —
# and degrade honestly when the source has no candle for a symbol/date.
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

# Folga do STOP. O stop não é percentual chutado: ele fica ALÉM do nível que
# invalida a estrutura por meio ATR — a mesma leitura de volatilidade que dá
# largura às zonas — pra que o ruído normal da barra não tire o trade antes de a
# estrutura realmente quebrar. Sem base de ATR o stop degrada para o PRÓPRIO nível
# de invalidação (estrutura pura, folga zero declarada) — nunca uma folga inventada.
_STOP_ATR_SLACK = 0.5
_STOP_BASIS = f"invalidação + folga de {_STOP_ATR_SLACK:g}·ATR{_ATR_PERIOD}"
_STOP_BASIS_NO_ATR = "invalidação exata (sem base de ATR para folga)"

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
    state: str  # "acionado" | "rompeu_retracou" | "formando"
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
    * intraday (15m/1h/4h) — the keyless intraday loader (exchange for crypto,
      yfinance for an equity), which raises :class:`IntradayUnavailableError` when
      the source has no candle for the symbol/date so the caller declares it
      unavailable instead of inventing a bar.
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
    # A MME 80 do Éden (setup Storm) entra AQUI, junto das EMAs de sempre: é uma
    # coluna a mais na série já carregada (custo ~zero) e não muda o que o gráfico
    # desenha por padrão — quem decide desenhá-la é ``_chart_emas(method)``.
    for w in sorted({*_EMA_WINDOWS, _STORM_EMA_LENTA}):
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

    # "acionado" tem que refletir o preço ATUAL, não só a história (bug 014): antes
    # bastava QUALQUER barra pós-ponto-3 romper o gatilho pra marcar 'acionado' — um
    # 1-2-3 que rompeu lá atrás e retraçou (preço 313,44 ≤ gatilho 334,7) ficava
    # "acionado" enganoso. Agora: rompeu E o preço segue do lado rompido = acionado;
    # rompeu mas voltou = "rompeu_retracou"; nunca rompeu = "formando".
    last_close = round(float(df["Close"].astype(float).iloc[-1]), 2)
    if direction == "compra":
        trigger = round(float(hi.iloc[p2]), 2)                 # rompe a máxima do ponto 2
        broke = bool((hi.iloc[p3 + 1:] > trigger).any())
        if not broke:
            state = "formando"
        elif last_close > trigger:
            state = "acionado"                                 # rompeu e o preço segue acima
        else:
            state = "rompeu_retracou"                          # rompeu mas voltou abaixo do gatilho
        pt_kinds = ("L", "H", "L")
    else:
        trigger = round(float(lo.iloc[p2]), 2)                 # perde a mínima do ponto 2
        broke = bool((lo.iloc[p3 + 1:] < trigger).any())
        if not broke:
            state = "formando"
        elif last_close < trigger:
            state = "acionado"                                 # perdeu e o preço segue abaixo
        else:
            state = "rompeu_retracou"                          # perdeu mas voltou acima do gatilho
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
    # The 1-2-3 reversal is a pure price-SWING structure — it is not "on the EMA" or
    # "on the MMS"; the MA family the method picks only governs the buy REGIONS above.
    # Detect the pattern on the CANONICAL swing horizon (the default method's k)
    # regardless of `method`, so the report text (always canonical), the chart
    # annotation and the actionable plan can never disagree on the trigger. Before
    # this, an Erick run (k=3) drew the tighter-swing 1-2-3 on the chart (e.g. AAOI
    # gatilho 91,50) while the report text — built canonical (k=5) — read 160,87; the
    # reader saw two triggers for "the" pattern and a stale "acionado".
    canonical_k = _method_k(_DEFAULT_METHOD)
    if _method_k(method) == canonical_k:
        p_lows, p_highs = lows, highs
    else:
        p_lows, p_highs = _swings(df, canonical_k)
    struct.pattern = _pattern_123(df, p_lows, p_highs, fmt)
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


def _levels_lines(
    symbol: str, curr_date: str, timeframe: str, method: str
) -> list[str]:
    """Bullets de INVALIDAÇÃO / STOP / ALVO / R:R para a seção do relatório.

    Reusa o plano acionável (mesma derivação da tela, ver :func:`_pattern_levels`),
    então relatório e gráfico nunca discordam de um nível. Cada item sem base sai
    como "sem nível definido" — o mesmo contrato das zonas."""
    plan = build_actionable_plan_dict(symbol, curr_date, timeframe, method)
    inval, stop = plan.get("invalidation"), plan.get("stop")
    target, rr = plan.get("target"), plan.get("risk_reward")
    lines = ["", "**Níveis operáveis do padrão** (derivados da estrutura, nada arbitrado):"]

    if inval and inval.get("price") is not None:
        lines.append(f"- **Invalidação**: {inval['price']:,.2f} — {inval['meaning']}")
    else:
        lines.append("- **Invalidação**: sem nível definido.")

    if stop and stop.get("price") is not None:
        lines.append(f"- **Stop (SL)**: {stop['price']:,.2f} ({stop['basis']}).")
    else:
        lines.append("- **Stop (SL)**: sem nível definido.")

    if target and target.get("price") is not None:
        band = ""
        if target.get("low") is not None and target.get("high") is not None:
            band = f" — faixa {target['low']:,.2f}–{target['high']:,.2f}"
        same = " — **é o mesmo nível da região de realização**" if target.get("same_as_realize") else ""
        lines.append(
            f"- **Alvo (TP)**: {target['price']:,.2f} ({target['label']}){band}{same}."
        )
    else:
        lines.append("- **Alvo (TP)**: sem nível definido (nenhum swing anterior à frente da entrada).")

    if rr and rr.get("rr") is not None:
        lines.append(
            f"- **Risco/retorno**: **{rr['rr']:.2f}:1** — entrada {rr['entry']:,.2f} "
            f"({rr['entry_basis']}), risco {rr['risk']:,.2f}, retorno {rr['reward']:,.2f}."
        )
    elif rr:
        lines.append(f"- **Risco/retorno**: não calculável — {rr.get('note') or 'sem base'}.")
    else:
        lines.append("- **Risco/retorno**: sem base (stop ou alvo indefinido).")
    return lines


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
            f"Intradiário {tf} indisponível agora para {symbol}: a fonte keyless não "
            "retornou candles (data fora da janela intradiária da fonte ou fonte fora "
            "do ar). Nenhum valor inventado — o intradiário só aparece quando há candle "
            "real (cripto na exchange, ação no yfinance)."
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
        # NOMEIA o setup ("recuo à média", não "compra": o 1-2-3 de compra é outro
        # setup no mesmo relatório) e não afirma "o preço está na média" quando o
        # número ao lado diz que ele está a alguns por cento dela.
        onde = "acima" if a.distance_pct >= 0 else "abaixo"
        lines += [
            f"🎯 **Setup ativo agora — recuo à média** — a mínima está "
            f"{abs(a.distance_pct):.1f}% {onde} da {a.ma_label} "
            f"(mínima {a.low:,.2f} / média {a.ma_value:,.2f}): "
            "região de recuo à média em formação.",
            "",
        ]

    lines.append("### Regiões de recuo à média (último ano)")
    if s.buy_regions:
        # Most recent first; cap the list so the report stays readable.
        for r in reversed(s.buy_regions[-5:]):
            lines.append(_fmt_region(r))
        if len(s.buy_regions) > 5:
            lines.append(f"- _(+{len(s.buy_regions) - 5} outras regiões no período)_")
    else:
        lines.append("_Nenhuma região de recuo à média identificada no último ano._")
    lines.append("")

    if s.pattern is not None:
        p = s.pattern
        if p.direction == "venda":
            lines.append("### Padrão 1-2-3 de venda")
            if p.state == "acionado":
                gatilho = "**acionado** (perdeu a mínima do ponto 2 e o preço segue abaixo)"
            elif p.state == "rompeu_retracou":
                gatilho = "**perdeu e voltou** (perdeu a mínima do ponto 2 mas o preço voltou acima do gatilho — sinal não confirmado)"
            else:
                gatilho = "**em formação** (ainda não perdeu a mínima do ponto 2)"
            lines += [
                f"- **Ponto 1** (topo): {p.p1['date']} — {p.p1['price']:,.2f}",
                f"- **Ponto 2** (repique / mínima): {p.p2['date']} — {p.p2['price']:,.2f}",
                f"- **Ponto 3** (topo descendente, abaixo do ponto 1): "
                f"{p.p3['date']} — {p.p3['price']:,.2f}",
                f"- **Gatilho**: perda de {p.trigger:,.2f} — {gatilho}.",
            ]
        else:
            lines.append("### Padrão 1-2-3 de compra")
            if p.state == "acionado":
                gatilho = "**acionado** (rompeu a máxima do ponto 2 e o preço segue acima)"
            elif p.state == "rompeu_retracou":
                gatilho = "**rompeu e retraçou** (rompeu a máxima do ponto 2 mas o preço voltou abaixo do gatilho — sinal não confirmado)"
            else:
                gatilho = "**em formação** (ainda não rompeu a máxima do ponto 2)"
            lines += [
                f"- **Ponto 1** (fundo): {p.p1['date']} — {p.p1['price']:,.2f}",
                f"- **Ponto 2** (repique / máxima): {p.p2['date']} — {p.p2['price']:,.2f}",
                f"- **Ponto 3** (fundo ascendente, acima do ponto 1): "
                f"{p.p3['date']} — {p.p3['price']:,.2f}",
                f"- **Gatilho**: rompimento de {p.trigger:,.2f} — {gatilho}.",
            ]
        lines += _levels_lines(symbol, curr_date, timeframe, method)
    else:
        lines.append("### Padrão 1-2-3")
        lines.append("_Nenhum padrão 1-2-3 identificado no histórico disponível._")

    if not s.buy_regions and s.pattern is None and s.active_region is None:
        lines += ["", "**Nenhum setup identificado.**"]

    return "\n".join(lines)


# ----------------------------------------------------------------- chart -------
def _chart_emas(method: str) -> tuple[int, ...]:
    """EMAs que o gráfico DESENHA para este método.

    O padrão é o de sempre (8/21/50). O método Storm acrescenta a MME 80 — ela é
    metade do filtro Éden, e um Éden sem a lenta na tela é um veto que o leitor não
    consegue conferir. Só nele: acrescentá-la a todos os métodos poria uma linha a
    mais em telas que não a usam para nada.
    """
    if (method or "").startswith("storm"):
        return tuple(sorted({*_EMA_WINDOWS, _STORM_EMA_LENTA}))
    return _EMA_WINDOWS


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
    emas = _chart_emas(method)
    ema = {str(w): [num(v) for v in tail[f"EMA{w}"]] for w in emas}
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
        "ema_windows": list(emas),
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
    "intradiario_indisponivel": "intradiário indisponível agora — a fonte não retornou candle real",
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
    # Níveis operáveis do 1-2-3 — derivados da estrutura do padrão (ver
    # :func:`_pattern_levels`), NUNCA de um percentual chutado. Todos ``None``
    # quando não há padrão detectado: a tela diz "sem nível definido".
    invalidation: dict[str, Any] | None = None   # onde o padrão deixa de existir
    stop: dict[str, Any] | None = None           # invalidação + folga de ATR
    target: dict[str, Any] | None = None         # alvo (TP) do setup
    risk_reward: dict[str, Any] | None = None    # R:R a partir de entrada/stop/alvo
    # DE QUAL SETUP veio o ``setup_state``. São dois setups INDEPENDENTES que a
    # tela chamava igual: ``recuo_media`` (recuo até uma média ascendente, a faixa
    # verde do gráfico) e ``123`` (rompimento da máxima do ponto 2). Podem coexistir
    # e até discordar — sem este campo o veredito não dizia de quem estava falando.
    setup_source: str | None = None              # recuo_media | 123 | None

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
            "invalidation": self.invalidation,
            "stop": self.stop,
            "target": self.target,
            "risk_reward": self.risk_reward,
            "setup_source": self.setup_source,
        }


def _nearest_overhead_high(
    df: pd.DataFrame, highs: list[int], price: float, fmt: str = "%Y-%m-%d"
):
    """Nearest prior swing high sitting ABOVE ``price`` — the region to realize
    into (resistance / topo anterior). ``None`` when price is in new-high air, so
    the caller reports "sem nível definido" rather than inventing a target.

    A comparação é feita na precisão PUBLICADA (2 casas), não no valor cru. Com o
    filtro cru (``h > price``) e o retorno arredondado, um topo a menos de um
    centavo da referência passava e voltava ARREDONDADO PARA BAIXO — o "alvo" caía
    atrás da entrada. Caso real (MSFT 1d, 29/08): gatilho 512,76 e alvo 512,76,
    stop 471,35 — risco de 41 pontos, retorno ZERO. Nível que não se distingue da
    referência no preço que a tela mostra não é nível: é ``None`` honesto.
    """
    ref = round(price, 2)
    best: tuple[str, float] | None = None
    for i in highs:
        h = round(float(df["High"].iloc[i]), 2)
        if h > ref and (best is None or h < best[1]):
            best = (df["Date"].iloc[i].strftime(fmt), h)
    if best is None:
        return None
    return {"label": f"topo anterior {best[0]}", "price": best[1]}


def _nearest_support_low(
    df: pd.DataFrame, lows: list[int], price: float, fmt: str = "%Y-%m-%d"
):
    """Nearest prior swing low sitting BELOW ``price`` — the support a SHORT
    realizes into. Mirror of :func:`_nearest_overhead_high`; a venda setup must
    never inherit the long's overhead target (schemas.py already warns about the
    inverted skeleton). ``None`` when price is in new-low air, so the caller
    reports "sem nível definido" instead of inventing a target. Compara na precisão
    PUBLICADA, espelhando :func:`_nearest_overhead_high` — um fundo a menos de um
    centavo da referência arredondava PRA CIMA e virava "alvo" atrás da entrada."""
    ref = round(price, 2)
    best: tuple[str, float] | None = None
    for i in lows:
        lo = round(float(df["Low"].iloc[i]), 2)
        if lo < ref and (best is None or lo > best[1]):
            best = (df["Date"].iloc[i].strftime(fmt), lo)
    if best is None:
        return None
    return {"label": f"fundo anterior {best[0]}", "price": best[1]}


def _entry_ref(pattern: Pattern123, price: float, compra: bool) -> tuple[float, str]:
    """Entrada de referência do setup e o motivo dela, escrito.

    Enquanto o padrão não acionou, a entrada é o GATILHO (é onde se entra). Depois
    de acionado o gatilho já ficou para trás, então a referência honesta é o PREÇO
    ATUAL — é o que ainda resta de trade para quem lê a tela agora."""
    if pattern.state == "acionado":
        return float(price), "preço atual (padrão já acionado)"
    return float(pattern.trigger), (
        "gatilho — rompimento da máxima do ponto 2" if compra
        else "gatilho — perda da mínima do ponto 2"
    )


def _pattern_levels(
    pattern: Pattern123 | None,
    df: pd.DataFrame,
    lows: list[int],
    highs: list[int],
    price: float,
    atr: float | None,
    realize_zone: dict[str, Any] | None,
    fmt: str = "%Y-%m-%d",
) -> tuple[dict | None, dict | None, dict | None, dict | None]:
    """``(invalidação, stop, alvo, risco_retorno)`` derivados do 1-2-3 detectado.

    Tudo sai de ESTRUTURA real da série — nenhum nível é inventado nem arredondado
    "pra ficar bonito":

    * **invalidação** — o ponto 3, que é o que sustenta o padrão. Num 1-2-3 de
      compra o ponto 3 é o fundo ascendente: perdê-lo mata a premissa de fundos
      subindo. Num 1-2-3 de venda o ponto 3 é o topo descendente: voltar acima
      dele mata a premissa de topos caindo. É o preço de uma barra real.
    * **stop** — a invalidação com folga de ``_STOP_ATR_SLACK·ATR`` (abaixo na
      compra, acima na venda). Sem ATR, o stop É a invalidação e o motivo fica
      declarado — jamais um percentual chutado.
    * **alvo** — o swing anterior à frente da ENTRADA (topo acima na compra, fundo
      abaixo na venda). Medir a partir da entrada, e não do preço, é o que impede o
      absurdo de um 1-2-3 ainda não acionado ter como "alvo" o próprio gatilho (o
      topo do ponto 2 é justamente o nível que se rompe para entrar). A venda usa o
      seu próprio lado da estrutura — não herda o esqueleto do long.
    * **risco/retorno** — só quando stop E alvo existem e estão do lado certo da
      entrada; caso contrário ``rr=None`` com o motivo escrito.

    Sem padrão detectado devolve ``(None, None, None, None)`` — a tela mostra
    "sem nível definido" em vez de fabricar um esqueleto de trade.
    """
    if pattern is None:
        return None, None, None, None

    compra = pattern.direction != "venda"
    inval_price = float(pattern.p3["price"])
    if compra:
        invalidation = {
            "label": f"perda do ponto 3 ({pattern.p3['date']})",
            "price": round(inval_price, 2),
            "meaning": (
                f"o setup morre se perder {inval_price:,.2f} — abaixo do ponto 3 "
                "o fundo ascendente deixa de ser ascendente e o 1-2-3 de compra "
                "não existe mais."
            ),
        }
    else:
        invalidation = {
            "label": f"retomada do ponto 3 ({pattern.p3['date']})",
            "price": round(inval_price, 2),
            "meaning": (
                f"o setup morre se voltar acima de {inval_price:,.2f} — acima do "
                "ponto 3 o topo descendente deixa de ser descendente e o 1-2-3 de "
                "venda não existe mais."
            ),
        }

    if atr is not None and atr > 0:
        slack = _STOP_ATR_SLACK * atr
        stop_price = inval_price - slack if compra else inval_price + slack
        stop_basis = _STOP_BASIS
    else:
        stop_price = inval_price
        stop_basis = _STOP_BASIS_NO_ATR
    stop = {
        "label": "stop (SL)",
        "price": round(stop_price, 2),
        "anchor": round(inval_price, 2),
        "atr": atr,
        "basis": stop_basis,
    }

    entry, entry_basis = _entry_ref(pattern, price, compra)
    raw_target = (
        _nearest_overhead_high(df, highs, entry, fmt) if compra
        else _nearest_support_low(df, lows, entry, fmt)
    )
    target = _banded(raw_target, atr)
    if target is not None:
        # Reconciliação com a região de realização: quando o alvo do padrão É o
        # mesmo nível, a tela desenha UM só e diz que são o mesmo (nunca dois).
        rz_price = (realize_zone or {}).get("price")
        target["same_as_realize"] = rz_price is not None and rz_price == target["price"]

    risk_reward = _risk_reward(entry, entry_basis, stop, target, compra)
    return invalidation, stop, target, risk_reward


def _risk_reward(
    entry: float, entry_basis: str, stop: dict | None,
    target: dict | None, compra: bool,
) -> dict | None:
    """R:R do setup a partir de níveis REAIS (entrada, stop, alvo).

    ``None`` quando falta stop ou alvo — sem os dois não há razão a calcular, e
    inventar uma seria pior que não mostrar nada. Quando os níveis existem mas
    estão do lado errado da entrada (alvo já para trás, stop além da entrada),
    devolve ``rr=None`` com o motivo em ``note`` — a tela diz por que não há R:R
    em vez de exibir um número sem sentido.
    """
    if stop is None or target is None or target.get("price") is None:
        return None
    stop_p, tgt_p = float(stop["price"]), float(target["price"])
    # Contas na precisão PUBLICADA: stop e alvo já vêm arredondados, e medir o risco
    # contra uma entrada CRUA fazia a conta discordar da tela por frações de centavo
    # (era assim que um "reward 0,0" convivia com um alvo aparentemente acima).
    entry = round(entry, 2)
    risk = entry - stop_p if compra else stop_p - entry
    reward = tgt_p - entry if compra else entry - tgt_p
    out = {
        "entry": entry,
        "entry_basis": entry_basis,
        "risk": round(abs(risk), 2),
        "reward": round(abs(reward), 2),
        "rr": None,
        "note": None,
    }
    if risk <= 0:
        out["note"] = "stop do lado errado da entrada — sem risco mensurável neste ponto."
        return out
    if reward <= 0:
        out["note"] = "o alvo já ficou para trás da entrada — sem retorno a projetar."
        return out
    out["rr"] = round(reward / risk, 2)
    return out


# Papel da região de realização quando existe um 1-2-3 na tela. Sem padrão ela é
# o alvo de sempre; com padrão pode ser o MESMO nível do alvo, o próprio gatilho
# (aí a linha do 1-2-3 já a desenha) ou, num setup de venda, apenas a resistência
# acima — jamais o "alvo" de um short.
_REALIZE_ROLE = {
    "alvo": "realização (alvo)",
    "gatilho": "realização = gatilho do 1-2-3",
    "resistencia": "topo anterior (resistência)",
}


def _reconcile_realize(
    realize_zone: dict[str, Any] | None,
    pattern: Pattern123 | None,
    target: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Carimba ``role``/``role_label`` na região de realização (ver :data:`_REALIZE_ROLE`).

    Não mexe em nenhum preço — só nomeia o papel do nível, pra que a tela nunca
    mostre dois "alvos" concorrentes nem chame de alvo de um short um topo que
    está acima do preço."""
    if realize_zone is None:
        return None
    role = "alvo"
    if pattern is not None:
        if pattern.direction == "venda":
            role = "resistencia"
        elif target is not None and target.get("same_as_realize"):
            role = "alvo"
        elif realize_zone.get("price") == pattern.trigger:
            role = "gatilho"
    return {**realize_zone, "role": role, "role_label": _REALIZE_ROLE[role]}


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
    plan with ``None`` levels; an intraday request the source has no candle for
    (e.g. an equity backtest beyond yfinance's intraday window) yields an explicit
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
    lows, highs = _swings(df, _method_k(method))

    # Região de compra na MÉDIA — o setup do RECUO, que NÃO é o 1-2-3. ``ma_label``
    # fica no payload porque a tela precisa NOMEAR o setup (não basta "compra": o
    # 1-2-3 de compra é outro setup, com outro gatilho, e os dois convivem na mesma
    # tela). ``origin`` distingue o ramo: "ativa" é a média que o detector diz estar
    # sendo tocada AGORA, "historica" é a última onde o preço reagiu.
    buy_zone = None
    if struct.active_region is not None:
        a = struct.active_region
        buy_zone = {"label": f"{a.ma_label} — preço na média agora", "price": a.ma_value,
                    "ma_label": a.ma_label, "origin": "ativa"}
    elif struct.buy_regions:
        r = struct.buy_regions[-1]  # most recent
        buy_zone = {"label": f"{r.ma_label} — média onde reagiu em {r.date}", "price": r.ma_value,
                    "ma_label": r.ma_label, "origin": "historica"}

    # Região de realização — nearest prior swing high overhead.
    realize_zone = _nearest_overhead_high(df, highs, price, fmt)

    # Pullback a aguardar + the resulting setup state / horizon:
    #  • already sitting on the rising MA  -> live setup, no pullback to await
    #  • a rising MA sits BELOW price       -> await a recuo down to it
    #  • a 1-2-3 is forming                 -> await the trigger (rompe/perde ponto 2)
    #  • nothing actionable                 -> no level, no operable horizon
    pullback_zone = None
    pullback_is_trigger = False
    setup_source = None
    if struct.active_region is not None:
        setup_state = "ativo"
        setup_source = "recuo_media"
    elif buy_zone is not None and buy_zone["price"] < price:
        setup_state = "aguardar_pullback"
        setup_source = "recuo_media"
        pullback_zone = {
            "label": f"recuo até {buy_zone['label'].split(' —')[0]} (média subindo)",
            "price": buy_zone["price"],
        }
    elif struct.pattern is not None and struct.pattern.state in ("formando", "rompeu_retracou"):
        # "formando" (nunca rompeu) e "rompeu_retracou" (rompeu e voltou) têm o preço
        # do lado NÃO rompido do gatilho → em ambos o que se espera é o rompimento.
        setup_state = "aguardar_rompimento"
        setup_source = "123"
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

    # A zona da média NOMEIA o próprio setup e diz se está ATIVA AGORA — e "ativa"
    # é medida contra a faixa que o gráfico DESENHA (±0,5·ATR), não contra a
    # tolerância de toque do detector (``_TOUCH_TOL`` = 8%), que é ordens de
    # grandeza mais larga. Era essa diferença de régua que produzia o ZEC-USD 4h
    # de 29/08: preço 836,38 visivelmente fora da faixa 790,32–815,92 enquanto o
    # rótulo afirmava "preço na média agora". O limiar do detector fica como está
    # (mudá-lo mudaria a DETECÇÃO); o que muda é a tela parar de afirmar o que o
    # número desmente.
    if buy_zone is not None:
        origem = buy_zone.pop("origin", None)
        ma_label = buy_zone.get("ma_label") or "média"
        dist = ((price / buy_zone["price"] - 1) * 100) if buy_zone.get("price") else None
        dentro = (buy_zone.get("low") is not None
                  and buy_zone["low"] <= price <= buy_zone["high"])
        buy_zone["setup"] = "recuo_media"
        buy_zone["tag"] = f"recuo à média ({ma_label})"
        buy_zone["active_now"] = bool(dentro)
        buy_zone["distance_pct"] = round(dist, 1) if dist is not None else None
        if origem == "ativa" and not dentro and dist is not None:
            onde = "acima" if dist > 0 else "abaixo"
            buy_zone["label"] = (f"{ma_label} — preço {abs(dist):.1f}% {onde} da média, "
                                 f"fora da faixa (não é entrada agora)")

    # Reconciliação compra×realização: âncoras distintas com bandas que se cobrem
    # (ex.: EMA 21 e topo anterior a 0,7 de distância com ±0,5·ATR) NÃO são duas
    # zonas independentes — comprar e realizar no mesmo preço é setup degenerado.
    # Declara a sobreposição no molde do same_as_realize do alvo, nunca esconde.
    if (
        buy_zone is not None and realize_zone is not None
        and buy_zone.get("high") is not None and realize_zone.get("low") is not None
        and buy_zone["high"] >= realize_zone["low"]
    ):
        note = ("faixa de recuo à média cobre a de realização — sem espaço entre a "
                "média e o alvo: setup degenerado, não duas zonas independentes")
        buy_zone["overlap_note"] = note
        realize_zone["overlap_note"] = note

    # Onde INVALIDA, onde é o STOP e onde é o ALVO — mais o R:R que transforma
    # "tem 1-2-3" em trade operável. Ancorados no ponto 3 e no swing anterior da
    # série; sem padrão, os quatro ficam None (a tela diz "sem nível definido").
    invalidation, stop, target, risk_reward = _pattern_levels(
        struct.pattern, df, lows, highs, price, atr, realize_zone, fmt
    )
    realize_zone = _reconcile_realize(realize_zone, struct.pattern, target)

    return ActionablePlan(
        symbol=symbol, as_of=as_of, price=price, timeframe=tf_ref,
        horizon=_HORIZON[setup_state], setup_state=setup_state,
        buy_zone=buy_zone, realize_zone=realize_zone, pullback_zone=pullback_zone,
        pattern=struct.pattern.as_dict() if struct.pattern is not None else None,
        invalidation=invalidation, stop=stop, target=target, risk_reward=risk_reward,
        setup_source=setup_source,
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


# ============================================================== SETUP 1-2-3 STORM ==
# O 1-2-3 do Alexandre Wolwacz ("Stormer") com o filtro ÉDEN DOS TRADERS.
#
# NÃO é variação do 1-2-3 que já vive neste módulo (:func:`_pattern_123`). É OUTRO
# padrão, com a MESMA NUMERAÇÃO significando coisas DIFERENTES — por isso ele tem
# detector PRÓPRIO, e não um ramo dentro do existente:
#
#                       1-2-3 deste módulo             1-2-3 STORM
#   pontos              swings confirmados (k=5)       3 CANDLES consecutivos
#   ponto 2 (compra)    o TOPO do repique              o FUNDO (menor mínima dos 3)
#   ponto 3 (compra)    fundo ASCENDENTE acima do p1   recuperação que FALHA em
#                                                        romper a máxima do ponto 1
#   stop                ponto 3 + folga de ATR         abaixo do PONTO 2
#   alvo                swing anterior mais próximo    PROJEÇÃO DA AMPLITUDE dos 3
#   filtro              nenhum                         ÉDEN (MME 8 × MME 80) — VETO
#
# A semântica do ponto 2 está literalmente INVERTIDA entre os dois. Forçar o detector
# de swings a servir aos dois produziria exatamente o gênero de defeito que este
# projeto passou o dia matando: um nome para duas coisas (ver DA-075).

# Rótulos pt-BR do estado do gatilho Storm — os mesmos três estados do 1-2-3 deste
# módulo, porque a pergunta que eles respondem é a mesma ("já rompeu? o preço segue
# do lado rompido?"), só que medida no gatilho do Storm.
_STORM_ESTADO = {
    "formando": "em formação — o gatilho ainda não foi rompido",
    "acionado": "acionado — rompeu e o preço segue do lado rompido",
    "rompeu_retracou": "rompeu e retraçou (não confirmado)",
}


@dataclass
class StormPattern:
    """Os três CANDLES do 1-2-3 Storm, mais o que se deriva deles.

    ``p1``/``p2``/``p3`` carregam o OHLC inteiro do candle (nada de guardar só um
    preço: a amplitude, o gatilho e a invalidação leem extremos diferentes) e o
    ``price`` que aquele ponto REPRESENTA na leitura — a máxima do ponto 1 (o nível
    que o ponto 3 falha em romper), a mínima do ponto 2 (o fundo) e a máxima do
    ponto 3 numa compra; espelhado na venda.
    """
    p1: dict[str, Any]
    p2: dict[str, Any]
    p3: dict[str, Any]
    direction: str      # "compra" (fundo) | "venda" (topo)
    trigger: float
    state: str          # "formando" | "acionado" | "rompeu_retracou"
    amplitude: float    # maior máxima − menor mínima dos 3 candles

    def as_dict(self) -> dict[str, Any]:
        return {
            "p1": self.p1, "p2": self.p2, "p3": self.p3,
            "direction": self.direction, "trigger": self.trigger,
            "state": self.state, "state_label": _STORM_ESTADO.get(self.state, self.state),
            "amplitude": self.amplitude,
        }


def _eden(df: pd.DataFrame) -> dict[str, Any]:
    """Filtro ÉDEN DOS TRADERS: MME 8 × MME 80 × posição do preço.

    Três estados, e o terceiro é VETO — não é penalidade de tamanho:

    * MME 8 **acima** da MME 80 **e** preço acima das duas → Éden de **compra**;
    * MME 8 **abaixo** da MME 80 **e** preço abaixo das duas → Éden de **venda**;
    * qualquer outra combinação → **sem Éden, não opera**.

    A ARMADILHA que a spec nomeia ganha nome próprio no motivo: preço acima da MME 8
    mas ABAIXO da MME 80 é repique dentro de tendência de baixa, não reversão (e o
    espelho vale na venda). Ela já cai no terceiro estado pela regra geral; nomeá-la
    é o que impede a tela de dizer só "desalinhado" no caso mais caro.

    Série curta: a EMA recursiva devolve número desde a primeira barra, então uma
    MME 80 lida com 30 candles é um número que PARECE média de 80 períodos e não é.
    Aqui isso vira ``disponivel: False`` declarado — nunca um Éden inventado.
    """
    col_r, col_l = f"EMA{_STORM_EMA_RAPIDA}", f"EMA{_STORM_EMA_LENTA}"
    n = len(df)
    if n < _STORM_EMA_LENTA or col_r not in df.columns or col_l not in df.columns:
        return {
            "disponivel": False, "alinhado": False, "direcao": None, "armadilha": False,
            "ema_rapida": None, "ema_lenta": None, "preco": None,
            "motivo": (f"série com {n} candles — a MME {_STORM_EMA_LENTA} precisa de pelo "
                       f"menos {_STORM_EMA_LENTA} para significar alguma coisa"),
        }
    rapida = round(float(df[col_r].iloc[-1]), 2)
    lenta = round(float(df[col_l].iloc[-1]), 2)
    preco = round(float(df["Close"].astype(float).iloc[-1]), 2)
    base = {"disponivel": True, "ema_rapida": rapida, "ema_lenta": lenta, "preco": preco}
    if rapida > lenta and preco > rapida and preco > lenta:
        return {**base, "alinhado": True, "direcao": "compra", "armadilha": False,
                "motivo": (f"MME {_STORM_EMA_RAPIDA} acima da MME {_STORM_EMA_LENTA} e "
                           "preço acima das duas")}
    if rapida < lenta and preco < rapida and preco < lenta:
        return {**base, "alinhado": True, "direcao": "venda", "armadilha": False,
                "motivo": (f"MME {_STORM_EMA_RAPIDA} abaixo da MME {_STORM_EMA_LENTA} e "
                           "preço abaixo das duas")}
    armadilha_compra = preco > rapida and preco < lenta
    armadilha_venda = preco < rapida and preco > lenta
    if armadilha_compra:
        motivo = (f"ARMADILHA: preço acima da MME {_STORM_EMA_RAPIDA} mas ABAIXO da MME "
                  f"{_STORM_EMA_LENTA} — repique dentro de tendência de baixa, não reversão")
    elif armadilha_venda:
        motivo = (f"ARMADILHA: preço abaixo da MME {_STORM_EMA_RAPIDA} mas ACIMA da MME "
                  f"{_STORM_EMA_LENTA} — recuo dentro de tendência de alta, não reversão")
    else:
        motivo = (f"MME {_STORM_EMA_RAPIDA} e MME {_STORM_EMA_LENTA} cruzadas ou o preço "
                  "entre elas — sem Éden")
    return {**base, "alinhado": False, "direcao": None,
            "armadilha": bool(armadilha_compra or armadilha_venda), "motivo": motivo}


def _storm_ponto(df: pd.DataFrame, idx: int, kind: str, fmt: str) -> dict[str, Any]:
    """Um ponto do Storm = o CANDLE inteiro + o preço que ele representa na leitura."""
    row = df.iloc[idx]
    preco = float(row["Low"]) if kind == "L" else float(row["High"])
    return {
        "date": row["Date"].strftime(fmt),
        "price": round(preco, 2),
        "open": round(float(row["Open"]), 2),
        "high": round(float(row["High"]), 2),
        "low": round(float(row["Low"]), 2),
        "close": round(float(row["Close"]), 2),
    }


def _storm_123(df: pd.DataFrame, fmt: str = "%Y-%m-%d") -> StormPattern | None:
    """O 1-2-3 Storm mais RECENTE em qualquer direção, lido em 3 candles seguidos.

    Compra (fundo) — as três condições da spec, nesta ordem:
      1. ponto 1 é candle de alta ou lateral (``close >= open``);
      2. ponto 2 é O FUNDO: mínima menor que a do 1 E que a do 3 (é o que faz dele
         "a menor mínima dos 3"; empate não serve, senão o fundo é ambíguo);
      3. ponto 3 é RECUPERAÇÃO (fecha acima do fechamento do ponto 2) que FALHA em
         romper o ponto 1 (``high3 < high1``) — a falha é o coração do padrão.

    Venda é o espelho exato. Varre da esquerda pra direita sobrescrevendo, então o
    triplo válido MAIS RECENTE vence (mesma regra do 1-2-3 deste módulo).

    Gatilho = a MAIOR das máximas do ponto 2 e do ponto 3 na compra (a menor das
    mínimas na venda). A spec escreve "máxima do ponto 2 (ou 3)"; usar só a do 2
    quando a do 3 está acima entregaria um gatilho já rompido no nascimento.
    """
    if len(df) < 3:
        return None
    o = df["Open"].astype(float).values
    h = df["High"].astype(float).values
    lo = df["Low"].astype(float).values
    c = df["Close"].astype(float).values
    best: tuple[int, int, int, str] | None = None
    for i in range(len(df) - 2):
        a, b, d = i, i + 1, i + 2
        if (c[a] >= o[a]                                  # 1. alta ou lateral
                and lo[b] < lo[a] and lo[b] < lo[d]       # 2. o fundo
                and c[d] > c[b] and h[d] < h[a]):         # 3. recupera e falha
            best = (a, b, d, "compra")
        elif (c[a] <= o[a]                                # 1. baixa ou lateral
                and h[b] > h[a] and h[b] > h[d]           # 2. o topo
                and c[d] < c[b] and lo[d] > lo[a]):       # 3. cai e falha
            best = (a, b, d, "venda")
    if best is None:
        return None
    a, b, d, direction = best
    compra = direction != "venda"
    amplitude = round(float(max(h[a], h[b], h[d]) - min(lo[a], lo[b], lo[d])), 2)
    last_close = round(float(c[-1]), 2)
    if compra:
        trigger = round(float(max(h[b], h[d])), 2)
        broke = bool((h[d + 1:] > trigger).any())
        kinds = ("H", "L", "H")   # p1 vale pela MÁXIMA (o teto que o 3 não rompe)
        state = "formando" if not broke else (
            "acionado" if last_close > trigger else "rompeu_retracou")
    else:
        trigger = round(float(min(lo[b], lo[d])), 2)
        broke = bool((lo[d + 1:] < trigger).any())
        kinds = ("L", "H", "L")
        state = "formando" if not broke else (
            "acionado" if last_close < trigger else "rompeu_retracou")
    return StormPattern(
        p1=_storm_ponto(df, a, kinds[0], fmt),
        p2=_storm_ponto(df, b, kinds[1], fmt),
        p3=_storm_ponto(df, d, kinds[2], fmt),
        direction=direction, trigger=trigger, state=state, amplitude=amplitude,
    )


def _storm_levels(
    pat: StormPattern, atr: float | None, price: float,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """``(invalidação, stop, alvo, risco_retorno)`` do Storm — nenhum herdado do outro 1-2-3.

    * **invalidação** — o PONTO 2 (a mínima na compra, a máxima na venda). É o fundo
      que o padrão declara: perdê-lo é dizer que a reversão não aconteceu.
    * **stop** — o PONTO 2, exato. Sem a folga de meio ATR que o 1-2-3 de swings usa:
      medida na watchlist real, ela derruba a mediana de R:R de 1,13 para 0,80 porque
      meio ATR14 é enorme perto da amplitude de TRÊS candles. O quanto abaixo do ponto
      2 se põe a ordem é decisão de quem opera — ver o comentário na função.
    * **alvo** — PROJEÇÃO DA AMPLITUDE: (maior máxima − menor mínima dos 3 candles)
      lançada a partir do GATILHO. Ancorar no gatilho, e não no preço de agora, é o
      que mantém o alvo um nível ESTRUTURAL: projetado do preço corrente ele fugiria
      junto com o preço e nunca seria atingido.
    * **risco/retorno** — pela mesma função do resto do módulo (:func:`_risk_reward`),
      com a entrada de referência degradando pro preço atual depois de acionado.
    """
    compra = pat.direction != "venda"
    inval_price = float(pat.p2["low"] if compra else pat.p2["high"])
    lado = "perder" if compra else "voltar acima de"
    invalidation = {
        "label": f"perda do ponto 2 ({pat.p2['date']})" if compra
                 else f"retomada do ponto 2 ({pat.p2['date']})",
        "price": round(inval_price, 2),
        "meaning": (f"o setup morre se {lado} o ponto 2 — é o fundo que a reversão "
                    "declarou" if compra else
                    f"o setup morre se {lado} o ponto 2 — é o topo que a reversão declarou"),
    }
    # STOP NO PONTO 2 — e no ponto 2 EXATO, sem folga inventada.
    #
    # A primeira versão desta função usava a folga de meio ATR que o resto do módulo
    # aplica ao 1-2-3 de swings. MEDIDO na watchlist real (20 ativos × 1d/4h/1h,
    # 29/08): com a folga, a mediana de R:R do Storm é 0,80 e só 21% dos pares dão
    # R:R ≥ 1; sem ela, mediana 1,13 e 77% ≥ 1. A razão é estrutural, não estatística:
    # o Storm mede TRÊS CANDLES, e meio ATR14 é enorme perto da amplitude de três
    # candles — no 1-2-3 de swings, que abrange dezenas de barras, a mesma folga é
    # ruído. Aplicar aqui a folga de lá era carregar um número de um setup pro outro.
    #
    # A spec do Stormer diz "stop abaixo do ponto 2" sem quantificar o "abaixo". O
    # nível ESTRUTURAL é o ponto 2; o quanto abaixo dele cada um põe a ordem é
    # decisão de quem opera, e inventar um valor aqui seria publicar como estrutura
    # uma preferência. Fica o ponto 2 exato, com o motivo escrito na tela.
    stop_price = inval_price
    stop_basis = ("no ponto 2 — a spec põe o stop abaixo dele, e o quanto abaixo é "
                  "decisão de quem opera (não se inventa folga aqui)")
    stop = {"label": "stop (SL)", "price": round(stop_price, 2),
            "anchor": round(inval_price, 2), "atr": atr, "basis": stop_basis,
            "slack": 0.0}

    alvo_price = pat.trigger + pat.amplitude if compra else pat.trigger - pat.amplitude
    target = {
        "label": f"projeção da amplitude dos 3 candles ({pat.amplitude:,.2f}) a partir do gatilho",
        "price": round(float(alvo_price), 2),
        "amplitude": pat.amplitude,
        "low": None, "high": None, "band_basis": None, "same_as_realize": False,
    }
    if pat.state == "acionado":
        entry, entry_basis = float(price), "preço atual (padrão já acionado)"
    else:
        entry, entry_basis = float(pat.trigger), (
            "gatilho — rompimento da máxima do ponto 2/3" if compra
            else "gatilho — perda da mínima do ponto 2/3")
    risk_reward = _risk_reward(entry, entry_basis, stop, target, compra)
    return invalidation, stop, target, risk_reward


def _storm_qualidade(
    pat: StormPattern | None, eden: dict[str, Any], ema_lenta_no_p3: float | None,
) -> dict[str, Any]:
    """Classificação perfeita/boa/ruim + o VETO, escrito.

    A spec só opera **perfeita** e **boa**. As regras, na ordem em que vetam:

    1. **Sem Éden alinhado → ruim, não opera.** É veto, não desconto: a tela diz
       "não opera" e o motivo (inclusive quando o motivo é a armadilha nomeada).
    2. **Éden alinhado na direção CONTRÁRIA à do padrão → ruim, não opera.** Um 1-2-3
       de compra sob Éden de venda é justamente o trade contra a tendência principal
       que a regra proíbe.
    3. **Alinhado e na mesma direção → perfeita quando o PONTO 3 está inteiro do lado
       certo da MME 80** (o candido do ponto 3 acima dela na compra, abaixo na venda,
       medida NA BARRA DO PRÓPRIO PONTO 3 — comparar um candle de semanas atrás com a
       média de hoje seria comparar coisas de tempos diferentes). Senão, **boa**:
       estrutura válida, sem o reforço da tendência principal.
    """
    if pat is None:
        return {"qualidade": None, "motivo": "nenhum 1-2-3 Storm na janela lida",
                "opera": False, "veto": None}
    if not eden.get("alinhado"):
        return {"qualidade": "ruim", "motivo": eden.get("motivo") or "sem Éden",
                "opera": False,
                "veto": f"sem Éden alinhado — {eden.get('motivo') or 'não opera'}"}
    if eden.get("direcao") != pat.direction:
        return {
            "qualidade": "ruim",
            "motivo": (f"o Éden está de {eden.get('direcao')} e o padrão é de "
                       f"{pat.direction}"),
            "opera": False,
            "veto": (f"padrão de {pat.direction} contra Éden de {eden.get('direcao')} — "
                     "operar contra o Éden é o caso que a regra proíbe"),
        }
    compra = pat.direction != "venda"
    lado_certo = (
        ema_lenta_no_p3 is not None
        and (pat.p3["low"] > ema_lenta_no_p3 if compra else pat.p3["high"] < ema_lenta_no_p3)
    )
    if lado_certo:
        onde = "acima" if compra else "abaixo"
        return {"qualidade": "perfeita", "opera": True, "veto": None,
                "motivo": (f"ponto 3 inteiro {onde} da MME {_STORM_EMA_LENTA} — a tendência "
                           "principal sustenta a reversão")}
    onde = "acima" if compra else "abaixo"
    return {"qualidade": "boa", "opera": True, "veto": None,
            "motivo": (f"estrutura válida e Éden alinhado, mas o ponto 3 não está inteiro "
                       f"{onde} da MME {_STORM_EMA_LENTA}")}


def build_storm_plan(
    symbol: str, curr_date: str, timeframe: str = _DEFAULT_TIMEFRAME,
) -> dict[str, Any]:
    """Plano do 1-2-3 Storm na série date-guarded — leitura estrutural, $0 de LLM.

    Devolve sempre um dicionário serializável, com o Éden declarado mesmo quando não
    há padrão: "por que não opera" é informação, e some-la seria a tela ficar muda
    justamente no caso em que o filtro fez o seu trabalho.
    """
    fmt = _date_fmt(timeframe)
    df = _prep(symbol, curr_date, timeframe)
    price = round(float(df["Close"].astype(float).iloc[-1]), 2) if len(df) else None
    as_of = df["Date"].iloc[-1].strftime(fmt) if len(df) else None
    eden = _eden(df)
    pat = _storm_123(df, fmt)
    col_l = f"EMA{_STORM_EMA_LENTA}"
    ema_lenta_no_p3 = None
    if pat is not None and eden.get("disponivel") and col_l in df.columns:
        # A MME 80 NA BARRA DO PONTO 3 (não a de hoje): a regra de qualidade compara
        # aquele candle com a média que existia quando ele se formou.
        alvo_data = pat.p3["date"]
        casadas = df.index[df["Date"].dt.strftime(fmt) == alvo_data]
        if len(casadas):
            v = df[col_l].iloc[int(casadas[-1])]
            ema_lenta_no_p3 = None if pd.isna(v) else round(float(v), 2)
    qual = _storm_qualidade(pat, eden, ema_lenta_no_p3)
    out: dict[str, Any] = {
        "symbol": symbol, "as_of": as_of, "price": price,
        "timeframe": _plan_timeframe_ref(timeframe),
        "eden": eden,
        "pattern": pat.as_dict() if pat is not None else None,
        "ema_lenta_no_p3": ema_lenta_no_p3,
        "invalidation": None, "stop": None, "target": None, "risk_reward": None,
        **qual,
    }
    if pat is not None and price is not None:
        inval, stop, target, rr = _storm_levels(pat, _atr(df), price)
        out.update({"invalidation": inval, "stop": stop, "target": target,
                    "risk_reward": rr})
    return out


def build_storm_plan_dict(
    symbol: str, curr_date: str, timeframe: str = _DEFAULT_TIMEFRAME,
) -> dict[str, Any]:
    """Wrapper de UI: nunca levanta — falha vira plano vazio com o motivo escrito."""
    try:
        return build_storm_plan(symbol, curr_date, timeframe)
    except Exception as exc:  # noqa: BLE001
        logger.warning("storm-plan build failed for %s: %s", symbol, exc)
        return {
            "symbol": symbol, "as_of": None, "price": None,
            "timeframe": _plan_timeframe_ref(timeframe),
            "eden": {"disponivel": False, "alinhado": False, "direcao": None,
                     "armadilha": False, "ema_rapida": None, "ema_lenta": None,
                     "preco": None, "motivo": "série indisponível para esta data/frame"},
            "pattern": None, "ema_lenta_no_p3": None,
            "invalidation": None, "stop": None, "target": None, "risk_reward": None,
            "qualidade": None, "motivo": "sem dado para ler o Storm",
            "opera": False, "veto": None,
        }

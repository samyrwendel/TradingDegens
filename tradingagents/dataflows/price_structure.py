"""Deterministic price-structure / setup detection (fork addition).

The market analyst calculates *indicators* (RSI, MACD, moving averages,
Bollinger) but never looks for **price structure** — the setup the product owner
actually trades:

* **Região de compra na média** — the price pulls back to a rising moving
  average, touches it, and reacts up from there. Buy at the touch and hold; over
  days-to-months it tends to pay.
* **Padrão 1-2-3 de compra** — the classic reversal: point 1 a swing low, point 2
  the next swing high (repique), point 3 a higher swing low (ascending bottom);
  the trigger is a break above point 2's high.

Both are detected here from the SAME cached daily series the rest of the engine
uses (:func:`load_ohlcv`, already cut to ``<= curr_date``), so nothing can see a
future candle — a detection run on a past date only sees bars up to that date.
Every reported point carries a real date and price from the series; no number is
fabricated.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .stockstats_utils import load_ohlcv

logger = logging.getLogger(__name__)

# Moving averages we scan for a pullback-to-support touch. 50 and 200 are the
# usual reference; 20 catches the shallower pullbacks of a steep trend.
_MA_WINDOWS = (20, 50, 200)

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

    def as_dict(self) -> dict[str, Any]:
        return {
            "p1": self.p1, "p2": self.p2, "p3": self.p3,
            "trigger": self.trigger, "state": self.state,
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


def _prep(symbol: str, curr_date: str) -> pd.DataFrame:
    """Load the date-guarded daily series and attach the moving averages."""
    df = load_ohlcv(symbol, curr_date).reset_index(drop=True)
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"]).reset_index(drop=True)
    close = df["Close"].astype(float)
    for w in _MA_WINDOWS:
        df[f"MA{w}"] = close.rolling(w).mean()
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


def _nearest_touched_ma(df: pd.DataFrame, i: int) -> tuple[int, float, float] | None:
    """Return (window, ma_value, distance) for the nearest RISING MA the bar ``i``
    pulled back into (was above it recently, low now within tolerance)."""
    low_i = float(df["Low"].iloc[i])
    best: tuple[int, float, float] | None = None
    for w in _MA_WINDOWS:
        col = f"MA{w}"
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
                best = (w, float(ma), dist)
    return best


def _buy_regions(df: pd.DataFrame, lows: list[int]) -> list[BuyRegion]:
    n = len(df)
    year_start = df["Date"].iloc[-1] - pd.Timedelta(days=365)
    out: list[BuyRegion] = []
    for i in lows:
        if df["Date"].iloc[i] < year_start:
            continue
        touch = _nearest_touched_ma(df, i)
        if not touch:
            continue
        w, ma, dist = touch
        close_i = float(df["Close"].iloc[i])
        fwd = df["Close"].iloc[i + 1:min(n, i + 1 + _REACT_BARS)].astype(float)
        react = round((float(fwd.max()) / close_i - 1) * 100, 1) if len(fwd) else None
        out.append(BuyRegion(
            date=df["Date"].iloc[i].strftime("%Y-%m-%d"),
            ma_label=f"MMS{w}",
            low=round(float(df["Low"].iloc[i]), 2),
            ma_value=round(ma, 2),
            distance_pct=round(dist * 100, 1),
            reaction_pct=react,
        ))
    return out


def _active_region(df: pd.DataFrame) -> BuyRegion | None:
    """Is price sitting on a rising MA *right now* (last bar)? That is the
    actionable, live buy region the UI highlights."""
    i = len(df) - 1
    touch = _nearest_touched_ma(df, i)
    if not touch:
        return None
    w, ma, dist = touch
    return BuyRegion(
        date=df["Date"].iloc[i].strftime("%Y-%m-%d"),
        ma_label=f"MMS{w}",
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


def _pattern_123(df: pd.DataFrame, lows: list[int], highs: list[int]) -> Pattern123 | None:
    seq = _alternating(df, lows, highs)
    best: tuple[int, int, int] | None = None
    for a in range(len(seq) - 2):
        if seq[a][1] == "L" and seq[a + 1][1] == "H" and seq[a + 2][1] == "L":
            p1, p2, p3 = seq[a][0], seq[a + 1][0], seq[a + 2][0]
            # ascending bottom: point 3 must sit ABOVE point 1
            if float(df["Low"].iloc[p3]) > float(df["Low"].iloc[p1]):
                best = (p1, p2, p3)  # keep scanning → most recent valid triple wins
    if not best:
        return None
    p1, p2, p3 = best
    trigger = round(float(df["High"].iloc[p2]), 2)
    after = df["High"].iloc[p3 + 1:].astype(float)
    state = "acionado" if (after > trigger).any() else "formando"

    def pt(idx: int, kind: str) -> dict[str, Any]:
        price = df["Low"].iloc[idx] if kind == "L" else df["High"].iloc[idx]
        return {"date": df["Date"].iloc[idx].strftime("%Y-%m-%d"), "price": round(float(price), 2)}

    return Pattern123(pt(p1, "L"), pt(p2, "H"), pt(p3, "L"), trigger, state)


def detect_price_structure(symbol: str, curr_date: str) -> PriceStructure:
    """Detect buy regions and the 1-2-3 pattern on the date-guarded daily series.

    Propagates :class:`NoMarketDataError` from :func:`load_ohlcv` for an unknown
    symbol so the caller can degrade to an explicit note.
    """
    df = _prep(symbol, curr_date)
    struct = PriceStructure(symbol=symbol, as_of=str(curr_date))
    if len(df) <= 2 * _SWING_K + 1:
        return struct  # too thin to have any confirmed swing
    lows, highs = _swings(df)
    struct.buy_regions = _buy_regions(df, lows)
    struct.active_region = _active_region(df)
    struct.pattern = _pattern_123(df, lows, highs)
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


def build_price_structure_section(symbol: str, curr_date: str) -> str:
    """Render the 'Estrutura de preço / setups' markdown section (pt-BR).

    Always returns a section — 'nenhum setup identificado' when nothing is found,
    an explicit note on a hard data failure. Never silence, never a fake number.
    """
    try:
        s = detect_price_structure(symbol, curr_date)
    except Exception as exc:  # noqa: BLE001 — never break the report over enrichment
        logger.warning("price-structure detection failed for %s: %s", symbol, exc)
        return (
            "## Estrutura de preço / setups\n\n"
            f"Estrutura de preço indisponível ({type(exc).__name__}); "
            "nenhum valor inventado."
        )

    lines = [
        "## Estrutura de preço / setups",
        "",
        "_Detecção determinística sobre a série diária real (até "
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

    lines.append("### Padrão 1-2-3 de compra")
    if s.pattern is not None:
        p = s.pattern
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
        lines.append("_Nenhum padrão 1-2-3 identificado no histórico disponível._")

    if not s.buy_regions and s.pattern is None and s.active_region is None:
        lines += ["", "**Nenhum setup identificado.**"]

    return "\n".join(lines)


# ----------------------------------------------------------------- chart -------
def build_price_chart(symbol: str, curr_date: str, bars: int = 260) -> dict[str, Any]:
    """Compact candle + moving-average + setup-marker payload for the web UI.

    Returns the last ``bars`` daily candles (date-guarded), the moving averages
    aligned to them (null where the window has no value yet), and the detected
    setup markers that fall inside the window. Fail-open: returns an empty payload
    on any error so a chart hiccup never blocks the analysis result.
    """
    try:
        df = _prep(symbol, curr_date)
        struct = detect_price_structure(symbol, curr_date)
    except Exception as exc:  # noqa: BLE001
        logger.warning("price-chart build failed for %s: %s", symbol, exc)
        return {"symbol": symbol, "candles": [], "ma": {}, "markers": {}}

    tail = df.tail(bars).reset_index(drop=True)

    def num(v: Any) -> float | None:
        return None if pd.isna(v) else round(float(v), 2)

    candles = [
        {
            "d": row["Date"].strftime("%Y-%m-%d"),
            "o": num(row["Open"]), "h": num(row["High"]),
            "l": num(row["Low"]), "c": num(row["Close"]),
        }
        for _, row in tail.iterrows()
    ]
    ma = {str(w): [num(v) for v in tail[f"MA{w}"]] for w in _MA_WINDOWS}
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
        "candles": candles,
        "ma": ma,
        "ma_windows": list(_MA_WINDOWS),
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
}


@dataclass
class ActionablePlan:
    """Deterministic, operable read attached to the verdict header.

    Every price is a real level from the date-guarded series (last close, a rising
    moving average the detector already found, or a prior swing high). When there
    is no basis for a level it is ``None`` — the UI renders "sem nível definido"
    and NEVER a fabricated or rounded-to-look-precise number (fork brief 23/08).
    """

    symbol: str
    as_of: str | None            # date of the last candle actually read
    price: float | None          # last close = "preço no momento da análise"
    timeframe: str
    horizon: str
    setup_state: str             # ativo | aguardar_pullback | aguardar_rompimento | sem_setup | sem_dado
    buy_zone: dict[str, Any] | None       # {"label", "price"} — a rising MA (MMS)
    realize_zone: dict[str, Any] | None   # {"label", "price"} — prior swing high overhead
    pullback_zone: dict[str, Any] | None  # {"label", "price"} — recuo to await, if not live

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
        }


def _nearest_overhead_high(df: pd.DataFrame, highs: list[int], price: float):
    """Nearest prior swing high sitting ABOVE ``price`` — the region to realize
    into (resistance / topo anterior). ``None`` when price is in new-high air, so
    the caller reports "sem nível definido" rather than inventing a target."""
    best: tuple[str, float] | None = None
    for i in highs:
        h = float(df["High"].iloc[i])
        if h > price and (best is None or h < best[1]):
            best = (df["Date"].iloc[i].strftime("%Y-%m-%d"), round(h, 2))
    if best is None:
        return None
    return {"label": f"topo anterior {best[0]}", "price": best[1]}


def build_actionable_plan(symbol: str, curr_date: str) -> ActionablePlan:
    """Turn the cached daily series + detected structure into an operable plan.

    Reuses :func:`detect_price_structure` (buy regions, live region, 1-2-3) and
    the same swings — nothing is recomputed from scratch and no number is
    fabricated. Propagates nothing: a data failure yields a ``sem_dado`` plan with
    ``None`` levels, never a fake read.
    """
    try:
        df = _prep(symbol, curr_date)
    except Exception as exc:  # noqa: BLE001 — enrichment must never break the run
        logger.warning("actionable-plan data load failed for %s: %s", symbol, exc)
        return ActionablePlan(
            symbol=symbol, as_of=None, price=None, timeframe=_TIMEFRAME_REF,
            horizon=_HORIZON["sem_dado"], setup_state="sem_dado",
            buy_zone=None, realize_zone=None, pullback_zone=None,
        )

    if len(df) <= 2 * _SWING_K + 1:
        return ActionablePlan(
            symbol=symbol,
            as_of=df["Date"].iloc[-1].strftime("%Y-%m-%d") if len(df) else None,
            price=round(float(df["Close"].iloc[-1]), 2) if len(df) else None,
            timeframe=_TIMEFRAME_REF, horizon=_HORIZON["sem_dado"],
            setup_state="sem_dado",
            buy_zone=None, realize_zone=None, pullback_zone=None,
        )

    price = round(float(df["Close"].iloc[-1]), 2)
    as_of = df["Date"].iloc[-1].strftime("%Y-%m-%d")

    struct = detect_price_structure(symbol, curr_date)
    _lows, highs = _swings(df)

    # Região de compra — a RISING moving average the detector already identified.
    buy_zone = None
    if struct.active_region is not None:
        a = struct.active_region
        buy_zone = {"label": f"{a.ma_label} — preço na média agora", "price": a.ma_value}
    elif struct.buy_regions:
        r = struct.buy_regions[-1]  # most recent
        buy_zone = {"label": f"{r.ma_label} — média onde reagiu em {r.date}", "price": r.ma_value}

    # Região de realização — nearest prior swing high overhead.
    realize_zone = _nearest_overhead_high(df, highs, price)

    # Pullback a aguardar + the resulting setup state / horizon:
    #  • already sitting on the rising MA  -> live setup, no pullback to await
    #  • a rising MA sits BELOW price       -> await a recuo down to it
    #  • a 1-2-3 is forming                 -> await the breakout trigger
    #  • nothing actionable                 -> no level, no operable horizon
    pullback_zone = None
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
        pullback_zone = {
            "label": "rompimento da máxima do ponto 2 (gatilho 1-2-3)",
            "price": struct.pattern.trigger,
        }
    else:
        setup_state = "sem_setup"

    return ActionablePlan(
        symbol=symbol, as_of=as_of, price=price, timeframe=_TIMEFRAME_REF,
        horizon=_HORIZON[setup_state], setup_state=setup_state,
        buy_zone=buy_zone, realize_zone=realize_zone, pullback_zone=pullback_zone,
    )


def build_actionable_plan_dict(symbol: str, curr_date: str) -> dict[str, Any]:
    """UI-facing wrapper: always returns a JSON-serializable dict, never raises."""
    try:
        return build_actionable_plan(symbol, curr_date).as_dict()
    except Exception as exc:  # noqa: BLE001
        logger.warning("actionable-plan build failed for %s: %s", symbol, exc)
        return ActionablePlan(
            symbol=symbol, as_of=None, price=None, timeframe=_TIMEFRAME_REF,
            horizon=_HORIZON["sem_dado"], setup_state="sem_dado",
            buy_zone=None, realize_zone=None, pullback_zone=None,
        ).as_dict()

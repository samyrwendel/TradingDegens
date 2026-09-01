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
import threading
import time
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
    # MORTE do padrão, MEDIDA e datada. O nível de invalidação já era calculado e
    # desenhado, mas ninguém comparava o preço contra ele: um 1-2-3 que morreu
    # continuava na tela com a mesma cor e o mesmo peso de um vivo. `state` não
    # servia pra isso — ele descreve a relação com o GATILHO, e um padrão pode ter
    # acionado e depois perdido o ponto 3, que são fatos independentes.
    invalidado: bool = False
    invalidado_em: str | None = None   # a data da barra que fechou além do ponto 3
    # DESFECHO do trade (DA-125): ``{tipo: alvo|stop, em, price, entrada_em, ...}``
    # quando o gatilho rompeu e o preço chegou a um dos dois. A partir dele o setup
    # está ENCERRADO — e um setup encerrado não se invalida: não há o que invalidar
    # num trade que já terminou. Sem isto, o LINK-USD do dia 30/08 saía "INVALIDADO"
    # oito horas depois de ter ATINGIDO O ALVO.
    desfecho: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "p1": self.p1, "p2": self.p2, "p3": self.p3,
            "trigger": self.trigger, "state": self.state,
            "direction": self.direction,
            # `invalidado` é o estado EFETIVO: falso quando o trade encerrou antes.
            # O fato estrutural cru (o fechamento além do ponto 3) continua em
            # `invalidado_em` — ele aconteceu, só não decide mais o veredito.
            "invalidado": self.invalidado and self.desfecho is None,
            "invalidado_em": self.invalidado_em,
            "desfecho": self.desfecho,
            "encerrado": self.desfecho is not None,
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


# Cache CURTO da série preparada (série + médias), em memória e por processo.
#
# ``_prep`` não é barato: ele roda seis janelas (três MMS + quatro EMAs) sobre a
# série INTEIRA, em pandas. E ele já era chamado DUAS vezes por (ativo, frame) numa
# única linha do scan — ``build_actionable_plan`` prepara a série e
# ``detect_price_structure``, chamada logo abaixo, prepara de novo. Com o Storm
# entrando na mesma linha seriam três.
#
# TTL de 60s, e o número não é gosto: é maior que uma varredura inteira da watchlist
# (medida em 6–9s), então dentro de UMA passada todas as leituras do mesmo ativo
# enxergam exatamente a mesma série — o que é mais correto que hoje, onde duas
# preparações do mesmo frame podiam, em tese, cair em lados diferentes de uma
# atualização. E é curto o bastante pra a próxima varredura do usuário já pegar
# barra nova. É irmão do TTL de 30s do ``_live_price`` no scanner, pela mesma razão.
#
# O que ele NÃO faz: guardar dado entre datas (``curr_date`` está na chave, então o
# date-guard continua inteiro) nem persistir nada em disco.
_PREP_TTL = 60.0
_PREP_MAX = 128
_prep_cache: dict[tuple[str, str, str], tuple[float, pd.DataFrame]] = {}
_prep_lock = threading.Lock()


def _prep(symbol: str, curr_date: str, timeframe: str = _DEFAULT_TIMEFRAME) -> pd.DataFrame:
    """Load the date-guarded series and attach the simple + exponential averages.

    Cacheado por 60s em memória (ver :data:`_PREP_TTL`). Devolve sempre uma CÓPIA:
    o custo de copiar um frame já pronto é ordens de grandeza menor que recalcular
    as médias, e assim nenhum chamador consegue contaminar a série do vizinho.
    """
    chave = (str(symbol), str(curr_date), str(timeframe))
    agora = time.monotonic()
    with _prep_lock:
        achado = _prep_cache.get(chave)
        if achado is not None and agora - achado[0] < _PREP_TTL:
            return achado[1].copy()
    df = _prep_calc(symbol, curr_date, timeframe)
    with _prep_lock:
        if len(_prep_cache) >= _PREP_MAX:
            # poda simples: o cache é pequeno e o TTL é curto — o mais VELHO sai
            mais_velho = min(_prep_cache.items(), key=lambda kv: kv[1][0])[0]
            _prep_cache.pop(mais_velho, None)
        _prep_cache[chave] = (agora, df)
    return df.copy()


def clear_prep_cache() -> None:
    """Esvazia o cache de :func:`_prep`.

    Existe por causa de quem TROCA A FONTE por baixo — a suíte, que substitui
    ``_load_frame`` por séries sintéticas: dois testes com o mesmo símbolo/data e
    dados diferentes cairiam na mesma chave dentro do TTL, e o segundo leria a série
    do primeiro. Em produção a fonte não muda por baixo, e o TTL de 60s é a resposta
    pro dado que se atualiza — mas um cache de processo que ninguém consegue limpar
    é um cache que a gente não controla.
    """
    with _prep_lock:
        _prep_cache.clear()


def _prep_calc(symbol: str, curr_date: str, timeframe: str = _DEFAULT_TIMEFRAME) -> pd.DataFrame:
    """A preparação de verdade (sem cache) — ver :func:`_prep`."""
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

    # INVALIDAÇÃO MEDIDA: a primeira barra APÓS o ponto 3 que FECHA além dele. Por
    # fechamento e não por pavio — é a mesma régua que o stop usa ao levar folga de
    # ATR pra não ser tirado por sombra, e a estrutura se julga no fechamento.
    # Uma vez perdido, o ponto 3 não "desperde": o padrão morreu naquela barra,
    # mesmo que o preço volte depois. Quem volta forma OUTRO padrão.
    p3_price = round(float(lo.iloc[p3] if pt_kinds[2] == "L" else hi.iloc[p3]), 2)
    invalidado_em = _primeira_barra_alem(df, p3, p3_price, direction == "compra", fmt)

    return Pattern123(
        pt(p1, pt_kinds[0]), pt(p2, pt_kinds[1]), pt(p3, pt_kinds[2]),
        trigger, state, direction,
        invalidado=invalidado_em is not None, invalidado_em=invalidado_em,
    )


def _primeira_barra_alem(df: pd.DataFrame, desde: int, nivel: float,
                         compra: bool, fmt: str) -> str | None:
    """Data da primeira barra após ``desde`` cujo FECHAMENTO passou de ``nivel``.

    ``None`` quando nunca passou. É o "quando" que o card precisa dizer: um selo de
    "invalidado" sem data não deixa o leitor conferir nada.
    """
    fech = df["Close"].astype(float)
    for i in range(desde + 1, len(df)):
        v = float(fech.iloc[i])
        if (v < nivel) if compra else (v > nivel):
            return df["Date"].iloc[i].strftime(fmt)
    return None


# ── A CRONOLOGIA DO PADRÃO: em que ORDEM as coisas aconteceram (DA-124) ──────
#
# *"pq invalidado se ele atingiu o alvo?"* — o Samyr, olhando o LINK-USD no 1h.
#
# A pergunta expôs que a tela não conta a ORDEM, e a ordem é a única coisa que
# decide se um trade ganhou ou perdeu. Cronometrado na série, o caso do print
# (LINK-USD 1h, análise de 30/08):
#
#   30/08 09:00   nasce o ponto 3 (11,34) — o padrão passa a existir
#   30/08 13:00   gatilho 11,52 TOCADO — a entrada aconteceu
#   30/08 15:00   alvo 11,63 TOCADO — duas barras depois da entrada
#   30/08 23:00   stop 11,27 tocado E o fechamento cai além do ponto 3: INVALIDA
#
# Ou seja: **ele tem razão** — o alvo foi alcançado com o padrão VIVO, e a morte
# veio oito horas depois. Um rótulo "invalidado" sozinho esconde que o setup pagou.
# (O caminho inverso também existe e engana ao contrário: preço tocando o nível do
# alvo DEPOIS da morte, com quem entrou já stopado. Sem timestamps os dois são
# indistinguíveis, e a leitura natural erra num dos dois sentidos.)
#
# É a MESMA disciplina da task 008, que tirou o veredito de fechamento do track
# record da inspeção de nível e o pôs na SÉRIE, com direção e ordem: **"o preço
# passou pelo nível" não é "o trade ganhou"**. Aqui ela chega ao gráfico.
#
# O que se mede — e é MEDIDA, não veredito novo: desde o ponto 3 (quando o padrão
# passou a existir), a data do PRIMEIRO toque em cada nível que decide o resultado
# — gatilho, alvo e stop —, e a posição de cada um em relação à invalidação. O
# toque é por AMPLITUDE da barra (mínima ≤ nível ≤ máxima), o mesmo critério do
# ``_primeiro_toque`` do ledger: um alvo não precisa de fechamento além dele para
# ser tocado.
#
# O GATILHO entra na lista de propósito. Sem ele, "tocou o alvo" continua não
# significando "ganhou" — é preciso ter ENTRADO antes, e é o gatilho que diz se e
# quando isso aconteceu.


def _primeiro_toque_na_serie(df: pd.DataFrame, nivel: float, fmt: str) -> str | None:
    """Data da primeira barra cuja AMPLITUDE contém ``nivel``, ou ``None``."""
    if df is None or df.empty or nivel is None:
        return None
    lows, highs = df["Low"].astype(float), df["High"].astype(float)
    tocou = (lows <= float(nivel)) & (highs >= float(nivel))
    if not bool(tocou.any()):
        return None
    return df["Date"].iloc[int(tocou.values.argmax())].strftime(fmt)


# ── O DESFECHO ENCERRA O TRADE, e nada posterior o reabre (DA-125) ───────────
#
# O defeito que isto mata, com o dado real (LINK-USD 1h, run 20260830-232525):
# gatilho 11,52 rompido às 13:00, **alvo 11,63 ATINGIDO às 15:00**, e às 23:00 o
# preço desabou para 10,99 — fechando além do ponto 3. O detector marcou
# ``invalidado=True`` às 23:00, e a tela disse "INVALIDADO" sobre um trade que
# tinha GANHO oito horas antes. **O veredito ficou invertido em relação ao
# dinheiro.**
#
# A regra: **invalidação só vale enquanto o trade está VIVO** — entre o rompimento
# do gatilho e o primeiro desfecho (alvo ou stop, o que vier primeiro). Depois do
# desfecho não há o que invalidar: o setup terminou. É a mesma disciplina do
# ``_primeiro_toque`` do ledger (task 008), onde "um trade que tocou o alvo e
# voltou fica ``bateu_tp`` pra sempre" — só que ali ela já valia e aqui não.
#
# **Antes do gatilho a invalidação continua valendo integralmente**: um padrão que
# perde o ponto 3 sem nunca ter acionado morreu mesmo, e não houve trade nenhum.


def _desfecho_do_padrao(cronologia: dict[str, Any] | None) -> dict[str, Any] | None:
    """O primeiro desfecho do trade — ``None`` quando não houve.

    Derivado da CRONOLOGIA (uma medição só, DA-124): exige o gatilho rompido e,
    depois dele, o primeiro toque em alvo ou stop. Sem entrada não há trade a
    encerrar, e um alvo roçado por um preço que nunca acionou o setup não é
    desfecho de coisa nenhuma.

    Empate na mesma barra resolve pelo STOP — a leitura pessimista, a mesma do
    ledger: sem tick não dá pra saber a ordem dentro da barra, e acerto inflado é
    o pior erro possível num painel que existe pra dizer a taxa real.
    """
    if not cronologia:
        return None
    ev = {e["nome"]: e for e in (cronologia.get("eventos") or [])}
    gat = ev.get("gatilho")
    if not gat:
        return None
    alvo, stop = ev.get("alvo (TP)"), ev.get("stop (SL)")
    cands = [(e["quando"], tipo, e) for tipo, e in (("alvo", alvo), ("stop", stop))
             if e and e["quando"] > gat["quando"]]
    if not cands:
        return None
    # ordena por data e, no empate, o stop vem primeiro (pessimista)
    cands.sort(key=lambda c: (c[0], 0 if c[1] == "stop" else 1))
    quando, tipo, e = cands[0]
    empate = any(c[0] == quando and c[1] != tipo for c in cands)
    return {"tipo": tipo, "em": quando, "price": e["price"],
            "entrada_em": gat["quando"], "entrada": gat["price"],
            "empate_na_barra": empate}


def _cronologia_do_padrao(df, pattern, target, stop, fmt) -> dict[str, Any] | None:
    """A linha do tempo do padrão — ``None`` quando não há padrão.

    Devolve ``{desde, invalidado_em, eventos: [{nome, price, quando, ordem}]}``,
    com ``ordem`` em ``antes`` / ``junto`` / ``depois`` **relativa à invalidação**
    (e ``None`` quando o padrão está vivo — aí não há morte a que se referir).

    Só os três níveis que decidem o resultado entram: gatilho, alvo e stop. A
    invalidação não é evento da lista — ela É o marco contra o qual os outros se
    ordenam."""
    if pattern is None or df is None or df.empty:
        return None
    p3 = (getattr(pattern, "p3", None) or {}).get("date")
    if not p3:
        return None
    vivos = df[df["Date"] >= pd.to_datetime(p3)]
    if vivos.empty:
        return None
    em = getattr(pattern, "invalidado_em", None) if getattr(pattern, "invalidado", False) else None
    niveis = [("gatilho", getattr(pattern, "trigger", None)),
              ("alvo (TP)", (target or {}).get("price")),
              ("stop (SL)", (stop or {}).get("price"))]
    eventos = []
    for nome, preco in niveis:
        if preco is None:
            continue
        quando = _primeiro_toque_na_serie(vivos, float(preco), fmt)
        if quando is None:
            continue
        ordem = None
        if em:
            ordem = "antes" if quando < em else ("junto" if quando == em else "depois")
        eventos.append({"nome": nome, "price": round(float(preco), 2),
                        "quando": quando, "ordem": ordem})
    eventos.sort(key=lambda e: e["quando"])
    return {"desde": p3, "invalidado_em": em, "eventos": eventos}


# ── PROJEÇÃO DO PONTO 3: onde ele precisa nascer pra o padrão validar ─────────
#
# "Se tiver em formação de 123, marcar onde deve ser a nova formação do 3, tipo uma
# preparação para acompanhar a hora de entrar."
#
# A faixa NÃO é chutada: ela sai da própria definição do padrão, que já está escrita
# no detector. Num 1-2-3 de compra deste módulo o ponto 3 é um fundo ASCENDENTE —
# acima da mínima do ponto 1 (perdê-la mata a formação) e abaixo da máxima do ponto 2
# (acima dela o preço já rompeu o gatilho e não há mais recuo a esperar). Na venda é
# o espelho: um topo DESCENDENTE, abaixo da máxima do ponto 1 e acima da mínima do 2.
#
# E a regra é do MÉTODO ABERTO, nunca a do outro: no Storm a semântica dos pontos é
# invertida (o ponto 2 é o extremo, não o swing intermediário) e o ponto 3 é o
# PRÓXIMO candle, não um swing futuro qualquer. Misturar as duas projeções poria na
# tela uma faixa que a regra daquele método não sustenta — ver :func:`_projecao_storm`.
def _projecao_p3(
    df: pd.DataFrame, lows: list[int], highs: list[int], price: float,
    pattern: Pattern123 | None, fmt: str = "%Y-%m-%d",
) -> dict[str, Any] | None:
    """A faixa do ponto 3 do 1-2-3 de SWINGS, quando ela existe.

    Dois casos, e só dois:

    * **gestação** — há um par de swings (fundo→topo, ou topo→fundo) e nenhum triplo
      válido ainda: o próximo swing pode ser o ponto 3;
    * **novo após invalidação** — o padrão morreu, e o par 1-2 que sobrou ainda pode
      parir OUTRO ponto 3.

    Devolve ``None`` com padrão VIVO (ali o ponto 3 já existe — o que falta é o
    gatilho, que a tela já marca) e quando a regra não delimita a faixa. Declarar
    ausente é o certo: desenhar uma faixa de espera que a regra não sustenta seria
    inventar o nível mais perigoso da tela, o que diz "compre aqui".
    """
    if pattern is not None and not pattern.invalidado:
        return None
    if pattern is not None:
        # o par que sobrou do padrão morto
        compra = pattern.direction == "compra"
        piso = float(pattern.p1["price"]) if compra else float(pattern.p2["price"])
        teto = float(pattern.p2["price"]) if compra else float(pattern.p1["price"])
        caso = "novo_apos_invalidacao"
    else:
        seq = _alternating(df, lows, highs)
        if len(seq) < 2:
            return None
        (i1, k1), (i2, k2) = seq[-2], seq[-1]
        lo, hi = df["Low"].astype(float), df["High"].astype(float)
        if (k1, k2) == ("L", "H"):
            compra, caso = True, "gestacao"
            piso, teto = round(float(lo.iloc[i1]), 2), round(float(hi.iloc[i2]), 2)
        elif (k1, k2) == ("H", "L"):
            compra, caso = False, "gestacao"
            piso, teto = round(float(lo.iloc[i2]), 2), round(float(hi.iloc[i1]), 2)
        else:
            return None
    if teto <= piso:
        return None
    # O PISO já foi perdido: não há 1-2-3 em gestação, há outra estrutura. Dizer
    # isso é informação; desenhar a faixa mesmo assim seria afirmar o contrário.
    p = float(price)
    if (p < piso) if compra else (p > teto):
        return {"direcao": "compra" if compra else "venda", "caso": caso,
                "low": None, "high": None,
                "motivo": (f"o ponto 1 ({piso:,.2f}) foi perdido — não há 1-2-3 de "
                           f"{'compra' if compra else 'venda'} em gestação nesta série."
                           if compra else
                           f"o ponto 1 ({teto:,.2f}) foi rompido — não há 1-2-3 de "
                           f"venda em gestação nesta série.")}
    return {
        "direcao": "compra" if compra else "venda",
        "caso": caso,
        "low": round(piso, 2), "high": round(teto, 2),
        "price": round((piso + teto) / 2, 2),
        "condicao": (
            f"vira um 1-2-3 de compra se fizer um FUNDO acima de {piso:,.2f} "
            f"(perder esse nível mata a formação) e depois romper {teto:,.2f}"
            if compra else
            f"vira um 1-2-3 de venda se fizer um TOPO abaixo de {teto:,.2f} "
            f"(romper esse nível mata a formação) e depois perder {piso:,.2f}"),
        "gatilho_futuro": round(teto if compra else piso, 2),
    }


def _projecao_storm(df: pd.DataFrame, fmt: str = "%Y-%m-%d") -> dict[str, Any] | None:
    """A faixa do ponto 3 do STORM — e ela é de OUTRA natureza.

    O Storm lê TRÊS CANDLES CONSECUTIVOS, então o ponto 3 não é "um swing futuro
    qualquer": é **o próximo candle**, e a faixa é do que ele precisa fazer. Numa
    compra, dados o candle 1 (alta/lateral) e o candle 2 (o fundo), o próximo valida
    se FECHAR acima do fechamento do ponto 2 e a máxima dele FALHAR em romper a do
    ponto 1 — a falha é o coração do padrão.

    Por isso a faixa é ``(fechamento do p2, máxima do p1)`` e não a mesma do módulo:
    a semântica do ponto 2 é invertida entre os dois métodos, e usar a régua do outro
    poria na tela uma preparação que a regra daquele setup não sustenta.

    ``None`` quando os dois últimos candles não formam o começo do padrão — que é o
    caso comum, e dizer nada é melhor que desenhar espera pra um setup que não está
    nascendo.
    """
    if len(df) < 2:
        return None
    o = df["Open"].astype(float).values
    h = df["High"].astype(float).values
    lo = df["Low"].astype(float).values
    c = df["Close"].astype(float).values
    a, b = len(df) - 2, len(df) - 1
    quando = "o PRÓXIMO candle (o ponto 3 do Storm é o candle seguinte ao fundo)"
    if c[a] >= o[a] and lo[b] < lo[a]:            # 1 alta/lateral + 2 é o fundo
        piso, teto = round(float(c[b]), 2), round(float(h[a]), 2)
        if teto <= piso:
            return None
        return {"direcao": "compra", "caso": "gestacao_storm", "quando": quando,
                "low": piso, "high": teto,
                "price": round((piso + teto) / 2, 2),
                "condicao": (
                    f"vira um 1-2-3 Storm de compra se {quando} FECHAR acima de "
                    f"{piso:,.2f} e a máxima dele NÃO romper {teto:,.2f} — a falha "
                    f"em romper o ponto 1 é o coração do padrão"),
                "gatilho_futuro": None}
    if c[a] <= o[a] and h[b] > h[a]:              # 1 baixa/lateral + 2 é o topo
        piso, teto = round(float(lo[a]), 2), round(float(c[b]), 2)
        if teto <= piso:
            return None
        return {"direcao": "venda", "caso": "gestacao_storm", "quando": quando,
                "low": piso, "high": teto,
                "price": round((piso + teto) / 2, 2),
                "condicao": (
                    f"vira um 1-2-3 Storm de venda se {quando} FECHAR abaixo de "
                    f"{teto:,.2f} e a mínima dele NÃO perder {piso:,.2f} — a falha "
                    f"em romper o ponto 1 é o coração do padrão"),
                "gatilho_futuro": None}
    return None


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
            # "Setup ativo agora" saiu (DA-121): a leitura natural da palavra em
            # português aponta para a fase ERRADA — o dono do produto leu "ativo"
            # como "em movimento para o alvo", que é o oposto. A FASE vem primeiro
            # e o mecanismo em seguida, o mesmo par que a tela mostra.
            f"🎯 **Na entrada agora — recuo à média** — a mínima está "
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
    # A FAIXA onde o ponto 3 precisa nascer, quando o padrão está em gestação ou
    # morreu. É a "preparação para acompanhar a hora de entrar" — derivada da regra
    # do detector, nunca chutada. ``None`` com padrão vivo: ali o ponto 3 já existe.
    projecao_p3: dict[str, Any] | None = None
    # A CRONOLOGIA do padrão (DA-124): desde quando ele existe, quando invalidou, e
    # em que ORDEM o preço tocou gatilho, alvo e stop. Sem isto a tela mostra o
    # preço passando pelo alvo com um rótulo "invalidado" ao lado, e a leitura
    # natural erra — num sentido ou no outro, conforme a ordem real.
    cronologia: dict[str, Any] | None = None

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
            "projecao_p3": self.projecao_p3,
            "cronologia": self.cronologia,
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


# A base da entrada no GATILHO, escrita uma vez: ela é usada tanto pelo setup ainda
# não acionado quanto pelo R:R retrospectivo do acionado (ver :func:`_com_percurso`),
# e duas redações do mesmo motivo viram duas frases divergentes na tela.
_ENTRY_BASIS_GATILHO = {
    True: "gatilho — rompimento da máxima do ponto 2",
    False: "gatilho — perda da mínima do ponto 2",
}


def _entry_ref(pattern: Pattern123, price: float, compra: bool) -> tuple[float, str]:
    """Entrada de referência do setup e o motivo dela, escrito.

    Enquanto o padrão não acionou, a entrada é o GATILHO (é onde se entra). Depois
    de acionado o gatilho já ficou para trás, então a referência honesta é o PREÇO
    ATUAL — é o que ainda resta de trade para quem lê a tela agora."""
    if pattern.state == "acionado":
        return float(price), "preço atual (padrão já acionado)"
    return float(pattern.trigger), _ENTRY_BASIS_GATILHO[compra]


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
    risk_reward = _com_percurso(risk_reward, pattern.trigger, pattern.state, price,
                                stop, target, compra, _ENTRY_BASIS_GATILHO[compra])
    return invalidation, stop, target, risk_reward


def _percurso(trigger: float | None, price: float,
              target: dict | None, compra: bool) -> float | None:
    """Quanto do caminho GATILHO → ALVO o preço já andou, em %.

    Medida pura, sem faixa arbitrária: é a régua que separa "o método dá trade
    ruim" de "cheguei tarde". Pode passar de 100 (o alvo já foi atingido) e pode
    ficar negativa (o preço voltou para trás do gatilho depois de acionar) — os
    dois são fatos, e nenhum se arredonda pra caber num rótulo bonito.
    """
    tgt = (target or {}).get("price")
    if tgt is None or trigger is None:
        return None
    gat, tgt = float(trigger), float(tgt)
    caminho = (tgt - gat) if compra else (gat - tgt)
    if caminho <= 0:
        return None      # alvo no gatilho ou atrás dele: o ``note`` do R:R já explica
    andado = (float(price) - gat) if compra else (gat - float(price))
    return round(andado / caminho * 100, 1)


def _com_percurso(rr: dict | None, trigger: float | None, state: str | None,
                  price: float, stop: dict | None, target: dict | None,
                  compra: bool, basis_gatilho: str) -> dict | None:
    """Acrescenta ao R:R o que ele sozinho não conta: **de onde ele caiu**.

    Depois que o padrão aciona, :func:`_entry_ref` passa a medir a partir do PREÇO
    ATUAL — honesto, é o que ainda resta de trade —, mas o stop continua ancorado
    na invalidação. A consequência aritmética é que o R:R DESABA à medida que o
    trade amadurece: no print de 29/08 (venda, ação de 465) o stop 526,92 contra
    alvo 460,21 com o preço em 465,58 dá risco 61,34 × retorno 5,37 = **0,05**.
    Não é alvo conservador nem stop largo — é um setup que já andou ~92% do
    caminho.

    A tela mostrava esse 0,05 com o mesmo peso de um setup fresco, e a conclusão
    natural de quem lê é "o método dá trades ruins", quando o que houve foi chegar
    tarde. Então vão junto, sempre que o padrão está ACIONADO:

    * ``no_gatilho`` — o R:R que o setup OFERECIA no gatilho (o que o método
      entregou de fato a quem entrou na hora);
    * ``andado_pct`` / ``sobra_pct`` — a régua do percurso (:func:`_percurso`);
    * ``motivo`` — a frase, pra o número baixo nunca aparecer sozinho.

    Padrão não acionado devolve o R:R intacto: ali a entrada É o gatilho, não há
    dois números a comparar, e inventar um segundo seria repetir o mesmo.

    Recebe primitivos e não um ``Pattern123`` porque o Storm decai exatamente
    igual — mesma regra de entrada, mesmo stop parado — e uma segunda cópia da
    conta seria a mesma verdade escrita duas vezes, livre pra divergir.
    """
    if rr is None or trigger is None or state != "acionado":
        return rr
    andado = _percurso(trigger, price, target, compra)
    gatilho = _risk_reward(float(trigger), basis_gatilho, stop, target, compra)
    out = dict(rr)
    if gatilho is not None:
        out["no_gatilho"] = {k: gatilho.get(k) for k in
                             ("entry", "entry_basis", "risk", "reward", "rr", "note")}
    if andado is not None:
        out["andado_pct"] = andado
        out["sobra_pct"] = round(100.0 - andado, 1)
        if andado >= 100:
            out["motivo"] = (
                f"o gatilho ficou para trás e o alvo já foi alcançado — o percurso "
                f"do setup andou {andado:.0f}% e não sobra movimento a projetar."
            )
        elif andado > 0:
            out["motivo"] = (
                f"o gatilho ficou para trás: o preço já andou {andado:.0f}% do "
                f"caminho até o alvo e sobra {100 - andado:.0f}%. O R:R daqui mede "
                f"o que RESTA, não o que o setup ofereceu."
            )
        else:
            out["motivo"] = (
                "o padrão acionou e o preço voltou para trás do gatilho — o R:R "
                "daqui mede a entrada a mercado agora, não a do rompimento."
            )
    return out


def _risk_reward(
    entry: float, entry_basis: str, stop: dict | None,
    target: dict | None, compra: bool,
) -> dict | None:
    """R:R do setup a partir de níveis REAIS (entrada, stop, alvo).

    ``None`` só quando NÃO HÁ ESQUELETO nenhum (sem stop e sem alvo) — aí não há
    nem o que explicar. Faltando UM dos dois, ou com os dois do lado errado da
    entrada (alvo já para trás, stop além dela), devolve ``rr=None`` com o motivo
    em ``note``: a tela diz POR QUE não há R:R em vez de exibir um número sem
    sentido — ou, pior, de simplesmente não mostrar a linha.

    O ``None`` mudo era o buraco: um padrão com stop, invalidação e pontos 1-2-3
    desenhados, mas sem topo anterior à frente da entrada, devolvia ``None`` — e a
    tela ficava SEM linha de R:R, indistinguível de um frame que nem padrão tem.
    Nos prints do Samyr (mesmo ativo, 29/08) o R:R aparecia só no diário; no 1h e
    no 4h ele sumia sem uma palavra, e sumir sem palavra é o mesmo defeito que a
    DA-072 combate do outro lado (número incoerente publicado).
    """
    tem_stop = stop is not None and stop.get("price") is not None
    tem_alvo = target is not None and target.get("price") is not None
    if not tem_stop and not tem_alvo:
        return None
    if not tem_alvo or not tem_stop:
        # Precisão PUBLICADA aqui também: a perna que EXISTE continua sendo um
        # número conferível na tela, mesmo sem a razão.
        entry = round(entry, 2)
        base = {"entry": entry, "entry_basis": entry_basis,
                "risk": None, "reward": None, "rr": None, "note": None}
        if not tem_alvo:
            onde = "topo anterior acima" if compra else "fundo anterior abaixo"
            base["risk"] = round(abs(entry - float(stop["price"])), 2)
            base["note"] = (
                f"sem alvo estrutural à frente da entrada — não há {onde} dela nesta "
                "série, então não há retorno a projetar (o risco continua medido)."
            )
        else:
            base["reward"] = round(abs(float(target["price"]) - entry), 2)
            base["note"] = ("sem stop definido nesta leitura — sem risco a medir, "
                            "a razão não se calcula.")
        return base
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

    # A CRONOLOGIA primeiro, o DESFECHO dela (DA-124 + DA-125). O desfecho volta
    # PARA O PADRÃO: é ele que decide se a invalidação posterior ainda vale, e o
    # `as_dict` do padrão já sabe disso. Só aqui os dois lados existem juntos — o
    # detector não conhece alvo nem stop, e por isso não podia decidir sozinho.
    cronologia = _cronologia_do_padrao(df, struct.pattern, target, stop, fmt)
    if struct.pattern is not None:
        struct.pattern.desfecho = _desfecho_do_padrao(cronologia)

    return ActionablePlan(
        symbol=symbol, as_of=as_of, price=price, timeframe=tf_ref,
        horizon=_HORIZON[setup_state], setup_state=setup_state,
        buy_zone=buy_zone, realize_zone=realize_zone, pullback_zone=pullback_zone,
        pattern=struct.pattern.as_dict() if struct.pattern is not None else None,
        invalidation=invalidation, stop=stop, target=target, risk_reward=risk_reward,
        setup_source=setup_source,
        projecao_p3=_projecao_p3(df, lows, highs, price, struct.pattern, fmt),
        cronologia=cronologia,
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
    amplitude: float    # maior máxima − menor mínima dos 3 candles
    # As ENTRADAS do padrão, cada uma com o seu gatilho e o seu estado. A spec
    # escreve "rompimento da máxima do ponto 2 (ou 3)": são DUAS LEITURAS DO MESMO
    # padrão (mesmos p1/p2/p3, mesmo stop, mesma amplitude), não dois padrões. O
    # gatilho deixou de ser campo do PADRÃO justamente por isso — guardá-lo aqui
    # obrigaria a eleger uma das duas leituras como "a" verdadeira.
    entradas: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "p1": self.p1, "p2": self.p2, "p3": self.p3,
            "direction": self.direction, "amplitude": self.amplitude,
            "entradas": self.entradas,
        }


# A PROPORÇÃO DO CANDLE ACIMA DA MÉDIA — leitura VISUAL, não a regra de decisão.
#
# História, porque ela explica o desenho: a primeira versão desta task fez o Éden
# decidir pela proporção ("basta a maior parte do candle estar acima da MME 8"), lida
# dos exemplos em gráfico da aula. A MEDIÇÃO contra uma implementação de referência
# (scanner público do QuantBrasil, 30/08) corrigiu o rumo:
#
#   * o que ela REFUTOU: não é preciso o candle INTEIRO acima da média — dois ativos
#     marcados como Éden de compra estão com 77,5% e 82,8% do candle acima da MME 8;
#   * o que ela mostrou ser o critério OPERANTE: o **FECHAMENTO** contra as duas
#     médias. Todos os marcados como compra têm close acima da MME 8 E da MME 80 com
#     a 8 acima da 80; e os "não" falham exatamente onde deveriam — inclusive os dois
#     que são a ARMADILHA (close acima da 8, abaixo da 80, com 8 < 80).
#
# Os dois critérios COINCIDEM na prática (num candle normal, o fechamento acima puxa
# a maior parte junto). O que decide é o close: é determinístico, é mais estável e é
# o que a referência usa. A proporção fica MEDIDA e publicada no payload — ela é a
# leitura visual de "o candle está acima da média?" e informa o leitor —, mas não
# autoriza nem veta nada.
#
# Limite honesto da medição que corrigiu isto: 10 ativos, tabela extraída de texto
# achatado (a atribuição por ativo pode ter deslocamento) e um caso que não fechou.
# É INDÍCIO forte, não prova — e é por isso que a proporção continua no payload em
# vez de ser apagada: se a evidência virar, o número já está lá.
_FRACAO_ACIMA_MIN = 0.5


def _fracao_acima(high: float, low: float, media: float) -> float:
    """Que fração do RANGE do candle fica acima de ``media`` (0 a 1).

    Candle inteiro acima → 1,0; inteiro abaixo → 0,0; cortado ao meio → 0,5. Candle
    sem range (doji perfeito) não tem proporção a medir: vale 1 se está acima da
    média, 0 se abaixo, 0,5 se exatamente nela.
    """
    rng = float(high) - float(low)
    if rng <= 0:
        return 1.0 if high > media else (0.0 if high < media else 0.5)
    acima = float(high) - max(float(media), float(low))
    return max(0.0, min(1.0, acima / rng))


def _candle_acima(high: float, low: float, media: float) -> bool:
    """O candle conta como ACIMA da média?

    Estritamente MAIOR que a metade. O empate exato (metade acima, metade abaixo) não
    é nem acima nem abaixo — e num filtro que AUTORIZA trade, empate não autoriza:
    ``_candle_acima`` e ``_candle_abaixo`` são ambos falsos ali, de propósito.
    """
    return _fracao_acima(high, low, media) > _FRACAO_ACIMA_MIN


def _candle_abaixo(high: float, low: float, media: float) -> bool:
    return _fracao_acima(high, low, media) < (1.0 - _FRACAO_ACIMA_MIN)


# ── VOCABULÁRIO DO ÉDEN — UM lugar decide como o filtro se escreve ────────────
#
# *"nos cards de texto onde usamos Éden, identifica Éden de Alta e de Baixa na menção."*
#
# O dado sempre existiu (``direcao``, ``alinhado``, ``armadilha``, ``zona_neutra``) e
# nunca chegava ao texto: o card mostrava "MME 8 × MME 80" com os dois valores e não
# dizia de que Éden se tratava. E a prosa do módulo dizia "Éden de compra/venda" — o
# nome do SINAL — quando o Éden é filtro de REGIME. **Alta/Baixa** é o rótulo de tela.
#
# Sai daqui e de nenhum outro lugar. Foi escrevendo rótulo à mão em cada superfície que
# a tela ganhou três jeitos de dizer timeframe (DA-095); o Éden não repete isso.
# ``rotulo`` é a forma de leitura e ``rotulo_curto`` a de espaço apertado (etiqueta na
# vela, célula do scan) — mesmo par de formas do vocabulário de timeframe.
_EDEN_ROTULO = {
    "alta":         ("Éden de Alta", "Éden de Alta"),
    "baixa":        ("Éden de Baixa", "Éden de Baixa"),
    "armadilha":    ("ARMADILHA (entre as médias)", "armadilha"),
    "neutra":       ("ZONA NEUTRA (entre as médias)", "zona neutra"),
    "desalinhado":  ("sem Éden (médias desalinhadas)", "sem Éden"),
    "indisponivel": ("Éden indisponível", "Éden indisponível"),
}
# A equivalência com a doutrina do Stormer, pro `title` — quem leu "Éden de compra" no
# material precisa reconhecer o que está na tela.
_EDEN_DOUTRINA = {"alta": "Éden de compra", "baixa": "Éden de venda"}


def _eden_nomes(estado: str) -> dict[str, Any]:
    """Os campos de NOME de um estado do Éden, prontos pra viajar no payload."""
    rotulo, curto = _EDEN_ROTULO[estado]
    out = {"estado": estado, "rotulo": rotulo, "rotulo_curto": curto}
    if estado in _EDEN_DOUTRINA:
        out["doutrina"] = _EDEN_DOUTRINA[estado]
    return out


def _eden_nome_curto(eden: dict[str, Any]) -> str:
    """O nome CURTO do estado do Éden, sempre do vocabulário único.

    Lê o rótulo pronto quando ele veio; senão o deriva de ``estado`` ou de ``direcao``
    — nunca de uma segunda tabela. É por aqui que passa o Éden montado à mão (teste,
    plano em cache antigo) sem virar prosa quebrada como "contra sem Éden".
    """
    pronto = eden.get("rotulo_curto")
    if pronto:
        return pronto
    est = eden.get("estado")
    if est in _EDEN_ROTULO:
        return _EDEN_ROTULO[est][1]
    direcao = eden.get("direcao")
    if direcao:
        return _EDEN_ROTULO["alta" if direcao == "compra" else "baixa"][1]
    if eden.get("zona_neutra"):
        return _EDEN_ROTULO["armadilha" if eden.get("armadilha") else "neutra"][1]
    return _EDEN_ROTULO["indisponivel" if eden.get("disponivel") is False
                        else "desalinhado"][1]


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
            "zona_neutra": False, "direcao_estrutural": None,
            "fracao_acima_rapida": None, "fracao_acima_lenta": None,
            "ema_rapida": None, "ema_lenta": None, "preco": None,
            **_eden_nomes("indisponivel"),
            "motivo": (f"série com {n} candles — a MME {_STORM_EMA_LENTA} precisa de pelo "
                       f"menos {_STORM_EMA_LENTA} para significar alguma coisa"),
        }
    rapida = round(float(df[col_r].iloc[-1]), 2)
    lenta = round(float(df[col_l].iloc[-1]), 2)
    ult = df.iloc[-1]
    high, low = float(ult["High"]), float(ult["Low"])
    preco = round(float(ult["Close"]), 2)
    # A DECISÃO é do FECHAMENTO contra cada média (ver o bloco de :data:`_FRACAO_ACIMA_MIN`
    # para a medição que estabeleceu isto). A proporção do candle vai junto no payload
    # como leitura visual, mas não decide.
    ac_r, ab_r = preco > rapida, preco < rapida
    ac_l, ab_l = preco > lenta, preco < lenta
    base = {"disponivel": True, "ema_rapida": rapida, "ema_lenta": lenta, "preco": preco,
            "fracao_acima_rapida": round(_fracao_acima(high, low, rapida), 3),
            "fracao_acima_lenta": round(_fracao_acima(high, low, lenta), 3),
            "zona_neutra": False, "direcao_estrutural": None}
    # A DIREÇÃO ESTRUTURAL é só das médias — ela existe mesmo sem o preço estar do
    # lado certo, e é ela que diz, na zona neutra, qual lado é recuo e qual é repique.
    estrutural = "compra" if rapida > lenta else ("venda" if rapida < lenta else None)
    base["direcao_estrutural"] = estrutural
    if rapida > lenta and ac_r and ac_l:
        return {**base, "alinhado": True, "direcao": "compra", "armadilha": False,
                **_eden_nomes("alta"),
                "motivo": (f"MME {_STORM_EMA_RAPIDA} acima da MME {_STORM_EMA_LENTA} e "
                           "preço acima das duas")}
    if rapida < lenta and ab_r and ab_l:
        return {**base, "alinhado": True, "direcao": "venda", "armadilha": False,
                **_eden_nomes("baixa"),
                "motivo": (f"MME {_STORM_EMA_RAPIDA} abaixo da MME {_STORM_EMA_LENTA} e "
                           "preço abaixo das duas")}
    # ZONA NEUTRA: o candle está ENTRE as duas médias. O Stormer batiza assim a faixa
    # entre a MME 8 e a MME 80 — "esta região, operar aqui é muito mais perigoso" —,
    # e ela NÃO é o mesmo que "sem Éden": o preço está do lado certo de UMA das médias
    # e no meio do caminho da outra. É um TERCEIRO estado, e é o `_storm_qualidade`
    # que decide o que fazer com ele, porque a leitura depende da DIREÇÃO do padrão:
    # entre as médias, o mesmo lugar é recuo saudável pra um lado e repique-armadilha
    # pro outro.
    entre = (ab_r and ac_l) or (ac_r and ab_l)
    if entre:
        de_baixo = ab_r and ac_l          # preço abaixo da rápida, acima da lenta
        armadilha = (estrutural == "venda" and ac_r) or (estrutural == "compra" and ab_r)
        onde = (f"abaixo da MME {_STORM_EMA_RAPIDA} e acima da MME {_STORM_EMA_LENTA}"
                if de_baixo else
                f"acima da MME {_STORM_EMA_RAPIDA} e abaixo da MME {_STORM_EMA_LENTA}")
        return {**base, "alinhado": False, "direcao": None, "zona_neutra": True,
                "armadilha": bool(armadilha),
                # ARMADILHA e ZONA NEUTRA são o mesmo lugar do gráfico com leituras
                # opostas — cada um leva o SEU nome, nunca os dois como "sem Éden".
                **_eden_nomes("armadilha" if armadilha else "neutra"),
                "motivo": (f"ZONA NEUTRA: o preço está {onde} — a região entre as duas "
                           "médias. Operar aqui é muito mais perigoso: exige "
                           "seletividade extra, e o lado que vale depende da tendência "
                           f"({estrutural or 'indefinida'} pelas médias).")}
    return {**base, "alinhado": False, "direcao": None, "armadilha": False,
            **_eden_nomes("desalinhado"),
            "motivo": (f"MME {_STORM_EMA_RAPIDA} e MME {_STORM_EMA_LENTA} cruzadas ou o "
                       "preço exatamente sobre uma delas — sem Éden")}


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


# Rótulo pt-BR de cada ENTRADA e a ordem dela na fila do preço.
_STORM_ENTRADA_LABEL = {
    "ponto2": "rompimento da máxima do ponto 2",
    "ponto3": "rompimento da máxima do ponto 3",
}
_STORM_ENTRADA_LABEL_VENDA = {
    "ponto2": "perda da mínima do ponto 2",
    "ponto3": "perda da mínima do ponto 3",
}
# ANTECIPADA = o gatilho que o preço alcança PRIMEIRO (o mais baixo na compra, o
# mais alto na venda): entra antes da confirmação, com risco menor até o stop — e
# mais sinal falso. CONFIRMADA é o outro. Quando os dois níveis coincidem não há
# duas leituras: há uma, e a tela diz isso em vez de repetir o mesmo número.
_STORM_ORDEM = {
    "antecipada": "entra antes — gatilho mais próximo, risco menor, mais sinal falso",
    "confirmada": "espera a confirmação — gatilho mais longe, risco maior, menos sinal falso",
    "unica": "os pontos 2 e 3 têm o mesmo nível: as duas entradas coincidem",
}


def _storm_estado(h, lo, c, idx_p3: int, trigger: float, compra: bool) -> str:
    """Estado de UM gatilho: nunca rompeu, rompeu e segue, ou rompeu e voltou."""
    last_close = round(float(c[-1]), 2)
    if compra:
        broke = bool((h[idx_p3 + 1:] > trigger).any())
        if not broke:
            return "formando"
        return "acionado" if last_close > trigger else "rompeu_retracou"
    broke = bool((lo[idx_p3 + 1:] < trigger).any())
    if not broke:
        return "formando"
    return "acionado" if last_close < trigger else "rompeu_retracou"


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

    DUAS ENTRADAS, não uma. A spec escreve "rompimento da máxima do ponto 2 (ou 3)"
    — são dois pontos de entrada do MESMO padrão, e cada um tem o seu gatilho e o
    seu estado. A task 022 colapsava os dois no mais conservador (o máximo dos dois
    na compra); colapsar escondia justamente a leitura que entra antes.
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
    kinds = ("H", "L", "H") if compra else ("L", "H", "L")
    rotulos = _STORM_ENTRADA_LABEL if compra else _STORM_ENTRADA_LABEL_VENDA
    brutos = {"ponto2": float(h[b] if compra else lo[b]),
              "ponto3": float(h[d] if compra else lo[d])}
    entradas: list[dict[str, Any]] = []
    for nome in ("ponto2", "ponto3"):
        trigger = round(brutos[nome], 2)
        entradas.append({
            "entrada": nome,
            "label": rotulos[nome],
            "trigger": trigger,
            "state": _storm_estado(h, lo, c, d, trigger, compra),
        })
    # ORDEM na fila do preço, na PRECISÃO PUBLICADA (DA-072): dois gatilhos que a
    # tela mostra iguais são um só — comparar no valor cru inventaria uma segunda
    # leitura que o leitor não consegue distinguir.
    t2, t3 = entradas[0]["trigger"], entradas[1]["trigger"]
    if t2 == t3:
        entradas = [{**entradas[0], "entrada": "ponto2e3",
                     "label": rotulos["ponto2"] + " (o ponto 3 está no mesmo nível)",
                     "ordem": "unica"}]
    else:
        primeiro = 0 if ((t2 < t3) if compra else (t2 > t3)) else 1
        for i, e in enumerate(entradas):
            e["ordem"] = "antecipada" if i == primeiro else "confirmada"
    for e in entradas:
        e["ordem_label"] = _STORM_ORDEM[e["ordem"]]
        e["state_label"] = _STORM_ESTADO.get(e["state"], e["state"])
    return StormPattern(
        p1=_storm_ponto(df, a, kinds[0], fmt),
        p2=_storm_ponto(df, b, kinds[1], fmt),
        p3=_storm_ponto(df, d, kinds[2], fmt),
        direction=direction, amplitude=amplitude, entradas=entradas,
    )


def _storm_levels(
    pat: StormPattern, atr: float | None, price: float,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """``(invalidação, stop, leituras)`` do Storm — nenhum nível herdado do outro 1-2-3.

    **Invalidação e stop são COMUNS às duas leituras**: as duas entradas são do
    MESMO padrão, e o que mata o padrão é o mesmo ponto 2 nas duas.

    * **invalidação** — o PONTO 2 (a mínima na compra, a máxima na venda). É o fundo
      que o padrão declara: perdê-lo é dizer que a reversão não aconteceu.
    * **stop** — o PONTO 2, exato. Sem a folga de meio ATR que o 1-2-3 de swings usa:
      medida na watchlist real, ela derruba a mediana de R:R de 1,13 para 0,80 porque
      meio ATR14 é enorme perto da amplitude de TRÊS candles. O quanto abaixo do ponto
      2 se põe a ordem é decisão de quem opera — ver o comentário abaixo.
    * **alvo e R:R são POR LEITURA** — a amplitude é a mesma, mas ela é lançada a
      partir do gatilho DAQUELA entrada, e o risco é medido daquele gatilho até o
      mesmo stop. É aritmética, e é o ponto todo das duas leituras: gatilho mais
      perto com stop igual ⇒ risco menor ⇒ R:R melhor, ao custo de entrar antes da
      confirmação.
    """
    compra = pat.direction != "venda"
    inval_price = float(pat.p2["low"] if compra else pat.p2["high"])
    invalidation = {
        "label": f"perda do ponto 2 ({pat.p2['date']})" if compra
                 else f"retomada do ponto 2 ({pat.p2['date']})",
        "price": round(inval_price, 2),
        "meaning": ("o setup morre se perder o ponto 2 — é o fundo que a reversão "
                    "declarou" if compra else
                    "o setup morre se voltar acima do ponto 2 — é o topo que a reversão declarou"),
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
    stop = {"label": "stop (SL)", "price": round(inval_price, 2),
            "anchor": round(inval_price, 2), "atr": atr, "slack": 0.0,
            "basis": ("no ponto 2 — a spec põe o stop abaixo dele, e o quanto abaixo é "
                      "decisão de quem opera (não se inventa folga aqui)")}

    leituras: list[dict[str, Any]] = []
    for e in pat.entradas:
        trigger = float(e["trigger"])
        # O alvo é ancorado no GATILHO daquela leitura, nunca no preço de agora:
        # projetado do preço corrente ele fugiria junto com o preço e nunca seria
        # atingido.
        alvo = trigger + pat.amplitude if compra else trigger - pat.amplitude
        target = {
            "label": (f"projeção da amplitude dos 3 candles ({pat.amplitude:,.2f}) "
                      f"a partir do gatilho do {e['entrada'].replace('ponto', 'ponto ')}"),
            "price": round(float(alvo), 2), "amplitude": pat.amplitude,
            "low": None, "high": None, "band_basis": None, "same_as_realize": False,
        }
        if e["state"] == "acionado":
            entry, entry_basis = float(price), "preço atual (entrada já acionada)"
        else:
            entry, entry_basis = trigger, f"gatilho — {e['label']}"
        # O Storm decai igual ao 1-2-3: acionada a leitura, a entrada vira o preço
        # e o stop não sai do ponto 2 — então o R:R de uma entrada que já andou não
        # se compara com o que ela oferecia no rompimento. Vão os dois.
        base_gat = f"gatilho — {e['label']}"
        rr_leitura = _risk_reward(entry, entry_basis, stop, target, compra)
        leituras.append({
            **e, "target": target,
            "risk_reward": _com_percurso(rr_leitura, trigger, e["state"], price,
                                         stop, target, compra, base_gat),
        })
    return invalidation, stop, leituras


def _storm_qualidade(
    pat: StormPattern | None, eden: dict[str, Any], ema_lenta_no_p3: float | None,
) -> dict[str, Any]:
    """Classificação perfeita/boa/**neutra**/ruim + o VETO, escrito.

    Opera **perfeita**, **boa** e — com aviso — **neutra**. As regras, na ordem:

    0. **ZONA NEUTRA (o candle entre a MME 8 e a MME 80)**: o terceiro estado do
       Éden. A favor da tendência das médias → ``neutra``: **opera**, com o setup
       valendo MENOS e o aviso escrito ("operar aqui é muito mais perigoso"). Contra
       ela → é a ARMADILHA que a spec nomeia, e aí sim veta. O mesmo lugar do
       gráfico significa coisas opostas conforme a direção do padrão, e é por isso
       que a decisão mora aqui e não no ``_eden``.
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
    # ZONA NEUTRA — o terceiro estado, entre o alinhado e o desalinhado. Aqui o
    # mesmo lugar do gráfico significa coisas OPOSTAS conforme a direção do padrão:
    # com a tendência das médias é um recuo comprável; contra ela é o repique que a
    # spec nomeia como ARMADILHA. Por isso a decisão mora aqui e não no `_eden`, que
    # não conhece o padrão.
    if eden.get("zona_neutra"):
        estrutural = eden.get("direcao_estrutural")
        if estrutural is not None and pat.direction != estrutural:
            contra = ("repique dentro de tendência de baixa" if estrutural == "venda"
                      else "recuo dentro de tendência de alta")
            return {
                "qualidade": "ruim", "motivo": eden.get("motivo") or "zona neutra",
                "opera": False,
                "veto": (f"ARMADILHA na zona neutra: padrão de {pat.direction} com as "
                         f"médias de {estrutural} — é {contra}, não reversão"),
            }
        return {
            "qualidade": "neutra", "opera": True, "veto": None,
            "motivo": ("ZONA NEUTRA (entre a MME 8 e a MME 80): a estrutura existe e vai "
                       f"a favor das médias ({estrutural or 'indefinida'}), mas operar "
                       "aqui é muito mais perigoso — o setup vale MENOS e exige "
                       "seletividade extra. Não é veto; é aviso."),
        }
    # O NOME DO ESTADO vem do vocabulário único (:data:`_EDEN_ROTULO`), nunca escrito à
    # mão aqui: era isto que fazia a mesma leitura sair como "sem Éden" no veto e como
    # "ARMADILHA" no motivo, na mesma tela.
    nome = _eden_nome_curto(eden)
    if not eden.get("alinhado"):
        return {"qualidade": "ruim", "motivo": eden.get("motivo") or nome,
                "opera": False,
                "veto": f"{nome} — {eden.get('motivo') or 'não opera'}"}
    if eden.get("direcao") != pat.direction:
        do_padrao = "alta" if pat.direction == "compra" else "baixa"
        return {
            "qualidade": "ruim",
            "motivo": (f"o filtro está em {nome} e o padrão é de {pat.direction} "
                       f"(estrutura de {do_padrao})"),
            "opera": False,
            "veto": (f"padrão de {pat.direction} contra {nome} — operar contra o Éden é "
                     "o caso que a regra proíbe"),
        }
    compra = pat.direction != "venda"
    lado_certo = (
        ema_lenta_no_p3 is not None
        and (pat.p3["low"] > ema_lenta_no_p3 if compra else pat.p3["high"] < ema_lenta_no_p3)
    )
    if lado_certo:
        onde = "acima" if compra else "abaixo"
        return {"qualidade": "perfeita", "opera": True, "veto": None,
                "motivo": (f"{nome} e ponto 3 inteiro {onde} da MME {_STORM_EMA_LENTA} — a "
                           "tendência principal sustenta a reversão")}
    onde = "acima" if compra else "abaixo"
    return {"qualidade": "boa", "opera": True, "veto": None,
            "motivo": (f"estrutura válida sob {nome}, mas o ponto 3 não está inteiro "
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
        # Invalidação e stop são COMUNS às duas entradas (mesmo padrão, mesmo ponto
        # 2); alvo e R:R vivem DENTRO de cada leitura, porque mudam com o gatilho.
        "invalidation": None, "stop": None, "leituras": [],
        **qual,
    }
    if pat is not None and price is not None:
        inval, stop, leituras = _storm_levels(pat, _atr(df), price)
        out.update({"invalidation": inval, "stop": stop, "leituras": leituras})
        # MORTE do Storm, medida na mesma régua do outro detector: a primeira barra
        # após o ponto 3 que FECHA além do ponto 2 (o fundo que a reversão declarou).
        # Sem isto, um Storm morto continuava desenhado com a cor de um vivo.
        compra = pat.direction != "venda"
        nivel = float((inval or {}).get("price") or 0.0)
        idx3 = df.index[df["Date"].dt.strftime(fmt) == pat.p3["date"]]
        em = (_primeira_barra_alem(df, int(idx3[-1]), nivel, compra, fmt)
              if len(idx3) and nivel else None)
        out["pattern"] = {**out["pattern"], "invalidado": em is not None,
                          "invalidado_em": em}
    # A faixa do ponto 3 do STORM é de outra natureza (o PRÓXIMO candle, não um swing
    # futuro): sai da sua própria regra, nunca da do outro método.
    nasce = pat is None or (out["pattern"] or {}).get("invalidado")
    out["projecao_p3"] = _projecao_storm(df, fmt) if nasce else None
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
                     "preco": None, **_eden_nomes("indisponivel"),
                     "motivo": "série indisponível para esta data/frame"},
            "pattern": None, "ema_lenta_no_p3": None, "projecao_p3": None,
            "invalidation": None, "stop": None, "leituras": [],
            "qualidade": None, "motivo": "sem dado para ler o Storm",
            "opera": False, "veto": None,
        }

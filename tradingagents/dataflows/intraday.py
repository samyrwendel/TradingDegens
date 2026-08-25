"""Keyless intraday OHLCV candles — 15m / 1h / 4h — for the fork's price structure.

The daily path (:func:`.stockstats_utils.load_ohlcv`, yfinance) is the whole
timeframe story upstream: the multi-timeframe read only *resamples that daily
series up* to weekly. The product owner, however, decides half the time on the
**intraday** frame — 15m, 1h and 4h — and the daily series cannot be resampled
*down* to reach it. This module adds those missing lower timeframes.

**Why 4h is pulled straight from the exchange (not resampled 1h→4h).** Binance
exposes ``4h`` as a *native* kline interval, so we fetch it directly: the candle
is then aligned to the exchange's own 4h boundaries (00:00/04:00/08:00… UTC) —
exactly the bar the product owner (and Erick) reads off Quantfury/TV. Resampling
1h→4h in memory would (a) depend on a deep-enough 1h history and (b) risk a
boundary/label mismatch against what the trader sees on the chart. A real native
bar has neither problem and stays honest to "the candle the exchange printed".

Scope and honesty rules (fork brief 24/08, extended 25/08 for equities):

* **Two keyless sources, one real candle each.** Crypto trades 24/7 and Binance
  exposes spot klines from a **keyless public REST endpoint** (the same posture the
  rest of the crypto path already takes — Hyperliquid/Binance/OKX, no key), so
  15m/1h/4h are all *native* exchange bars. An **equity** has no crypto-style feed
  but yfinance — the very source the daily path already uses — serves keyless
  intraday: 15m and 1h (``60m``) are *native*, and 4h is **resampled from the 1h
  series per trading session** (an equity is not 24/7: ~6.5h/day with overnight
  gaps, so bars are chunked *within a session* — never a fabricated overnight bar).
  When the source returns nothing (a backtest older than yfinance's ~60-day
  intraday window, or a transient outage) the loader raises
  :class:`IntradayUnavailableError` and the caller declares "intradiário
  indisponível" instead of inventing a bar.
* **No look-ahead.** For a backtest date (``curr_date`` < today) the fetch is
  clamped with Binance's ``endTime`` to the requested day's end **and** the frame
  is filtered to ``<= curr_date`` end-of-day afterward — so a 15m bar printed
  today can never leak into an analysis dated in the past.
* **Cache like the daily path (DA-058).** Historical intraday is immutable, so a
  past date is cached forever (one file per base+interval+day, zero network on
  the repeat); the current day is cached with a short TTL so a run started before
  the latest bar closed picks it up soon, without hammering the exchange.
* **Never fabricate.** An empty or failed fetch raises rather than returning a
  cosmetic frame.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from .config import get_config
from .errors import NoMarketDataError
from .symbol_utils import crypto_base, normalize_symbol
from .utils import safe_ticker_component

logger = logging.getLogger(__name__)

# Intraday timeframes we support, mapped to Binance's kline interval strings.
# Kept small on purpose: these are the frames the product owner actually trades —
# 15m for timing, 1h for the intraday trend, 4h for the swing read Erick decides
# on. All three are NATIVE Binance intervals, so each is a real exchange bar (no
# in-memory resampling — see the module docstring).
INTRADAY_INTERVALS: dict[str, str] = {"15m": "15m", "1h": "1h", "4h": "4h"}

_BINANCE_SPOT = "https://api.binance.com"
_TIMEOUT = 12
# Binance spot klines cap; ~10 days of 15m, ~41 days of 1h or ~166 days of 4h —
# deep enough for MMS200 / the swing scan on any supported lower timeframe.
_KLINES_LIMIT = 1000

# A current-day intraday cache older than this is refetched so a run picks up the
# freshly-closed bar; a past day is immutable and never refetched (mirrors the
# daily OHLCV cache TTL in stockstats_utils).
INTRADAY_CACHE_TTL_SECONDS = 300

# --- equity intraday (yfinance, keyless) -----------------------------------
# yfinance interval strings for the frames it serves natively. 4h is absent (see
# _resample_session_4h): it is derived from the 1h series per trading session.
_EQUITY_YF_INTERVAL: dict[str, str] = {"15m": "15m", "1h": "60m"}
# How far back to request per frame. Bounded by yfinance's own intraday windows
# (~60 days for sub-hour intervals, ~730 for 1h); we stay comfortably under the
# 15m cap while still fetching enough bars for MMS200 / the swing scan.
_EQUITY_LOOKBACK_DAYS: dict[str, int] = {"15m": 45, "1h": 120}
# Native 1h bars per resampled 4h equity bar (chunked within a session).
_SESSION_4H_SPAN = 4


class IntradayUnavailableError(NoMarketDataError):
    """No keyless intraday candle exists for this symbol/timeframe.

    Subclasses :class:`NoMarketDataError` so the routing/coverage layers keep
    treating it as "no usable data" (degrade, never fabricate), while callers
    that want the specific "intradiário indisponível" wording can catch this
    narrower type. Raised when the keyless source has no candle for the
    symbol/date — a crypto pair the exchange does not list, or an equity request
    outside yfinance's intraday window / during a transient outage.
    """


def _parse_date(curr_date) -> datetime:
    """Parse ``curr_date`` (yyyy-mm-dd or datetime) to a UTC-midnight datetime."""
    if isinstance(curr_date, datetime):
        dt = curr_date
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return datetime.strptime(str(curr_date)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _end_of_day_ms(as_of: datetime) -> int:
    """Epoch-ms for 23:59:59.999 UTC on ``as_of`` — the inclusive window end."""
    eod = as_of.replace(hour=23, minute=59, second=59, microsecond=999000)
    return int(eod.timestamp() * 1000)


def _binance_klines(base: str, interval: str, end_ms: int, limit: int = _KLINES_LIMIT) -> list:
    """Raw keyless Binance spot klines for ``{base}USDT`` ending at ``end_ms``.

    Isolated as the single network seam so tests can monkeypatch it (and so the
    caching layer wraps exactly one call). Returns the raw Binance array; parsing
    lives in :func:`_klines_to_df`.
    """
    sym = f"{base}USDT"
    r = requests.get(
        f"{_BINANCE_SPOT}/api/v3/klines",
        params={"symbol": sym, "interval": interval, "endTime": end_ms, "limit": limit},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def _klines_to_df(rows: list) -> pd.DataFrame:
    """Turn Binance's kline array into an OHLCV frame with a UTC ``Date`` column.

    Binance rows are ``[openTime, open, high, low, close, volume, closeTime, ...]``
    with numbers as strings; the open time (ms) becomes ``Date``.
    """
    recs = []
    for k in rows:
        recs.append(
            {
                "Date": datetime.fromtimestamp(int(k[0]) / 1000, tz=timezone.utc).replace(tzinfo=None),
                "Open": float(k[1]),
                "High": float(k[2]),
                "Low": float(k[3]),
                "Close": float(k[4]),
                "Volume": float(k[5]),
            }
        )
    return pd.DataFrame.from_records(recs, columns=["Date", "Open", "High", "Low", "Close", "Volume"])


def _cache_path(base: str, interval: str, day_key: str) -> str:
    config = get_config()
    cache_dir = config["data_cache_dir"]
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, f"{base}-BINANCE-{interval}-{day_key}.csv")


def _cache_is_fresh(cache_file: str, is_live: bool) -> bool:
    """Whether a cached intraday file may be served without refetching.

    Past days are immutable (always fresh); the current day is fresh only within
    the TTL so a later run picks up the just-closed bar (#1150 posture)."""
    if not os.path.exists(cache_file):
        return False
    if not is_live:
        return True
    return time.time() - os.path.getmtime(cache_file) <= INTRADAY_CACHE_TTL_SECONDS


def _clean_and_date_guard(
    data: pd.DataFrame, as_of: datetime, is_live: bool
) -> pd.DataFrame:
    """Coerce dtypes and (for a backtest) drop any bar past the requested day.

    Shared by both intraday sources so the anti-look-ahead guard is identical for
    crypto and equity: even if a stale "live" cache leaked into a backtest key, no
    bar dated after ``as_of`` end-of-day survives.
    """
    data = data.copy()
    data["Date"] = pd.to_datetime(data["Date"], errors="coerce")
    data = data.dropna(subset=["Date"])
    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col in data.columns:
            data[col] = pd.to_numeric(data[col], errors="coerce")
    data = data.dropna(subset=["Close"]).reset_index(drop=True)
    if not is_live:
        cutoff = pd.Timestamp(as_of.replace(tzinfo=None)).normalize() + pd.Timedelta(
            hours=23, minutes=59, seconds=59
        )
        data = data[data["Date"] <= cutoff].reset_index(drop=True)
    return data


def load_intraday_ohlcv(symbol: str, curr_date: str, interval: str = "15m") -> pd.DataFrame:
    """Real intraday OHLCV (15m/1h/4h), cached and date-guarded.

    Routes by asset: a **crypto** symbol reads native Binance klines; anything else
    is treated as an **equity** and reads yfinance intraday (15m/1h native, 4h
    resampled per session). Either way:

    * ``curr_date`` in the past -> the frame is clamped to that day's end, so
      today's bar cannot leak backward.
    * The repeat of a historical request is served from disk with zero network.
    * An empty/failed source raises :class:`IntradayUnavailableError` (equity) or
      :class:`NoMarketDataError` (crypto) — a bar is never fabricated.
    """
    if interval not in INTRADAY_INTERVALS:
        raise ValueError(
            f"unsupported intraday interval {interval!r}; expected one of "
            f"{sorted(INTRADAY_INTERVALS)}"
        )
    base = crypto_base(symbol)
    if base:
        return _load_crypto_intraday(symbol, base, curr_date, interval)
    return _load_equity_intraday(symbol, curr_date, interval)


def _load_crypto_intraday(
    symbol: str, base: str, curr_date: str, interval: str
) -> pd.DataFrame:
    """Native Binance-kline intraday for a crypto symbol (the original path)."""
    binance_interval = INTRADAY_INTERVALS[interval]
    as_of = _parse_date(curr_date)
    today = datetime.now(timezone.utc).date()
    is_live = as_of.date() >= today
    # For a live run read up to now; for a backtest read up to the requested day's
    # end so the exchange never even returns a posterior bar.
    if is_live:
        end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    else:
        end_ms = _end_of_day_ms(as_of)

    day_key = "live" if is_live else as_of.date().isoformat()
    cache_file = _cache_path(base, binance_interval, day_key)

    data: pd.DataFrame | None = None
    if _cache_is_fresh(cache_file, is_live):
        cached = pd.read_csv(cache_file, on_bad_lines="skip", encoding="utf-8")
        if not cached.empty and "Close" in cached.columns:
            data = cached

    if data is None:
        rows = _binance_klines(base, binance_interval, end_ms)
        df = _klines_to_df(rows)
        if df.empty or "Close" not in df.columns:
            raise NoMarketDataError(
                symbol, f"{base}USDT", f"Binance returned no {interval} klines"
            )
        df.to_csv(cache_file, index=False, encoding="utf-8")
        data = df

    data = _clean_and_date_guard(data, as_of, is_live)
    if data.empty:
        raise NoMarketDataError(
            symbol, f"{base}USDT",
            f"no {interval} candle on/before {as_of.date()}",
        )
    return data


# --------------------------------------------------------- equity intraday -----
def _equity_cache_path(safe_symbol: str, yf_interval: str, day_key: str) -> str:
    config = get_config()
    cache_dir = config["data_cache_dir"]
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, f"{safe_symbol}-YFin-intraday-{yf_interval}-{day_key}.csv")


def _yf_intraday_download(canonical: str, yf_interval: str, start: str, end: str):
    """Raw keyless yfinance intraday download — the single network seam for equities.

    Isolated so tests monkeypatch exactly one call (mirrors ``_binance_klines`` for
    crypto). ``end`` is exclusive, like the daily path, so the caller requests the
    day AFTER ``curr_date`` and the in-memory guard trims the rest.
    """
    import yfinance as yf

    from .stockstats_utils import yf_retry

    return yf_retry(lambda: yf.download(
        canonical,
        interval=yf_interval,
        start=start,
        end=end,
        multi_level_index=False,
        progress=False,
        auto_adjust=True,
    ))


def _yf_intraday_to_df(downloaded) -> pd.DataFrame:
    """Normalise a yfinance intraday frame to the same naive-UTC OHLCV shape the
    crypto path produces (so the detector reads both identically).

    yfinance keeps the timestamp in the index (named ``Datetime`` for intraday) as
    a *tz-aware* series in the exchange's timezone; it is converted to UTC and made
    naive, matching the crypto candles (which are naive UTC). A US session then maps
    cleanly onto a single UTC calendar day, so the date guard and per-day 4h
    chunking stay correct.
    """
    empty = pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    if downloaded is None or getattr(downloaded, "empty", True):
        return empty
    df = downloaded.reset_index()
    # The first column is the datetime index (``Datetime`` intraday / ``Date`` daily).
    dt_col = df.columns[0]
    df = df.rename(columns={dt_col: "Date"})
    dates = pd.to_datetime(df["Date"], errors="coerce", utc=True)
    df["Date"] = dates.dt.tz_localize(None)
    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col not in df.columns:
            df[col] = pd.NA
    return df[["Date", "Open", "High", "Low", "Close", "Volume"]]


def _resample_session_4h(hourly: pd.DataFrame) -> pd.DataFrame:
    """Resample a date-guarded 1h equity frame to 4h bars, per trading session.

    An equity is not 24/7: a naive ``resample("4h")`` would straddle the overnight
    gap and fabricate empty bars. Instead the 1h bars are grouped by calendar day
    (their session) and chunked into runs of :data:`_SESSION_4H_SPAN` consecutive
    bars — so each 4h bar aggregates only real, contiguous session hours and no
    overnight bar is ever invented. A short session degrades honestly (its last
    chunk is just the hours that traded), never padded.
    """
    d = hourly.copy()
    d["Date"] = pd.to_datetime(d["Date"], errors="coerce")
    d = d.dropna(subset=["Date"]).sort_values("Date").reset_index(drop=True)
    out: list[dict] = []
    for _day, grp in d.groupby(d["Date"].dt.date, sort=True):
        grp = grp.reset_index(drop=True)
        for i in range(0, len(grp), _SESSION_4H_SPAN):
            chunk = grp.iloc[i:i + _SESSION_4H_SPAN]
            out.append({
                "Date": chunk["Date"].iloc[0],
                "Open": float(chunk["Open"].iloc[0]),
                "High": float(chunk["High"].max()),
                "Low": float(chunk["Low"].min()),
                "Close": float(chunk["Close"].iloc[-1]),
                "Volume": float(chunk["Volume"].sum()) if "Volume" in chunk else 0.0,
            })
    return pd.DataFrame.from_records(
        out, columns=["Date", "Open", "High", "Low", "Close", "Volume"]
    )


def _load_equity_intraday(symbol: str, curr_date: str, interval: str) -> pd.DataFrame:
    """Keyless yfinance intraday for an equity, cached and date-guarded.

    15m and 1h are native yfinance intervals; 4h is derived from the (already
    cached + date-guarded) 1h series via :func:`_resample_session_4h`, so it costs
    no extra network and inherits the same anti-look-ahead guard — mirroring how the
    weekly frame is resampled from the daily cache upstream.
    """
    as_of = _parse_date(curr_date)
    today = datetime.now(timezone.utc).date()
    is_live = as_of.date() >= today

    if interval == "4h":
        hourly = _load_equity_intraday(symbol, curr_date, "1h")
        four_h = _resample_session_4h(hourly)
        if four_h.empty:
            raise IntradayUnavailableError(
                symbol, normalize_symbol(symbol),
                f"intradiário 4h indisponível para {symbol}: sem série 1h para reamostrar",
            )
        return four_h

    canonical = normalize_symbol(symbol)
    safe_symbol = safe_ticker_component(canonical)
    yf_interval = _EQUITY_YF_INTERVAL[interval]
    lookback = _EQUITY_LOOKBACK_DAYS[interval]

    # end is EXCLUSIVE (yfinance) — request the day after the anchor so its bars are
    # included; look-ahead is still cut by _clean_and_date_guard below. For a live
    # run the anchor is today; for a backtest it is curr_date.
    anchor = today if is_live else as_of.date()
    end_dt = anchor + timedelta(days=1)
    start_dt = end_dt - timedelta(days=lookback + 1)

    day_key = "live" if is_live else as_of.date().isoformat()
    cache_file = _equity_cache_path(safe_symbol, yf_interval, day_key)

    data: pd.DataFrame | None = None
    if _cache_is_fresh(cache_file, is_live):
        cached = pd.read_csv(cache_file, on_bad_lines="skip", encoding="utf-8")
        if not cached.empty and "Close" in cached.columns:
            data = cached

    if data is None:
        try:
            downloaded = _yf_intraday_download(
                canonical, yf_interval, start_dt.isoformat(), end_dt.isoformat()
            )
        except Exception as exc:  # noqa: BLE001 — any source failure is "unavailable"
            raise IntradayUnavailableError(
                symbol, canonical,
                f"intradiário {interval} indisponível para {symbol}: {type(exc).__name__}",
            ) from exc
        df = _yf_intraday_to_df(downloaded)
        if df.empty or df["Close"].dropna().empty:
            raise IntradayUnavailableError(
                symbol, canonical,
                f"intradiário {interval} indisponível para {symbol}: a fonte não "
                "retornou candles (data fora da janela intradiária ou fonte fora do ar)",
            )
        df.to_csv(cache_file, index=False, encoding="utf-8")
        data = df

    data = _clean_and_date_guard(data, as_of, is_live)
    if data.empty:
        raise IntradayUnavailableError(
            symbol, canonical,
            f"intradiário {interval} sem candle em/antes de {as_of.date()} para {symbol}",
        )
    return data

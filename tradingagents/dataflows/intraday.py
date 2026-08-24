"""Keyless intraday OHLCV candles — 15m / 1h — for the fork's price structure.

The daily path (:func:`.stockstats_utils.load_ohlcv`, yfinance) is the whole
timeframe story upstream: the multi-timeframe read only *resamples that daily
series up* to weekly. The product owner, however, decides half the time on the
**intraday** frame — 15m and 1h — and the daily series cannot be resampled *down*
to reach it. This module adds that missing lower timeframe.

Scope and honesty rules (fork brief 24/08):

* **Crypto only, real exchange candle.** Crypto trades 24/7 and Binance exposes
  spot klines from a **keyless public REST endpoint** — the same posture the rest
  of the crypto path already takes (Hyperliquid/Binance/OKX, no key). An equity
  has no comparable keyless intraday feed, so a non-crypto symbol raises
  :class:`IntradayUnavailableError` and the caller declares "intradiário
  indisponível para ação" instead of inventing a bar.
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
from datetime import datetime, timezone

import pandas as pd
import requests

from .config import get_config
from .errors import NoMarketDataError
from .symbol_utils import crypto_base

logger = logging.getLogger(__name__)

# Intraday timeframes we support, mapped to Binance's kline interval strings.
# Kept small on purpose: these are the two frames the product owner actually
# trades (15m for timing, 1h for the intraday trend).
INTRADAY_INTERVALS: dict[str, str] = {"15m": "15m", "1h": "1h"}

_BINANCE_SPOT = "https://api.binance.com"
_TIMEOUT = 12
# Binance spot klines cap; ~10 days of 15m or ~41 days of 1h — deep enough for
# MMS200 / the swing scan on the lower timeframe.
_KLINES_LIMIT = 1000

# A current-day intraday cache older than this is refetched so a run picks up the
# freshly-closed bar; a past day is immutable and never refetched (mirrors the
# daily OHLCV cache TTL in stockstats_utils).
INTRADAY_CACHE_TTL_SECONDS = 300


class IntradayUnavailableError(NoMarketDataError):
    """No keyless intraday candle exists for this symbol/timeframe.

    Subclasses :class:`NoMarketDataError` so the routing/coverage layers keep
    treating it as "no usable data" (degrade, never fabricate), while callers
    that want the specific "intradiário indisponível para ação" wording can catch
    this narrower type.
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


def load_intraday_ohlcv(symbol: str, curr_date: str, interval: str = "15m") -> pd.DataFrame:
    """Real intraday OHLCV (15m/1h) for a crypto symbol, cached and date-guarded.

    * Non-crypto symbol -> :class:`IntradayUnavailableError` (no keyless intraday
      candle for equities), so the caller declares it unavailable, never invents.
    * ``curr_date`` in the past -> the frame is clamped to that day's end (Binance
      ``endTime`` + a defensive filter), so today's bar cannot leak backward.
    * The repeat of a historical request is served from disk with zero network.
    """
    if interval not in INTRADAY_INTERVALS:
        raise ValueError(
            f"unsupported intraday interval {interval!r}; expected one of "
            f"{sorted(INTRADAY_INTERVALS)}"
        )
    base = crypto_base(symbol)
    if not base:
        raise IntradayUnavailableError(
            symbol,
            None,
            f"intradiário {interval} indisponível: não há candle intradiário "
            "keyless para ativo não-cripto",
        )

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

    data = data.copy()
    data["Date"] = pd.to_datetime(data["Date"], errors="coerce")
    data = data.dropna(subset=["Date"])
    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col in data.columns:
            data[col] = pd.to_numeric(data[col], errors="coerce")
    data = data.dropna(subset=["Close"]).reset_index(drop=True)

    # Date guard: never let a bar past the requested day's end through, even if a
    # stale "live" cache file was reused for a backtest key edge case.
    if not is_live:
        cutoff = pd.Timestamp(as_of.replace(tzinfo=None)).normalize() + pd.Timedelta(
            hours=23, minutes=59, seconds=59
        )
        data = data[data["Date"] <= cutoff].reset_index(drop=True)

    if data.empty:
        raise NoMarketDataError(
            symbol, f"{base}USDT",
            f"no {interval} candle on/before {as_of.date()}",
        )
    return data

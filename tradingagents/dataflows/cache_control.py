"""Drop the price cache entries that a "quero o dado atualizado" demands.

The market-data cache exists so a run doesn't re-download five years of candles on
every stage. That is right for a normal run and wrong for the one action where the
user is explicitly asking for FRESH data: the per-stage "atualizar" of the web UI
(task 002 / DA-062), which re-runs a finished stage precisely because the numbers
may have moved since it ran.

Only the entries that can actually go stale are removed:

* the 5-year OHLCV frame (its same-day rows are governed by a TTL),
* the ``live`` intraday files, and
* the intraday file keyed to TODAY.

Past-day intraday files are immutable market history — deleting them would buy
nothing and spend an API call. News / sentiment / fundamentals have no disk cache
at all: re-running those stages already hits the provider live.
"""

from __future__ import annotations

import contextlib
import os
from datetime import datetime
from pathlib import Path

from tradingagents.dataflows.utils import safe_ticker_component


def _prefixes(ticker: str) -> set[str]:
    """Filename prefixes a ticker's cache entries may carry.

    Equity files key on the Yahoo symbol (``AAPL-YFin-…``); crypto intraday keys on
    the base asset of the pair (``BTC-USD`` → ``BTC-BINANCE-…``). Both are covered,
    each validated so a crafted ticker can never widen the glob past the cache dir.
    """
    out: set[str] = set()
    for candidate in (ticker, ticker.split("-")[0]):
        value = (candidate or "").strip().upper()
        if not value:
            continue
        with contextlib.suppress(ValueError):
            out.add(safe_ticker_component(value))
    return out


def _is_volatile(name: str, today: str) -> bool:
    """Whether a cache filename holds data that a refresh must re-fetch."""
    return (
        name.endswith("-live.csv")
        or name.endswith(f"-{today}.csv")
        or name.endswith("-YFin-5y.csv")
        or "-YFin-data-" in name
    )


def invalidate_price_cache(cache_dir: str | os.PathLike, ticker: str,
                           today: str | None = None) -> list[str]:
    """Remove the stale-able price cache files for ``ticker``; return their names.

    Fails soft on every filesystem error — a refresh that cannot clear the cache
    still re-runs the stage, it just may read a cached candle. Never raises.
    """
    base = Path(cache_dir)
    if not base.is_dir():
        return []
    today = today or datetime.now().strftime("%Y-%m-%d")
    prefixes = _prefixes(ticker)
    if not prefixes:
        return []
    removed: list[str] = []
    try:
        names = sorted(p.name for p in base.iterdir() if p.is_file())
    except OSError:
        return []
    for name in names:
        if not any(name.startswith(prefix + "-") for prefix in prefixes):
            continue
        if not _is_volatile(name, today):
            continue
        try:
            (base / name).unlink()
            removed.append(name)
        except OSError:
            continue
    return removed

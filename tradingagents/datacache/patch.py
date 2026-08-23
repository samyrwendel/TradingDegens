"""Monkeypatches that route every TradingAgents data fetch through the cache.

Three seams, one principle:

1. ``interface.VENDOR_METHODS`` — the single funnel for news, macro, fundamentals,
   indicators, price, global news, insider and prediction markets. Wrapping the
   dict entries is enough: ``route_to_vendor`` reads the dict at call time, so no
   caller re-import needs patching.
2. ``reddit.fetch_reddit_posts`` / ``stocktwits.fetch_stocktwits_messages`` — the
   two social fetchers bypass the funnel and were called once per backtest date;
   they are keyed by (source, ticker, calendar-day) so a whole run reuses one
   fetch, and a 429/empty is negative-cached so it is not retried 44 times.
3. ``stockstats_utils.load_ohlcv`` — the price cache whose filename embedded the
   shifting today-based window, minting a fresh 5-year CSV every day. Replaced by
   a stable-per-symbol filename that keeps the 5y series and re-cuts the window
   in memory (upstream already did the re-cut; only the filename was wrong).

Everything is idempotent and fail-open: a patch that errors logs and leaves the
original behaviour intact. This is why the cache can hook the *third-party*
upstream modules even though it now lives in-tree — it rewrites them in memory at
import time, so an ``origin`` (upstream) pull that changes their source never
breaks the cache: it keeps hooking the same symbols.
"""
from __future__ import annotations

import functools
import glob
import importlib
import logging
import sys
from datetime import datetime

from . import cache

logger = logging.getLogger("ta_datacache")
_WRAP = "_ta_datacache_wrapped"


# ============================================================ route funnel ====
def _is_sentinel(val):
    return isinstance(val, str) and (
        val.startswith("NO_DATA_AVAILABLE") or val.startswith("DATA_UNAVAILABLE")
    )


def _remake(error):
    """Re-raise a stored failure as a generic error.

    ``route_to_vendor`` treats an arbitrary exception as "this vendor failed,
    try the next" — exactly the fallback we want on a repeat run, without any
    network. We deliberately do not resurrect the original typed exception
    (NoMarketDataError needs symbol/canonical we don't reconstruct); a generic
    error preserves the fallback path.
    """
    t = (error or {}).get("type", "error")
    m = (error or {}).get("msg", "")
    return RuntimeError(f"[ta_datacache cached failure: {t}] {m}")


def _wrap_route_impl(method, vendor, fn):
    if getattr(fn, _WRAP, False):
        return fn
    category = method

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        k = cache.key(method, vendor, args, kwargs)
        hit = cache.get(category, k)
        if hit is not None:
            if hit.get("kind") == "neg":
                cache.record_hit(category, negative=True)
                if hit.get("error"):
                    raise _remake(hit["error"])
                return hit.get("value")
            cache.record_hit(category)
            return hit.get("value")

        cache.record_net(category)
        try:
            val = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 — cache-and-reraise
            # Cache the failure briefly so a repeat run does not re-hit a dead
            # primary vendor, then propagate unchanged so the router's own
            # fallback/no-data handling is preserved.
            cache.set_neg(category, k, error={"type": type(exc).__name__, "msg": str(exc)})
            raise

        if _is_sentinel(val):
            cache.set_neg(category, k, value=val)
        else:
            permanent = cache.classify(args, kwargs) == "permanent"
            cache.set_ok(category, k, val, permanent)
        return val

    setattr(wrapper, _WRAP, True)
    return wrapper


def patch_interface(mod):
    vm = getattr(mod, "VENDOR_METHODS", None)
    if not isinstance(vm, dict):
        return
    for method, vendors in vm.items():
        if not isinstance(vendors, dict):
            continue
        for vendor, impl in list(vendors.items()):
            if isinstance(impl, list) and impl and callable(impl[0]):
                impl[0] = _wrap_route_impl(method, vendor, impl[0])
            elif callable(impl):
                vendors[vendor] = _wrap_route_impl(method, vendor, impl)
    logger.info("ta_datacache: interface.VENDOR_METHODS wrapped")


# ============================================================ social fetchers ==
def _social_is_empty(val):
    """True when a social fetch returned a placeholder rather than real posts."""
    if not isinstance(val, str):
        return True
    v = val.strip()
    head = v[:60].lower()
    return (
        v.startswith("<no ")
        or "unavailable" in head
        or "no reddit posts found" in head
        or "no stocktwits messages" in head
    )


def _wrap_social(name, fn):
    if getattr(fn, _WRAP, False):
        return fn

    @functools.wraps(fn)
    def wrapper(ticker, *args, **kwargs):
        day = datetime.now().strftime("%Y-%m-%d")
        k = cache.key(name, str(ticker).upper(), day)
        hit = cache.get(name, k)
        if hit is not None:
            cache.record_hit(name, negative=(hit.get("kind") == "neg"))
            return hit.get("value")

        cache.record_net(name)
        try:
            val = fn(ticker, *args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 — social fetchers degrade to text
            val = f"<{name} unavailable: {type(exc).__name__}>"
            cache.set_neg(name, k, value=val)
            return val

        if _social_is_empty(val):
            # 429 / empty: short TTL so we don't re-storm the same dead endpoint.
            cache.set_neg(name, k, value=val)
        else:
            # Live "last 7 days" data: reuse for the rest of the day, refresh
            # tomorrow.
            cache.set_ok(name, k, val, permanent=False)
        return val

    setattr(wrapper, _WRAP, True)
    return wrapper


def patch_reddit(mod):
    fn = getattr(mod, "fetch_reddit_posts", None)
    if callable(fn):
        mod.fetch_reddit_posts = _wrap_social("reddit", fn)


def patch_stocktwits(mod):
    fn = getattr(mod, "fetch_stocktwits_messages", None)
    if callable(fn):
        mod.fetch_stocktwits_messages = _wrap_social("stocktwits", fn)


def patch_sentiment_analyst(mod):
    """Re-point the names the analyst imported *by value* to the wrapped fetchers."""
    try:
        rd = importlib.import_module("tradingagents.dataflows.reddit")
        patch_reddit(rd)
        if hasattr(mod, "fetch_reddit_posts"):
            mod.fetch_reddit_posts = rd.fetch_reddit_posts
    except Exception as exc:  # noqa: BLE001
        logger.warning("ta_datacache: reddit re-point skipped: %s", exc)
    try:
        st = importlib.import_module("tradingagents.dataflows.stocktwits")
        patch_stocktwits(st)
        if hasattr(mod, "fetch_stocktwits_messages"):
            mod.fetch_stocktwits_messages = st.fetch_stocktwits_messages
    except Exception as exc:  # noqa: BLE001
        logger.warning("ta_datacache: stocktwits re-point skipped: %s", exc)


# ============================================================ price cache file ==
def _cleanup_dated_dupes(os_mod, cache_dir, safe_symbol):
    """Remove the old per-day duplicated 5y CSVs now superseded by the stable file."""
    try:
        pattern = os_mod.path.join(cache_dir, f"{safe_symbol}-YFin-data-*.csv")
        for p in glob.glob(pattern):
            try:
                os_mod.remove(p)
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass


def _make_stable_load_ohlcv(mod):
    orig = getattr(mod, "load_ohlcv")
    if getattr(orig, _WRAP, False):
        return orig
    pd = mod.pd
    os_mod = mod.os

    def load_ohlcv(symbol, curr_date):
        try:
            canonical = mod.normalize_symbol(symbol)
            safe_symbol = mod.safe_ticker_component(canonical)
            config = mod.get_config()
            curr_date_dt = pd.to_datetime(curr_date)

            today_date = pd.Timestamp.today()
            start_str = (today_date - pd.DateOffset(years=5)).strftime("%Y-%m-%d")
            end_str = (today_date + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

            os_mod.makedirs(config["data_cache_dir"], exist_ok=True)
            # STABLE filename — one file per symbol. The previous name embedded
            # {start}-{end} derived from today, so it changed daily and minted a
            # duplicate 5-year CSV every day. The window is re-cut in memory
            # below, so a per-day filename was never needed.
            data_file = os_mod.path.join(
                config["data_cache_dir"], f"{safe_symbol}-YFin-5y.csv"
            )

            data = None
            if os_mod.path.exists(data_file):
                cached = pd.read_csv(data_file, on_bad_lines="skip", encoding="utf-8")
                if (
                    not cached.empty
                    and "Close" in cached.columns
                    and not mod._needs_same_day_refresh(data_file, curr_date_dt, today_date)
                ):
                    data = cached

            if data is None:
                import yfinance as yf

                downloaded = mod.yf_retry(lambda: yf.download(
                    canonical,
                    start=start_str,
                    end=end_str,
                    multi_level_index=False,
                    progress=False,
                    auto_adjust=True,
                ))
                downloaded = mod._ensure_date_column(downloaded.reset_index())
                if downloaded.empty or "Close" not in downloaded.columns:
                    raise mod.NoMarketDataError(
                        symbol, canonical, "Yahoo Finance returned no rows"
                    )
                downloaded.to_csv(data_file, index=False, encoding="utf-8")
                data = downloaded
                _cleanup_dated_dupes(os_mod, config["data_cache_dir"], safe_symbol)

            data = mod._clean_dataframe(data)
            data = data[data["Date"] <= curr_date_dt]
            mod._assert_ohlcv_not_stale(data, curr_date, symbol, canonical)
            return data
        except mod.NoMarketDataError:
            raise
        except Exception as exc:  # noqa: BLE001 — never worse than upstream
            logger.warning(
                "ta_datacache: stable load_ohlcv fell back to original for %s: %s",
                symbol, exc,
            )
            return orig(symbol, curr_date)

    setattr(load_ohlcv, _WRAP, True)
    return load_ohlcv


def patch_stockstats_utils(mod):
    if getattr(getattr(mod, "load_ohlcv", None), _WRAP, False):
        return
    mod.load_ohlcv = _make_stable_load_ohlcv(mod)
    logger.info("ta_datacache: stockstats_utils.load_ohlcv stabilised")


def _repoint_load_ohlcv(mod):
    ssu = importlib.import_module("tradingagents.dataflows.stockstats_utils")
    patch_stockstats_utils(ssu)
    if hasattr(mod, "load_ohlcv"):
        mod.load_ohlcv = ssu.load_ohlcv


def patch_y_finance(mod):
    _repoint_load_ohlcv(mod)


def patch_market_data_validator(mod):
    _repoint_load_ohlcv(mod)


# ============================================================ registry ========
# module fullname -> patch function name in this module
TARGETS = {
    "tradingagents.dataflows.interface": patch_interface,
    "tradingagents.dataflows.reddit": patch_reddit,
    "tradingagents.dataflows.stocktwits": patch_stocktwits,
    "tradingagents.dataflows.stockstats_utils": patch_stockstats_utils,
    "tradingagents.dataflows.y_finance": patch_y_finance,
    "tradingagents.dataflows.market_data_validator": patch_market_data_validator,
    "tradingagents.agents.analysts.sentiment_analyst": patch_sentiment_analyst,
}


def apply(fullname, module):
    fn = TARGETS.get(fullname)
    if fn is None:
        return
    try:
        fn(module)
    except Exception as exc:  # noqa: BLE001 — fail open
        logger.warning("ta_datacache: patch for %s failed: %s", fullname, exc)


def apply_to_loaded():
    """Patch any target modules already imported before the hook installed."""
    for name in TARGETS:
        mod = sys.modules.get(name)
        if mod is not None:
            apply(name, mod)

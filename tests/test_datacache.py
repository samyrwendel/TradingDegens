"""Network-free acceptance tests for the in-repo data-governance cache (DA-058).

Ported from the original out-of-tree ``ta_datacache`` suite (21 checks) now that
the cache is first-class fork code at ``tradingagents.datacache`` and is activated
by ``import tradingagents``. Verifies the three seams are hooked, the immutability
classifier, zero-network repeats for historical data, the negative-cache that
kills a 429 storm, and the metrics render.
"""
import datetime

import pytest

import tradingagents  # noqa: F401 — importing activates the cache hook
import tradingagents.agents.analysts.sentiment_analyst as sa
import tradingagents.dataflows.interface as iface
import tradingagents.dataflows.reddit as reddit
import tradingagents.dataflows.stockstats_utils as ssu
import tradingagents.dataflows.stocktwits as stw
from tradingagents.datacache import cache, patch

WRAP = "_ta_datacache_wrapped"


@pytest.fixture()
def clean_cache(tmp_path, monkeypatch):
    """Point the cache at a fresh dir and zero the process-wide metrics."""
    monkeypatch.setattr(cache, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(cache, "NEG_TTL", 3600)
    monkeypatch.setattr(cache, "DISABLED", False)
    cache._metrics.clear()
    yield
    cache._metrics.clear()


# ---------------------------------------------------------------- activation --
@pytest.mark.unit
def test_all_three_seams_are_hooked():
    wrapped = [
        getattr(v, WRAP, False) if not isinstance(v, list) else getattr(v[0], WRAP, False)
        for m in iface.VENDOR_METHODS.values()
        for v in m.values()
    ]
    assert all(wrapped) and len(wrapped) > 10
    assert getattr(ssu.load_ohlcv, WRAP, False)
    assert getattr(reddit.fetch_reddit_posts, WRAP, False)
    assert getattr(stw.fetch_stocktwits_messages, WRAP, False)
    assert sa.fetch_reddit_posts is reddit.fetch_reddit_posts
    assert sa.fetch_stocktwits_messages is stw.fetch_stocktwits_messages


# ---------------------------------------------------------------- classify ----
@pytest.mark.unit
def test_classify_immutability_rule():
    today = datetime.date.today().strftime("%Y-%m-%d")
    assert cache.classify(("BE", "2021-08-22"), {}) == "permanent"
    assert cache.classify(("BE", today), {}) == "volatile"
    assert cache.classify(("BE",), {}) == "volatile"
    assert cache.classify(("BE", "2099-01-01"), {}) == "volatile"


# ---------------------------------------------------------------- caching -----
@pytest.mark.unit
def test_repeat_historical_call_is_zero_network(clean_cache, monkeypatch):
    net = {"n": 0}

    def fake_fund(ticker, curr_date):
        net["n"] += 1
        return f"FUNDAMENTALS {ticker} @ {curr_date}"

    monkeypatch.setitem(
        iface.VENDOR_METHODS["get_fundamentals"],
        "yfinance",
        patch._wrap_route_impl("get_fundamentals", "yfinance", fake_fund),
    )
    monkeypatch.setattr(iface, "get_vendor", lambda *a, **k: "yfinance")

    r1 = iface.route_to_vendor("get_fundamentals", "TESTX", "2021-01-05")
    r2 = iface.route_to_vendor("get_fundamentals", "TESTX", "2021-01-05")
    assert r1 == r2 and r1.startswith("FUNDAMENTALS TESTX")
    assert net["n"] == 1  # served from cache on the repeat

    m = cache.snapshot().get("get_fundamentals", {})
    assert m.get("net") == 1 and m.get("hit") == 1

    iface.route_to_vendor("get_fundamentals", "TESTX", "2020-06-06")
    assert net["n"] == 2  # a different historical date is a real miss


@pytest.mark.unit
def test_reddit_429_storm_is_killed(clean_cache):
    calls = {"n": 0}

    def fake_reddit(ticker):
        calls["n"] += 1
        return f"<no Reddit posts found mentioning {ticker.upper()} in the past 7 days>"

    wrapped = patch._wrap_social("reddit", fake_reddit)
    first = wrapped("BE")
    rest = [wrapped("BE") for _ in range(11)]  # simulate 12 backtest dates
    assert calls["n"] == 1
    assert all(o == first for o in rest)

    rm = cache.snapshot().get("reddit", {})
    assert rm.get("net") == 1 and rm.get("neg_hit") == 11


@pytest.mark.unit
def test_raised_failure_cached_and_reraised_without_network(clean_cache):
    rc = {"n": 0}

    def boom(ticker, start_date, end_date):
        rc["n"] += 1
        raise RuntimeError("vendor 429")

    w = patch._wrap_route_impl("get_news", "yfinance", boom)
    for _ in range(5):
        with pytest.raises(Exception):
            w("BE", "2021-01-01", "2021-01-07")
    assert rc["n"] == 1  # hit network once, then served from neg-cache


@pytest.mark.unit
def test_validation_error_is_not_negative_cached(clean_cache):
    """A deterministic validation error (bad indicator name) must NOT be
    negative-cached: it would never succeed on retry and the cached re-raise
    (RuntimeError) can abort a whole run for 3h. So the underlying fn is called
    every time — no stale cached failure repeated (task 014, criterion 4)."""
    rc = {"n": 0}

    def bad_indicator(symbol, indicator, curr_date, look_back_days=30):
        rc["n"] += 1
        raise ValueError("Indicator ema is not supported. Please choose from: [...]")

    w = patch._wrap_route_impl("get_indicators", "yfinance", bad_indicator)
    for _ in range(4):
        with pytest.raises(ValueError):     # propagates as the ORIGINAL ValueError, not a cached RuntimeError
            w("BTC-USD", "ema", "2026-08-22")
    assert rc["n"] == 4                     # never served from a negative cache


@pytest.mark.unit
def test_transient_error_still_negative_cached(clean_cache):
    """A genuine transient/vendor failure is still cached so a dead vendor is not
    hammered — the validation-error skip must not regress this (DA-058)."""
    rc = {"n": 0}

    def rate_limited(ticker, start_date, end_date):
        rc["n"] += 1
        raise RuntimeError("vendor 429")

    w = patch._wrap_route_impl("get_news", "yfinance", rate_limited)
    for _ in range(4):
        with pytest.raises(Exception):
            w("BE", "2021-01-01", "2021-01-07")
    assert rc["n"] == 1                     # hit once, then served from neg-cache


@pytest.mark.unit
def test_metrics_summary_renders(clean_cache):
    cache.record_net("get_news")
    cache.record_hit("get_news")
    txt = cache.summary_text()
    assert "net" in txt and "cache-hit" in txt

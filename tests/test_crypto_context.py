"""Crypto network-context vendors — on-chain, spot-ETF flow, Fear & Greed.

Fork brief part 3 (@ericksekiama corpus, confirmed by a second independent
modeling): three feeds an equity-shaped pipeline is blind to but the modeled
decision process treats as first-class for a crypto call. These tests mock the
HTTP layer so they never touch the network, and assert the acceptance the brief
demands: real sourced values on a live crypto run, nothing on a stock,
zero-network on a same-day repeat (cache), no look-ahead on a backtest date,
per-source degradation without fabrication, and the paid-key-only MVRV declared
rather than proxied.
"""
from datetime import date, datetime, timezone

import pytest

import tradingagents  # noqa: F401 — importing activates the datacache hook
from tradingagents.agents.utils import crypto_context_tools as cct
from tradingagents.agents.utils.crypto_context_coverage import (
    build_crypto_context,
    ensure_crypto_context_coverage,
    report_covers_crypto_context,
)
from tradingagents.agents.utils.date_guard import base_date
from tradingagents.datacache import cache
from tradingagents.dataflows import crypto_context as cc
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.interface import (
    OPTIONAL_CATEGORIES,
    VENDOR_METHODS,
    route_to_vendor,
)


# ------------------------------------------------------------- HTTP fixtures ---
def _utc_today() -> date:
    # The vendors decide live-vs-backtest against UTC now (matching the
    # derivatives vendor), so a "live" date in a test must be UTC's today, not
    # the local date — which can be a day behind UTC.
    return datetime.now(timezone.utc).date()


def _epoch(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def _fng_payload(readings):
    """readings: list of (date, value, classification), newest first."""
    return {
        "data": [
            {"value": str(v), "value_classification": c, "timestamp": str(_epoch(d))}
            for d, v, c in readings
        ],
        "metadata": {"error": None},
    }


def _global_payload():
    return {
        "data": {
            "market_cap_percentage": {"btc": 59.27, "eth": 11.24, "usdt": 7.01},
            "total_market_cap": {"usd": 2_606_042_024_458.19},
        }
    }


def _stablecoins_payload():
    return [
        {"symbol": "usdt", "market_cap": 183_210_792_036},
        {"symbol": "usdc", "market_cap": 73_560_381_329},
    ]


def _hashrate_payload():
    return {
        "currentHashrate": 885411665725113000000,
        "currentDifficulty": 125807076547197.5,
    }


def _farside_html(rows):
    """rows: list of (date_str, total_cell). Minimal Farside-shaped table."""
    body = "".join(
        f"<tr><td>{d}</td><td>10.0</td><td>{t}</td></tr>" for d, t in rows
    )
    return (
        "<html><body><table>"
        "<tr><th>Date</th><th>IBIT</th><th>Total</th></tr>"
        f"{body}"
        "<tr><td>Total</td><td>1,000</td><td>62,426</td></tr>"
        "</table></body></html>"
    )


@pytest.fixture
def live_http(monkeypatch):
    """Wire every keyless source to succeed for a live BTC run."""
    today = _utc_today()
    yday = date.fromordinal(today.toordinal() - 1)

    def fake_json(url, params=None):
        if "mining/hashrate" in url:
            return _hashrate_payload()
        if url.rstrip("/").endswith("/global"):
            return _global_payload()
        if "coins/markets" in url:
            return _stablecoins_payload()
        if "alternative.me" in url:
            return _fng_payload([(today, 73, "Greed"), (yday, 66, "Greed")])
        raise AssertionError(f"unexpected GET json {url}")

    def fake_text(url):
        if "blocks/tip/height" in url:
            return "963801"
        if "q/hashrate" in url:
            return "700000000"
        if "farside.co.uk/btc" in url:
            return _farside_html([
                (yday.strftime("%d %b %Y"), "128.3"),
                (today.strftime("%d %b %Y"), "244.4"),
            ])
        if "farside.co.uk/eth" in url:
            return _farside_html([(today.strftime("%d %b %Y"), "(53.6)")])
        raise AssertionError(f"unexpected GET text {url}")

    monkeypatch.setattr(cc, "_get_json", fake_json)
    monkeypatch.setattr(cc, "_get_text", fake_text)
    return {"today": today, "yday": yday}


# ------------------------------------------------------------- registration ----
@pytest.mark.unit
def test_methods_registered_and_route():
    for method, vendor in (
        ("get_onchain_metrics", "onchain_public"),
        ("get_etf_flows", "farside"),
        ("get_fear_greed", "alternative_me"),
    ):
        assert method in VENDOR_METHODS
        assert vendor in VENDOR_METHODS[method]


@pytest.mark.unit
def test_categories_are_optional_so_a_blip_never_aborts_the_run():
    # Enrichment, not a core decision input: a total outage must degrade to a
    # sentinel, never crash the analysis.
    assert {"onchain_data", "etf_flow_data", "crypto_sentiment"} <= OPTIONAL_CATEGORIES


# ----------------------------------------------------- acceptance #2: stocks ---
@pytest.mark.unit
@pytest.mark.parametrize("fn", [cc.get_onchain_metrics, cc.get_etf_flows, cc.get_fear_greed])
def test_non_crypto_symbol_raises(fn):
    with pytest.raises(NoMarketDataError):
        fn("AAPL", "2026-08-23")


@pytest.mark.unit
def test_stock_gets_no_context_sections():
    """AAPL: none of the three appear — the router returns 'no data', not a section."""
    out = build_crypto_context("AAPL", "2026-08-23")
    low = out.lower()
    assert "hashrate" not in low
    assert "farside" not in low
    assert "medo & ganância" not in low
    assert not report_covers_crypto_context(out)


# ------------------------------------------------ acceptance #1: live crypto ---
@pytest.mark.unit
def test_live_btc_shows_all_three_sections_with_real_values(live_http):
    out = build_crypto_context("BTC-USD", _utc_today().isoformat())
    # Three named sections.
    assert "On-chain" in out
    assert "Fluxo de ETF" in out
    assert "Medo & Ganância" in out
    # Real, verbatim API values — not prettied-up round numbers.
    assert "885411665725113000000" in out       # raw hashrate
    assert "125807076547197" in out             # raw difficulty
    assert "244.4" in out                        # exact ETF flow cell
    assert "73/100" in out                       # exact Fear & Greed reading
    # Named keyless sources.
    assert "mempool.space" in out
    assert "Farside" in out
    assert "alternative.me" in out
    assert report_covers_crypto_context(out)


@pytest.mark.unit
def test_dominance_and_stablecoin_cap_present(live_http):
    out = cc.get_onchain_metrics("BTC-USD", _utc_today().isoformat())
    assert "59.27%" in out                       # BTC dominance, exact
    assert "$183.21B" in out or "$256" in out or "stablecoin" in out.lower()
    assert "Cap total do mercado cripto" in out


# ------------------------------------- acceptance #6: paid-key metric declared -
@pytest.mark.unit
def test_mvrv_declared_unavailable_not_proxied(live_http):
    out = cc.get_onchain_metrics("BTC-USD", _utc_today().isoformat())
    assert "MVRV" in out
    low = out.lower()
    assert "indisponível" in low
    assert "glassnode" in low or "cryptoquant" in low
    # It is declared, not replaced by a number/proxy.
    assert "não estimado" in low or "nunca aproximados" in low


# --------------------------------------------- acceptance #7: pt-BR contract ---
@pytest.mark.unit
def test_ptbr_terms_lead_with_english_original(live_http):
    out = build_crypto_context("BTC-USD", _utc_today().isoformat())
    assert "(Fear & Greed" in out                # pt-BR term, English in parens
    assert "(spot-ETF flow" in out
    assert "dominância" in out.lower()
    # Fear & Greed classification localized.
    assert "Ganância" in out


# --------------------------------------------- acceptance #4: no look-ahead ----
@pytest.mark.unit
def test_fear_greed_backtest_picks_past_reading_not_today(monkeypatch):
    """A past date must read the reading dated on/before it, never a later one."""
    as_of = date(2026, 6, 1)
    requested = {}

    def fake_json(url, params=None):
        requested["limit"] = (params or {}).get("limit")
        # Newest-first history that straddles the backtest date.
        return _fng_payload([
            (date(2026, 8, 20), 90, "Extreme Greed"),   # AFTER as_of — must be ignored
            (as_of, 40, "Fear"),                          # the correct reading
            (date(2026, 5, 20), 55, "Neutral"),
        ])

    monkeypatch.setattr(cc, "_get_json", fake_json)
    out = cc.get_fear_greed("BTC-USD", as_of.isoformat())
    assert "40/100" in out and "Medo" in out
    assert "90/100" not in out                   # no future leak
    assert "2026-06-01" in out
    assert requested["limit"] and requested["limit"] > 2   # history window pulled


@pytest.mark.unit
def test_etf_flow_backtest_selects_row_on_or_before_date(monkeypatch):
    def fake_text(url):
        assert "farside.co.uk/btc" in url
        return _farside_html([
            ("28 May 2026", "111.1"),
            ("01 Jun 2026", "222.2"),      # the requested day
            ("05 Jun 2026", "999.9"),      # AFTER — must be ignored
        ])

    monkeypatch.setattr(cc, "_get_text", fake_text)
    out = cc.get_etf_flows("BTC-USD", "2026-06-01")
    assert "222.2" in out and "2026-06-01" in out
    assert "999.9" not in out                    # no look-ahead


@pytest.mark.unit
def test_onchain_backtest_declares_unavailable_without_network(monkeypatch):
    """Live-only keyless feeds have no history: a past date must NOT fetch."""
    def boom(*a, **k):
        raise AssertionError("on-chain must not hit the network for a past date")

    monkeypatch.setattr(cc, "_get_json", boom)
    monkeypatch.setattr(cc, "_get_text", boom)
    out = cc.get_onchain_metrics("BTC-USD", "2026-06-01")
    low = out.lower()
    assert "indisponível" in low
    assert "look-ahead" in low
    # MVRV still declared (paid-key), never fabricated.
    assert "MVRV" in out


# ----------------------------------------- acceptance #5: degrade, no fabricate -
@pytest.mark.unit
def test_every_source_down_degrades_named_without_fabrication(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(cc, "_get_json", boom)
    monkeypatch.setattr(cc, "_get_text", boom)

    onchain = cc.get_onchain_metrics("BTC-USD", _utc_today().isoformat())
    etf = cc.get_etf_flows("BTC-USD", _utc_today().isoformat())
    fng = cc.get_fear_greed("BTC-USD", _utc_today().isoformat())

    for out in (onchain, etf, fng):
        assert "indisponível" in out.lower()
    # Named sources survive the degrade.
    assert "mempool.space" in onchain or "blockchain.info" in onchain
    assert "Farside" in etf
    assert "alternative.me" in fng
    # No fabricated Fear & Greed score / ETF figure slipped in.
    assert "/100" not in fng
    assert "net inflow" not in etf and "net outflow" not in etf


@pytest.mark.unit
def test_eth_has_etf_flows_other_coins_declare_absence(monkeypatch):
    monkeypatch.setattr(cc, "_get_text", lambda url: _farside_html([("21 Aug 2026", "(53.6)")]))
    eth = cc.get_etf_flows("ETH-USD", "2026-08-23")
    assert "(53.6)" in eth
    # SOL has no spot ETF — declared absence, not a failure.
    sol = cc.get_etf_flows("SOL-USD", "2026-08-23")
    assert "não há etf spot" in sol.lower() or "não há etf" in sol.lower()


# ------------------------------------------------- acceptance #3: cache = 0 net -
@pytest.fixture()
def clean_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(cache, "DISABLED", False)
    cache._metrics.clear()
    yield
    cache._metrics.clear()


@pytest.mark.unit
def test_same_day_repeat_is_zero_network(clean_cache, live_http):
    """Second run the same day serves from cache — proven by the network counter."""
    calls = {"n": 0}
    inner_json = cc._get_json

    def counting_json(url, params=None):
        calls["n"] += 1
        return inner_json(url, params)

    inner_text = cc._get_text

    def counting_text(url):
        calls["n"] += 1
        return inner_text(url)

    import tradingagents.dataflows.crypto_context as ccmod
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ccmod, "_get_json", counting_json)
    monkeypatch.setattr(ccmod, "_get_text", counting_text)
    try:
        today = _utc_today().isoformat()
        r1 = route_to_vendor("get_fear_greed", "BTC-USD", today)
        after_first = calls["n"]
        assert after_first >= 1                    # first run hit the network
        r2 = route_to_vendor("get_fear_greed", "BTC-USD", today)
        assert calls["n"] == after_first           # second run: ZERO extra network
        assert r1 == r2

        m = cache.snapshot().get("get_fear_greed", {})
        assert m.get("net") == 1 and m.get("hit") == 1
    finally:
        monkeypatch.undo()


# --------------------------------------------------------- tool + coverage -----
@pytest.mark.unit
def test_tool_clamps_lookahead_curr_date(monkeypatch):
    """The @guard_dates wrapper clamps a future curr_date before assembling."""
    captured = {}
    monkeypatch.setattr(
        cct, "build_crypto_context",
        lambda symbol, curr_date: captured.update(curr_date=curr_date) or "ok",
    )
    with base_date("2026-06-01"):
        cct.get_crypto_context.invoke({"symbol": "BTC-USD", "curr_date": "2026-08-23"})
    assert captured["curr_date"] == "2026-06-01"


@pytest.mark.unit
def test_coverage_appends_when_missing_and_skips_when_present(live_http):
    today = _utc_today().isoformat()
    # A report that never mentions the signals gets the block appended.
    appended = ensure_crypto_context_coverage("Só preço e RSI.", "BTC-USD", today)
    assert report_covers_crypto_context(appended)
    assert "On-chain" in appended
    # A report that already carries them is returned unchanged (no duplicate).
    already = build_crypto_context("BTC-USD", today) + "\n\ntese."
    same = ensure_crypto_context_coverage(already, "BTC-USD", today)
    assert same == already

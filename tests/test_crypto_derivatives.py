"""Crypto derivatives vendor — real funding/OI/liquidations, guarded and sourced.

Fork brief part 2: crypto "support" upstream is just a yfinance ticker; this adds
the derivatives that actually move a perp (funding, open interest, liquidations)
from keyless public feeds. These tests mock the HTTP layer so they never touch
the network, and assert the four rules the brief demands: real sourced values,
per-source graceful degradation, no look-ahead on backtest dates, and no
fabrication.
"""
import pytest

from tradingagents.agents.utils import crypto_derivatives_tools as cdt
from tradingagents.agents.utils.date_guard import base_date
from tradingagents.dataflows import crypto
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.interface import VENDOR_METHODS, route_to_vendor


# --------------------------------------------------------------- HTTP mocks ----
def _hl_payload(base="BTC", funding="0.0000125", oi="36000", mark="77000"):
    return [
        {"universe": [{"name": base}]},
        [{"funding": funding, "openInterest": oi, "markPx": mark,
          "dayNtlVlm": "2000000000"}],
    ]


def _okx_liq_payload():
    return {
        "code": "0",
        "data": [
            {"details": [
                {"bkPx": "77000", "sz": "10", "posSide": "long",
                 "side": "sell", "ts": "1787515000000"},
                {"bkPx": "77100", "sz": "5", "posSide": "short",
                 "side": "buy", "ts": "1787516000000"},
            ]},
        ],
    }


def _okx_instruments_payload():
    return {"code": "0", "data": [{"ctVal": "0.01", "ctValCcy": "BTC"}]}


@pytest.fixture
def live_http(monkeypatch):
    """Wire Hyperliquid + OKX to succeed; Binance untouched (HL is primary live)."""
    monkeypatch.setattr(crypto, "_post_json", lambda url, payload: _hl_payload())

    def fake_get(url, params=None):
        if "liquidation-orders" in url:
            return _okx_liq_payload()
        if "instruments" in url:
            return _okx_instruments_payload()
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(crypto, "_get_json", fake_get)


# ------------------------------------------------------------- registration ----
@pytest.mark.unit
def test_method_registered_and_routes():
    assert "get_crypto_derivatives" in VENDOR_METHODS
    assert "crypto_exchanges" in VENDOR_METHODS["get_crypto_derivatives"]


@pytest.mark.unit
def test_non_crypto_symbol_raises():
    with pytest.raises(NoMarketDataError):
        crypto.get_crypto_derivatives("AAPL", "2026-08-23")


# ------------------------------------------------------------------ live -------
@pytest.mark.unit
def test_live_report_has_all_three_signals_and_sources(live_http):
    from datetime import date
    out = crypto.get_crypto_derivatives("BTC-USD", date.today().isoformat())
    low = out.lower()
    assert "funding" in low and "open interest" in low and "liquidation" in low
    # Named sources, not anonymous numbers.
    assert "Hyperliquid" in out
    assert "OKX" in out
    # Exact API values printed verbatim, not prettied-up round numbers.
    assert "0.0000125" in out      # raw funding rate
    assert "36000" in out          # raw open interest
    # Real computed liquidation total: 10*0.01*77000 + 5*0.01*77100 = 7700 + 3855
    assert "$11.6K" in out or "11,555" in out or "$11" in out


@pytest.mark.unit
def test_liquidations_split_long_vs_short(live_http):
    from datetime import date
    out = crypto.get_crypto_derivatives("BTC-USD", date.today().isoformat())
    assert "longs" in out and "shorts" in out


# ------------------------------------------------------- degradation -----------
@pytest.mark.unit
def test_source_failure_degrades_without_fabrication(monkeypatch):
    """Every source down → explicit 'unavailable', never an invented number."""
    from datetime import date

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(crypto, "_post_json", boom)
    monkeypatch.setattr(crypto, "_get_json", boom)
    out = crypto.get_crypto_derivatives("BTC-USD", date.today().isoformat())
    assert out.lower().count("unavailable") >= 3      # funding, OI, liquidations
    assert "no value reported" in out
    # No fabricated dollar figure slipped in.
    assert "$" not in out.split("_Sources")[0].replace("USDⓈ", "")


# --------------------------------------------------- no look-ahead -------------
@pytest.mark.unit
def test_backtest_uses_hl_history_not_live_and_clamps(monkeypatch):
    """A past-dated run must read HL funding *history* (clamped), never the live
    ``metaAndAssetCtxs`` 'now' snapshot — that would be look-ahead."""
    seen = {"types": []}
    day_end_2026_06_01 = 1_780_444_799_999   # ~2026-06-01T23:59:59Z

    def fake_post(url, payload):
        seen["types"].append(payload["type"])
        assert payload["type"] != "metaAndAssetCtxs", "live snapshot in a backtest!"
        if payload["type"] == "fundingHistory":
            assert payload["endTime"] <= day_end_2026_06_01   # clamped to the day
            return [{"coin": "BTC", "fundingRate": "0.0000125",
                     "time": 1_780_400_000_000}]
        raise AssertionError(f"unexpected type {payload['type']}")

    def fake_get(url, params=None):
        # OI hist / liquidations degrade for an old date.
        if "openInterestHist" in url:
            return []
        if "liquidation-orders" in url:
            return {"code": "0", "data": []}
        if "instruments" in url:
            return _okx_instruments_payload()
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(crypto, "_post_json", fake_post)
    monkeypatch.setattr(crypto, "_get_json", fake_get)

    out = crypto.get_crypto_derivatives("BTC-USD", "2026-06-01")
    assert "metaAndAssetCtxs" not in seen["types"]   # live snapshot never touched
    assert "fundingHistory" in seen["types"]         # HL historical funding used
    assert "Hyperliquid perp, 1h" in out             # HL is primary, not Binance
    assert "0.0000125" in out                        # exact API rate, not rounded
    assert "unavailable" in out.lower()              # OI + liquidations degrade


@pytest.mark.unit
def test_hl_unknown_coin_degrades_to_binance(monkeypatch):
    """A coin Hyperliquid doesn't list (null history) degrades to Binance, no crash."""
    def fake_post(url, payload):
        return None    # HL returns null for an unlisted coin

    def fake_get(url, params=None):
        if "fapi/v1/fundingRate" in url:
            return [{"fundingTime": 1_780_400_000_000, "fundingRate": "0.0002",
                     "markPrice": "5"}]
        if "openInterestHist" in url:
            return []
        if "liquidation-orders" in url:
            return {"code": "0", "data": []}
        if "instruments" in url:
            return {"code": "0", "data": [{"ctVal": "1"}]}
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(crypto, "_post_json", fake_post)
    monkeypatch.setattr(crypto, "_get_json", fake_get)
    out = crypto.get_crypto_derivatives("LINK-USD", "2026-06-01")
    assert "Binance perp fallback" in out            # HL missing → Binance took over
    assert "0.0002" in out


@pytest.mark.unit
def test_tool_clamps_curr_date(monkeypatch):
    """The @guard_dates wrapper clamps a look-ahead curr_date before routing."""
    captured = {}
    monkeypatch.setattr(
        cdt, "route_to_vendor",
        lambda method, *args, **kw: captured.update(args=args) or "ok",
    )
    with base_date("2026-06-01"):
        cdt.get_crypto_derivatives.invoke({"symbol": "BTC-USD", "curr_date": "2026-08-23"})
    assert captured["args"][1] == "2026-06-01"    # clamped down to the base date


@pytest.mark.unit
def test_route_dispatches_to_vendor(monkeypatch, live_http):
    from datetime import date
    out = route_to_vendor("get_crypto_derivatives", "BTC-USD", date.today().isoformat())
    assert "Crypto Derivatives" in out

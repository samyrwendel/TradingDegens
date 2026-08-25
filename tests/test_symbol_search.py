"""Keyless symbol search + name resolution (Yahoo search via yfinance).

Network-free: the single seam (:func:`_yahoo_search`) is monkeypatched, so these
pin the contract without hitting Yahoo:

* a symbol resolves to its display name; a name resolves to its symbol;
* a plain ticker is never sent to the network (``looks_like_symbol``) and never
  hijacked into a different symbol;
* results are cached (DA-058) and a transient outage never poisons the cache;
* every path is fail-open — a source hiccup yields ``None`` / ``[]``, never a crash
  and never an invented name.
"""
import pytest

from tradingagents.dataflows import symbol_search as ss


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    return tmp_path


def _q(symbol, short=None, long=None, qtype="EQUITY"):
    return {"symbol": symbol, "shortname": short, "longname": long, "quoteType": qtype}


# ------------------------------------------------------ looks_like_symbol ------
@pytest.mark.unit
@pytest.mark.parametrize("term,expected", [
    ("AAPL", True), ("BTC-USD", True), ("0700.HK", True), ("GC=F", True), ("MSFT", True),
    ("Microsoft", False), ("Bitcoin", False), ("Apple", False), ("apple", False),
    ("Johnson & Johnson", False), ("", False), ("   ", False),
])
def test_looks_like_symbol(term, expected):
    assert ss.looks_like_symbol(term) is expected


# ------------------------------------------------------------- search ----------
@pytest.mark.unit
def test_search_symbols_cleans_and_shapes(cache_dir, monkeypatch):
    monkeypatch.setattr(ss, "_yahoo_search", lambda term, count=8: [
        _q("MSFT", "Microsoft Corporation", "Microsoft Corporation"),
        _q("MSF.DE", "MICROSOFT CORP", "Microsoft Corporation"),
        _q("", "no symbol", "skip me"),        # no symbol -> dropped
        _q("NONAME", None, None),               # no name   -> dropped
    ])
    out = ss.search_symbols("Microsoft", limit=5)
    assert [r["symbol"] for r in out] == ["MSFT", "MSF.DE"]
    assert out[0] == {"symbol": "MSFT", "name": "Microsoft Corporation", "type": "EQUITY", "exchange": ""}


@pytest.mark.unit
def test_search_prefers_shortname(cache_dir, monkeypatch):
    monkeypatch.setattr(ss, "_yahoo_search",
                        lambda term, count=8: [_q("CDW", "CDW Corp", "CDW Corporation")])
    assert ss.search_symbols("CDW")[0]["name"] == "CDW Corp"


@pytest.mark.unit
def test_search_is_cached_zero_network_on_repeat(cache_dir, monkeypatch):
    calls = {"n": 0}

    def counting(term, count=8):
        calls["n"] += 1
        return [_q("MSFT", "Microsoft Corporation")]

    monkeypatch.setattr(ss, "_yahoo_search", counting)
    ss.search_symbols("Microsoft")
    ss.search_symbols("Microsoft")
    assert calls["n"] == 1, "repeat term must hit the cache"


@pytest.mark.unit
def test_search_empty_is_not_cached(cache_dir, monkeypatch):
    """A transient outage (empty result) must NOT be cached as a permanent blank."""
    calls = {"n": 0}

    def flaky(term, count=8):
        calls["n"] += 1
        return [] if calls["n"] == 1 else [_q("MSFT", "Microsoft Corporation")]

    monkeypatch.setattr(ss, "_yahoo_search", flaky)
    assert ss.search_symbols("Microsoft") == []       # 1st: outage
    assert ss.search_symbols("Microsoft")[0]["symbol"] == "MSFT"  # 2nd: recovered, refetched
    assert calls["n"] == 2


@pytest.mark.unit
def test_search_fail_open_on_exception(cache_dir, monkeypatch):
    def boom(term, count=8):
        raise RuntimeError("yahoo down")

    monkeypatch.setattr(ss, "_yahoo_search", boom)
    assert ss.search_symbols("Microsoft") == []


# --------------------------------------------------------- resolve_name --------
@pytest.mark.unit
def test_resolve_name_exact_symbol_match(cache_dir, monkeypatch):
    monkeypatch.setattr(ss, "_yahoo_search", lambda term, count=8: [
        _q("MSFT", "Microsoft Corporation"),
        _q("MSF.DE", "MICROSOFT CORP"),
    ])
    assert ss.resolve_name("MSFT") == "Microsoft Corporation"


@pytest.mark.unit
def test_resolve_name_normalizes_crypto(cache_dir, monkeypatch):
    # BTCUSD -> BTC-USD (normalize_symbol), matched against the returned symbol.
    monkeypatch.setattr(ss, "_yahoo_search",
                        lambda term, count=8: [_q("BTC-USD", "Bitcoin USD", qtype="CRYPTOCURRENCY")])
    assert ss.resolve_name("BTCUSD") == "Bitcoin USD"


@pytest.mark.unit
def test_resolve_name_none_when_no_exact_match(cache_dir, monkeypatch):
    monkeypatch.setattr(ss, "_yahoo_search",
                        lambda term, count=8: [_q("OTHER", "Some Other Co")])
    assert ss.resolve_name("MSFT") is None


@pytest.mark.unit
def test_resolve_name_cached(cache_dir, monkeypatch):
    calls = {"n": 0}

    def counting(term, count=8):
        calls["n"] += 1
        return [_q("MSFT", "Microsoft Corporation")]

    monkeypatch.setattr(ss, "_yahoo_search", counting)
    ss.resolve_name("MSFT")
    ss.resolve_name("MSFT")
    assert calls["n"] == 1


# ------------------------------------------------- resolve_query_to_symbol -----
@pytest.mark.unit
def test_resolve_query_name_to_symbol(cache_dir, monkeypatch):
    monkeypatch.setattr(ss, "_yahoo_search", lambda term, count=8: [
        _q("MSFT", "Microsoft Corporation"),
        _q("MSF.DE", "MICROSOFT CORP"),
    ])
    assert ss.resolve_query_to_symbol("Microsoft") == "MSFT"


@pytest.mark.unit
def test_resolve_query_exact_symbol_passthrough(cache_dir, monkeypatch):
    """A term that IS one of the returned symbols is returned as-is, not hijacked
    into a more 'relevant' top hit."""
    monkeypatch.setattr(ss, "_yahoo_search", lambda term, count=8: [
        _q("MSFT", "Microsoft Corporation"),   # top hit
        _q("AAOI", "Applied Optoelectronics"),  # the exact match, lower down
    ])
    assert ss.resolve_query_to_symbol("AAOI") == "AAOI"


@pytest.mark.unit
def test_resolve_query_none_when_no_results(cache_dir, monkeypatch):
    monkeypatch.setattr(ss, "_yahoo_search", lambda term, count=8: [])
    assert ss.resolve_query_to_symbol("zzzznotathing") is None

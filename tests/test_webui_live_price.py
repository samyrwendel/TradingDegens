"""Preço LIVE da watchlist (task 010): fetch leve + cache TTL no runner + endpoint.

Não bate na rede: o ``fetch_live_price`` é trocado por um fake, então checamos o
CACHE (só busca o que expirou), o passthrough de null (fonte caída → None → "—" na
UI) e a leitura resiliente do ``fast_info`` do yfinance (mapping OU atributo).
"""

import json
import threading
import urllib.request

import tradingagents.dataflows.live_price as live_price
from tradingagents.dataflows.live_price import _fast_get, _to_float, fetch_live_price
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.server import make_server
from tradingagents.webui.store import HistoryStore


def _runner(tmp_path):
    return AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "openai",
                     "deep_think_llm": "x", "quick_think_llm": "y"},
        store=HistoryStore(tmp_path))


def test_live_prices_caches_within_ttl(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(live_price, "fetch_live_price",
                        lambda s: (calls.append(s) or {"price": 1.0, "change_pct": None, "currency": "USD"}))
    runner = _runner(tmp_path)
    a = runner.live_prices(["MCD", "mcd", "BTC"])   # dedup case-insensitive
    b = runner.live_prices(["MCD", "BTC"])          # tudo do cache: sem nova busca
    assert set(a) == {"MCD", "BTC"}
    assert a == b
    assert sorted(calls) == ["BTC", "MCD"]          # cada símbolo buscado UMA vez


def test_live_prices_refetches_after_ttl(tmp_path, monkeypatch):
    import tradingagents.webui.runner as runner_mod
    calls = []
    monkeypatch.setattr(live_price, "fetch_live_price",
                        lambda s: (calls.append(s) or {"price": 2.0, "change_pct": 1.0, "currency": "USD"}))
    monkeypatch.setattr(runner_mod, "_PRICE_TTL", 0.0)   # tudo expira na hora
    runner = _runner(tmp_path)
    runner.live_prices(["MCD"])
    runner.live_prices(["MCD"])
    assert calls == ["MCD", "MCD"]                       # re-busca após TTL


def test_live_prices_passes_null_through(tmp_path, monkeypatch):
    monkeypatch.setattr(live_price, "fetch_live_price", lambda s: None)  # fonte caída
    runner = _runner(tmp_path)
    assert runner.live_prices(["NADA"]) == {"NADA": None}


def test_prices_endpoint_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(
        live_price, "fetch_live_price",
        lambda s: {"price": 267.12, "change_pct": -0.37, "currency": "USD"} if s == "MCD" else None)
    runner = _runner(tmp_path)
    httpd = make_server("127.0.0.1", 0, runner=runner)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        got = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/prices?tickers=MCD,BTC"))
    finally:
        httpd.shutdown()
    assert got == {"prices": {"MCD": {"price": 267.12, "change_pct": -0.37, "currency": "USD"},
                              "BTC": None}}


class _FastInfoAttr:
    last_price = 100.0
    previous_close = 80.0
    currency = "USD"


class _FastInfoMap:
    def __init__(self):
        self._d = {"last_price": 50.0, "previous_close": 50.0, "currency": "BRL"}

    def get(self, k):
        return self._d.get(k)


def test_fetch_live_price_reads_attr_and_mapping(monkeypatch):
    import types
    fake_yf = types.SimpleNamespace(Ticker=lambda sym: types.SimpleNamespace(fast_info=_FastInfoAttr()))
    monkeypatch.setitem(__import__("sys").modules, "yfinance", fake_yf)
    out = fetch_live_price("AAPL")
    assert out == {"price": 100.0, "change_pct": 25.0, "currency": "USD"}   # (100-80)/80

    fake_yf.Ticker = lambda sym: types.SimpleNamespace(fast_info=_FastInfoMap())
    out2 = fetch_live_price("PETR4.SA")
    assert out2 == {"price": 50.0, "change_pct": 0.0, "currency": "BRL"}    # prev==last → 0%


def test_fetch_live_price_failopen(monkeypatch):
    import types
    def boom(sym):
        raise RuntimeError("yahoo down")
    monkeypatch.setitem(__import__("sys").modules, "yfinance",
                        types.SimpleNamespace(Ticker=boom))
    assert fetch_live_price("AAPL") is None      # exceção da fonte → None, nunca sobe
    assert fetch_live_price("") is None          # vazio é no-op


def test_helpers_to_float_and_fast_get():
    assert _to_float("3.5") == 3.5
    assert _to_float(None) is None
    assert _to_float(float("nan")) is None
    assert _to_float("x") is None
    assert _fast_get(_FastInfoAttr(), "last_price") == 100.0
    assert _fast_get(_FastInfoMap(), "currency") == "BRL"
    assert _fast_get(_FastInfoAttr(), "nope", "currency") == "USD"   # cai no 2º nome

"""Preço LIVE: fetch leve + cache TTL no runner + endpoint + a SESSÃO declarada.

Não bate na rede: o ``fetch_live_price`` é trocado por um fake, então checamos o
CACHE (só busca o que expirou), o passthrough de null (fonte caída → None → "—" na
UI) e — o que a task 010 de UI trouxe — QUAL preço a cotação está devolvendo:
fechamento, pré-market e after-market são números DIFERENTES, e chamar qualquer um
deles de "agora" é mentira. A fonte passou a ser o ``info`` (tem ``marketState`` e
os preços de pré/pós) no lugar do ``fast_info`` (só o regular, e sem dizer que é).
"""

import json
import threading
import urllib.request

import tradingagents.dataflows.live_price as live_price
from tradingagents.dataflows.live_price import _to_float, fetch_live_price
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


def _fake_info(monkeypatch, info):
    """Troca o yfinance por um duplo que devolve ``info`` — sem rede."""
    import types
    monkeypatch.setitem(
        __import__("sys").modules, "yfinance",
        types.SimpleNamespace(Ticker=lambda sym: types.SimpleNamespace(info=dict(info))))


_BASE = {"currency": "USD", "quoteType": "EQUITY", "exchangeTimezoneName": "America/New_York",
         "regularMarketPrice": 513.53, "regularMarketPreviousClose": 505.06,
         "regularMarketTime": 1787947201}


def test_mercado_aberto_diz_que_e_agora(monkeypatch):
    _fake_info(monkeypatch, {**_BASE, "marketState": "REGULAR"})
    out = fetch_live_price("MSFT")
    assert out["price"] == 513.53
    assert out["sessao"] == "regular" and "agora" in out["rotulo"]
    assert out["change_pct"] == 1.68                      # (513.53-505.06)/505.06


def test_mercado_fechado_nao_chama_fechamento_de_cotacao_atual(monkeypatch):
    """O pedido do Samyr: com o mercado fechado a tela mostrava o fechamento como se
    fosse "agora". O número pode ser esse; o RÓTULO é que não podia mentir."""
    _fake_info(monkeypatch, {**_BASE, "marketState": "CLOSED", "postMarketPrice": 513.06})
    out = fetch_live_price("MSFT")
    assert out["price"] == 513.53
    assert out["sessao"] == "fechado" and out["rotulo"] == "último fechamento"


def test_after_market_mostra_o_preco_do_after(monkeypatch):
    """Pós-mercado com negócio: o preço É outro (513,06 × 513,53) e é ele que vale."""
    _fake_info(monkeypatch, {**_BASE, "marketState": "POST", "postMarketPrice": 513.06,
                             "postMarketTime": 1787961586})
    out = fetch_live_price("MSFT")
    assert out["price"] == 513.06
    assert out["regular_price"] == 513.53          # o regular viaja junto pra comparação
    assert out["sessao"] == "pos" and out["rotulo"] == "after-market"


def test_pre_market_sem_negocio_nao_finge_pre_market(monkeypatch):
    """A fonte diz PRE mas não manda preço de pré: o que existe é o fechamento — e é
    isso que se diz, em vez de carimbar "pré-market" num número que não é dele."""
    _fake_info(monkeypatch, {**_BASE, "marketState": "PRE"})
    out = fetch_live_price("MSFT")
    assert out["price"] == 513.53
    assert out["sessao"] == "fechado" and "pré-market sem negócio" in out["rotulo"]


def test_pre_market_com_negocio(monkeypatch):
    _fake_info(monkeypatch, {**_BASE, "marketState": "PRE", "preMarketPrice": 508.0,
                             "preMarketTime": 1787961586})
    out = fetch_live_price("MSFT")
    assert out["price"] == 508.0 and out["sessao"] == "pre"


def test_cripto_nao_tem_pregao(monkeypatch):
    _fake_info(monkeypatch, {"currency": "USD", "quoteType": "CRYPTOCURRENCY",
                             "exchangeTimezoneName": "UTC", "regularMarketPrice": 77990.13,
                             "regularMarketPreviousClose": 77843.0, "marketState": "REGULAR",
                             "regularMarketTime": 1787961586})
    out = fetch_live_price("BTC-USD")
    assert out["sessao"] == "24h" and "24h" in out["rotulo"]


def test_hora_do_numero_vem_no_fuso_da_BOLSA(monkeypatch):
    """A hora do servidor não diz nada sobre a sessão; a da bolsa, sim."""
    _fake_info(monkeypatch, {**_BASE, "marketState": "CLOSED"})
    out = fetch_live_price("MSFT")
    assert out["as_of"] and ":" in out["as_of"]
    assert out["fuso"] == "America/New_York"


def test_sessao_desconhecida_nao_inventa_rotulo(monkeypatch):
    _fake_info(monkeypatch, {**_BASE, "marketState": "SEI_LA"})
    out = fetch_live_price("MSFT")
    assert out["sessao"] == "desconhecida" and out["rotulo"] == "último preço conhecido"


def test_fetch_live_price_failopen(monkeypatch):
    import types
    def boom(sym):
        raise RuntimeError("yahoo down")
    monkeypatch.setitem(__import__("sys").modules, "yfinance",
                        types.SimpleNamespace(Ticker=boom))
    assert fetch_live_price("AAPL") is None      # exceção da fonte → None, nunca sobe
    assert fetch_live_price("") is None          # vazio é no-op


def test_sem_preco_nenhum_e_None(monkeypatch):
    _fake_info(monkeypatch, {"marketState": "CLOSED", "currency": "USD"})
    assert fetch_live_price("XPTO") is None


def test_helpers_to_float():
    assert _to_float("3.5") == 3.5
    assert _to_float(None) is None
    assert _to_float(float("nan")) is None
    assert _to_float("x") is None

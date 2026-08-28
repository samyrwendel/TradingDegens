"""Invalidação cirúrgica do cache de preço — o "dados frescos" do 🔄 (task 002).

Atualizar uma etapa só significa alguma coisa se o motor de fato re-busca o dado:
com o cache intacto, a etapa re-rodaria lendo o MESMO candle e o usuário receberia
de volta exatamente o que já tinha. Mas o oposto (limpar tudo) também é errado —
candle de dia fechado é imutável e re-baixá-lo só gasta chamada de API.
"""

from tradingagents.dataflows.cache_control import invalidate_price_cache

TODAY = "2026-08-27"


def _seed(tmp_path, *names):
    for name in names:
        (tmp_path / name).write_text("Date,Close\n", encoding="utf-8")


def test_removes_only_the_stale_able_files(tmp_path):
    """Sai o que pode envelhecer (5 anos com TTL, live, o dia de hoje); fica o
    histórico imutável e o cache de OUTRO ativo."""
    _seed(tmp_path,
          "AAPL-YFin-5y.csv",
          "AAPL-YFin-data-2021-01-01-2026-08-28.csv",
          "AAPL-YFin-intraday-15m-live.csv",
          f"AAPL-YFin-intraday-15m-{TODAY}.csv",
          "AAPL-YFin-intraday-15m-2026-08-20.csv",
          "MSFT-YFin-5y.csv")

    removed = invalidate_price_cache(tmp_path, "AAPL", today=TODAY)

    assert sorted(removed) == [
        "AAPL-YFin-5y.csv",
        "AAPL-YFin-data-2021-01-01-2026-08-28.csv",
        f"AAPL-YFin-intraday-15m-{TODAY}.csv",
        "AAPL-YFin-intraday-15m-live.csv",
    ]
    assert (tmp_path / "AAPL-YFin-intraday-15m-2026-08-20.csv").exists()
    assert (tmp_path / "MSFT-YFin-5y.csv").exists()


def test_crypto_pair_reaches_the_base_asset_files(tmp_path):
    """Cripto guarda o intradiário pelo ATIVO BASE do par (``BTC-USD`` → ``BTC-BINANCE``):
    o refresh precisa alcançar os dois jeitos de nomear o mesmo ativo."""
    _seed(tmp_path, "BTC-BINANCE-4h-live.csv", "BTC-USD-YFin-5y.csv",
          "BTC-BINANCE-4h-2026-08-10.csv")
    removed = invalidate_price_cache(tmp_path, "BTC-USD", today=TODAY)
    assert sorted(removed) == ["BTC-BINANCE-4h-live.csv", "BTC-USD-YFin-5y.csv"]
    assert (tmp_path / "BTC-BINANCE-4h-2026-08-10.csv").exists()


def test_prefix_match_is_exact_not_substring(tmp_path):
    """``AA`` não pode arrastar o cache de ``AAPL`` junto — o prefixo casa até o
    separador, senão um refresh derrubaria dado de outro ativo."""
    _seed(tmp_path, "AAPL-YFin-5y.csv", "AA-YFin-5y.csv")
    removed = invalidate_price_cache(tmp_path, "AA", today=TODAY)
    assert removed == ["AA-YFin-5y.csv"]
    assert (tmp_path / "AAPL-YFin-5y.csv").exists()


def test_survives_a_bad_ticker_and_a_missing_dir(tmp_path):
    """Falha soft: um ticker impossível de virar caminho ou um diretório ausente não
    levantam — o refresh segue, no pior caso lendo um candle cacheado."""
    assert invalidate_price_cache(tmp_path / "nao-existe", "AAPL") == []
    _seed(tmp_path, "AAPL-YFin-5y.csv")
    assert invalidate_price_cache(tmp_path, "../../etc", today=TODAY) == []
    assert (tmp_path / "AAPL-YFin-5y.csv").exists()

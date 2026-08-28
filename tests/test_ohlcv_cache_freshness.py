"""OHLCV cache: nem snapshot velho do dia (#1150), nem arquivo que não alcança o
dia pedido (bug L2, task 20260828-008).

Dois modos de servir preço errado a partir do cache:

* **dia corrente** — um run começado antes de a barra do dia fechar era reusado
  por todos os runs seguintes, alimentando a análise com um close parcial. Duas
  variantes: a barra pode faltar, ou estar presente mas ainda em formação (a
  Yahoo publica candle diário parcial durante o pregão). O TTL governa as duas.
* **cache que não cobre o dia pedido** — "linhas históricas são imutáveis" só
  justifica reusar um arquivo que CONTÉM o dia pedido. MCD e BE tinham a série
  diária parada em 24/08 enquanto o 4h dos mesmos símbolos já estava em 27/08:
  como o pedido era "histórico" (27/08 com hoje = 28/08), o cache era servido
  para sempre e o ``drop_nature`` mediu -1,3% onde a queda real era -4,6%.

O refetch é limitado por TTL nos dois casos, então nem feriado nem repetição
martelam a fonte.
"""
from __future__ import annotations

import os
import time

import pandas as pd
import pytest

import tradingagents.dataflows.stockstats_utils as su

TODAY = pd.Timestamp("2026-07-18")
STALE = su.OHLCV_CACHE_TTL_SECONDS + 60


def _write(tmp_path, name="cache.csv", age_seconds=0.0, last_date="2026-07-17"):
    f = tmp_path / name
    pd.DataFrame({"Date": [last_date], "Close": [1.0]}).to_csv(f, index=False)
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(f, (old, old))
    return str(f)


def _frame(last_date):
    return pd.DataFrame({"Date": [last_date], "Close": [1.0]})


@pytest.mark.unit
def test_current_day_cache_past_ttl_is_refreshed(tmp_path):
    # Bar missing (rows stop at yesterday) and file older than the TTL -> refetch.
    f = _write(tmp_path, age_seconds=STALE)
    assert su._needs_refresh(f, _frame("2026-07-17"), TODAY, TODAY) is True


@pytest.mark.unit
def test_partial_current_day_bar_is_still_refreshed(tmp_path):
    # Today's row is present but may be an in-progress candle whose Close is not
    # the closing price. Row inspection can't distinguish it, so the TTL governs.
    f = _write(tmp_path, age_seconds=STALE, last_date="2026-07-18")
    assert su._needs_refresh(f, _frame("2026-07-18"), TODAY, TODAY) is True


@pytest.mark.unit
def test_recent_cache_is_not_refetched(tmp_path):
    # Written moments ago: don't hammer the vendor (weekend/holiday guard).
    f = _write(tmp_path)
    assert su._needs_refresh(f, _frame("2026-07-17"), TODAY, TODAY) is False


@pytest.mark.unit
def test_historical_request_that_the_cache_covers_uses_cache(tmp_path):
    # Past dates are immutable AND the file reaches the requested day: never refetch.
    past = pd.Timestamp("2026-05-01")
    f = _write(tmp_path, age_seconds=STALE, last_date="2026-05-01")
    assert su._needs_refresh(f, _frame("2026-05-01"), past, TODAY) is False


# ------------------------------------------------- bug L2: cache que não cobre --
@pytest.mark.unit
def test_cache_que_nao_alcanca_o_dia_pedido_e_refetchado(tmp_path):
    """MCD/BE: diário parado em 24/08 e análise pedida em 27/08. Pedido histórico,
    mas faltam 3 pregões — o arquivo NÃO cobre o dia e tem de ser revalidado."""
    hoje = pd.Timestamp("2026-08-28")
    pedido = pd.Timestamp("2026-08-27")
    f = _write(tmp_path, age_seconds=STALE, last_date="2026-08-24")
    assert su._cache_covers(_frame("2026-08-24"), pedido) is False
    assert su._needs_refresh(f, _frame("2026-08-24"), pedido, hoje) is True


@pytest.mark.unit
def test_fim_de_semana_nao_dispara_refetch(tmp_path):
    """Sexta 2026-08-21 é a última barra e o pedido é sábado 22: não há pregão
    no meio, então o cache COBRE o dia — nada a buscar (nem martelar a fonte)."""
    hoje = pd.Timestamp("2026-08-28")
    sabado = pd.Timestamp("2026-08-22")
    f = _write(tmp_path, age_seconds=STALE, last_date="2026-08-21")
    assert su._cache_covers(_frame("2026-08-21"), sabado) is True
    assert su._needs_refresh(f, _frame("2026-08-21"), sabado, hoje) is False


@pytest.mark.unit
def test_refetch_de_cache_descoberto_respeita_o_ttl(tmp_path):
    """Feriado real (a fonte não tem a barra mesmo): o arquivo recém-escrito não é
    rebuscado de novo antes do TTL — a correção não vira martelo na fonte."""
    hoje = pd.Timestamp("2026-08-28")
    pedido = pd.Timestamp("2026-08-27")
    f = _write(tmp_path, last_date="2026-08-24")   # escrito agora
    assert su._needs_refresh(f, _frame("2026-08-24"), pedido, hoje) is False


@pytest.mark.unit
def test_cache_vazio_nunca_cobre():
    assert su._cache_covers(pd.DataFrame({"Date": [], "Close": []}), TODAY) is False


@pytest.mark.unit
def test_load_ohlcv_refetches_stale_same_day_cache(tmp_path, monkeypatch):
    """End-to-end: the helper is actually wired into load_ohlcv's cache branch.

    Without this, the unit tests above would still pass if the helper were never
    called from the real code path.
    """
    monkeypatch.setattr(su, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    monkeypatch.setattr(su.pd.Timestamp, "today", staticmethod(lambda: TODAY))

    # Pre-seed the cache file load_ohlcv will look for, aged past the TTL.
    start = (TODAY - pd.DateOffset(years=5)).strftime("%Y-%m-%d")
    end = (TODAY + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    # semeia os DOIS nomes: o datado (loader original) e o estável por símbolo
    # (wrapper do ta_datacache, que é quem roda de fato) — senão o teste exercita
    # o fallback do wrapper em vez do caminho real de cache.
    old = time.time() - STALE
    for name in (f"AAPL-YFin-data-{start}-{end}.csv", "AAPL-YFin-5y.csv"):
        cache_file = tmp_path / name
        pd.DataFrame({"Date": ["2026-07-17"], "Close": [100.0]}).to_csv(cache_file, index=False)
        os.utime(cache_file, (old, old))

    calls = []

    def _fake_download(*a, **k):
        calls.append(1)
        return pd.DataFrame(
            {"Date": pd.to_datetime(["2026-07-17", "2026-07-18"]), "Close": [100.0, 222.0]}
        ).set_index("Date")

    monkeypatch.setattr(su.yf, "download", _fake_download)

    out = su.load_ohlcv("AAPL", TODAY.strftime("%Y-%m-%d"))

    assert calls, "stale same-day cache must trigger a refetch"
    assert 222.0 in out["Close"].values, "refreshed close must reach the caller"


@pytest.mark.unit
def test_load_ohlcv_reuses_fresh_same_day_cache(tmp_path, monkeypatch):
    # Mirror image: a fresh cache must NOT trigger a download.
    monkeypatch.setattr(su, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    monkeypatch.setattr(su.pd.Timestamp, "today", staticmethod(lambda: TODAY))

    start = (TODAY - pd.DateOffset(years=5)).strftime("%Y-%m-%d")
    end = (TODAY + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    for name in (f"AAPL-YFin-data-{start}-{end}.csv", "AAPL-YFin-5y.csv"):
        pd.DataFrame({"Date": ["2026-07-18"], "Close": [100.0]}).to_csv(
            tmp_path / name, index=False)

    # conta chamadas em vez de levantar: o loader agora cai pro cache quando a
    # fonte falha, então uma exceção aqui não provaria mais nada.
    calls = []

    def _count_download(*a, **k):
        calls.append(1)
        return pd.DataFrame({"Date": pd.to_datetime(["2026-07-18"]), "Close": [1.0]}).set_index("Date")

    monkeypatch.setattr(su.yf, "download", _count_download)
    su.load_ohlcv("AAPL", TODAY.strftime("%Y-%m-%d"))
    assert not calls, "fresh cache must not refetch"


@pytest.mark.unit
def test_load_ohlcv_refetches_cache_que_nao_cobre_o_dia(tmp_path, monkeypatch):
    """E2E do bug L2 no caminho REAL (wrapper estável do ta_datacache).

    Reproduz MCD/BE: cache com última barra em 24/08, análise pedida em 27/08 e
    "hoje" em 28/08 — pedido histórico. Antes o arquivo era servido para sempre e
    a série chegava ao ``drop_nature`` sem 25, 26 e 27/08. Agora é revalidado e as
    barras que faltavam aparecem.
    """
    hoje = pd.Timestamp("2026-08-28")
    monkeypatch.setattr(su, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    monkeypatch.setattr(su.pd.Timestamp, "today", staticmethod(lambda: hoje))

    start = (hoje - pd.DateOffset(years=5)).strftime("%Y-%m-%d")
    end = (hoje + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    velho = pd.DataFrame({"Date": ["2026-08-20", "2026-08-21", "2026-08-24"],
                          "Close": [269.2, 269.2, 272.6]})
    antigo = time.time() - STALE
    for name in (f"MCD-YFin-data-{start}-{end}.csv", "MCD-YFin-5y.csv"):
        velho.to_csv(tmp_path / name, index=False)
        os.utime(tmp_path / name, (antigo, antigo))

    calls = []

    def _fake_download(*a, **k):
        calls.append(1)
        return pd.DataFrame({
            "Date": pd.to_datetime(["2026-08-20", "2026-08-21", "2026-08-24",
                                    "2026-08-25", "2026-08-26", "2026-08-27"]),
            "Close": [269.2, 269.2, 272.6, 268.0, 264.5, 260.1],
        }).set_index("Date")

    import yfinance as yf
    monkeypatch.setattr(yf, "download", _fake_download)
    monkeypatch.setattr(su.yf, "download", _fake_download)

    out = su.load_ohlcv("MCD", "2026-08-27")

    assert calls, "cache que não alcança o dia pedido tem de ser revalidado"
    assert str(out["Date"].max().date()) == "2026-08-27"
    assert 260.1 in out["Close"].values      # a barra que faltava chegou ao chamador

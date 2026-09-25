"""OHLCV cache: a regra do FECHAMENTO (task 20260925-038).

A série diária/semanal fica congelada dentro do pregão e é renovada — inteira,
sem emenda — na primeira leitura depois do fechamento (16:10 NY em dia útil;
00:00 UTC pra cripto). Substitui o TTL de 15 min (#1150) e a cobertura do dia
pedido (bug L2): um arquivo gravado antes do último fechamento não vale, seja o
pedido de hoje ou histórico.

O defeito que motivou: a emenda incremental (DA-119) colou um trecho em que o
Yahoo omitiu o pregão de 22/09/2026, e o buraco ficou no cache pra sempre —
AAPL EMA21 327,78 no TD × 328,81 fresco em 24/09.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import tradingagents.dataflows.stockstats_utils as su

NY = ZoneInfo("America/New_York")


def _ny(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=NY)


def _grava(tmp_path, quando: datetime, name="c.csv"):
    f = tmp_path / name
    pd.DataFrame({"Date": ["2026-09-24"], "Close": [1.0]}).to_csv(f, index=False)
    os.utime(f, (quando.timestamp(), quando.timestamp()))
    return str(f)


# ------------------------------------------------------------ a regra pura ----
@pytest.mark.unit
@pytest.mark.parametrize("agora,esperado", [
    (_ny(2026, 9, 24, 11, 0), _ny(2026, 9, 23, 16, 10)),   # qui no pregão -> qua
    (_ny(2026, 9, 24, 16, 9), _ny(2026, 9, 23, 16, 10)),   # antes da folga
    (_ny(2026, 9, 24, 16, 10), _ny(2026, 9, 24, 16, 10)),  # fechou
    (_ny(2026, 9, 27, 12, 0), _ny(2026, 9, 25, 16, 10)),   # domingo -> sexta
    (_ny(2026, 9, 28, 9, 0), _ny(2026, 9, 25, 16, 10)),    # seg pré-abertura -> sexta
    (_ny(2026, 11, 2, 12, 0), _ny(2026, 10, 30, 16, 10)),  # atravessa o fim do DST
])
def test_ultimo_fechamento_acao(agora, esperado):
    assert su.ultimo_fechamento(agora) == esperado


@pytest.mark.unit
def test_ultimo_fechamento_cripto_e_meia_noite_utc_todo_dia():
    agora = datetime(2026, 9, 27, 3, 0, tzinfo=timezone.utc)   # domingo
    assert su.ultimo_fechamento(agora, cripto=True) == datetime(2026, 9, 27, tzinfo=timezone.utc)


@pytest.mark.unit
def test_dentro_do_pregao_NAO_renova(tmp_path):
    # gravado depois do fechamento de ontem; hoje o pregão está aberto
    f = _grava(tmp_path, _ny(2026, 9, 23, 17, 0))
    assert su._needs_refresh(f, agora=_ny(2026, 9, 24, 11, 0)) is False
    assert su._needs_refresh(f, agora=_ny(2026, 9, 24, 15, 59)) is False


@pytest.mark.unit
def test_depois_do_fechamento_RENOVA_uma_vez(tmp_path):
    f = _grava(tmp_path, _ny(2026, 9, 24, 11, 0))            # gravado no pregão
    assert su._needs_refresh(f, agora=_ny(2026, 9, 24, 16, 30)) is True
    f = _grava(tmp_path, _ny(2026, 9, 24, 16, 31))           # a renovação
    assert su._needs_refresh(f, agora=_ny(2026, 9, 24, 20, 0)) is False


@pytest.mark.unit
def test_arquivo_de_dias_atras_renova_mesmo_pra_pedido_historico(tmp_path):
    """O bug L2 e o histórico eterno: nenhum pedido serve arquivo de antes do fechamento."""
    f = _grava(tmp_path, _ny(2026, 8, 24, 17, 0))
    assert su._needs_refresh(f, agora=_ny(2026, 8, 28, 10, 0)) is True


@pytest.mark.unit
def test_fim_de_semana_nao_renova_arquivo_de_sexta_a_noite(tmp_path):
    f = _grava(tmp_path, _ny(2026, 9, 25, 18, 0))
    assert su._needs_refresh(f, agora=_ny(2026, 9, 27, 12, 0)) is False


@pytest.mark.unit
def test_cripto_renova_no_fim_de_semana(tmp_path):
    f = _grava(tmp_path, datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc))
    agora = datetime(2026, 9, 27, 1, 0, tzinfo=timezone.utc)
    assert su._needs_refresh(f, agora=agora) is False
    assert su._needs_refresh(f, cripto=True, agora=agora) is True


# ------------------------------------------------------ no caminho REAL ----
def _semeia(tmp_path, symbol, frame, mtime):
    today = pd.Timestamp.today()
    start = (today - pd.DateOffset(years=5)).strftime("%Y-%m-%d")
    end = (today + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    # os DOIS nomes: o datado (loader original) e o estável (wrapper do datacache,
    # que é quem roda de fato) — senão o teste exercita só o fallback.
    for name in (f"{symbol}-YFin-data-{start}-{end}.csv", f"{symbol}-YFin-5y.csv"):
        frame.to_csv(tmp_path / name, index=False)
        os.utime(tmp_path / name, (mtime, mtime))


def _fonte(monkeypatch, frame, calls):
    import yfinance as yf

    def _dl(*a, **k):
        calls.append(k.get("start"))
        return frame.set_index("Date")
    monkeypatch.setattr(yf, "download", _dl)
    monkeypatch.setattr(su.yf, "download", _dl)


@pytest.mark.unit
def test_load_ohlcv_nao_rebaixa_o_que_foi_gravado_depois_do_fechamento(tmp_path, monkeypatch):
    monkeypatch.setattr(su, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    _semeia(tmp_path, "AAPL", pd.DataFrame({"Date": ["2026-09-23"], "Close": [100.0]}),
            su.ultimo_fechamento().timestamp() + 60)
    calls = []
    _fonte(monkeypatch, pd.DataFrame({"Date": pd.to_datetime(["2026-09-23"]), "Close": [1.0]}), calls)
    su.load_ohlcv("AAPL", "2026-09-23")
    assert not calls, "série congelada não pode bater na fonte"


@pytest.mark.unit
def test_load_ohlcv_depois_do_fechamento_baixa_INTEIRO_e_cura_o_buraco(tmp_path, monkeypatch):
    """O caso real: cache sem o pregão de 22/09. A renovação pede a janela inteira
    (não 'do último dia do cache') e o dia que faltava volta."""
    monkeypatch.setattr(su, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    furado = pd.DataFrame({"Date": ["2026-09-19", "2026-09-23"], "Close": [100.0, 102.0]})
    _semeia(tmp_path, "AAPL", furado, su.ultimo_fechamento().timestamp() - 60)
    calls = []
    fresco = pd.DataFrame({"Date": pd.to_datetime(["2026-09-19", "2026-09-22", "2026-09-23"]),
                           "Close": [100.0, 101.0, 102.0]})
    _fonte(monkeypatch, fresco, calls)
    out = su.load_ohlcv("AAPL", "2026-09-23")
    assert calls and calls[0] != "2026-09-23", f"pediu incremental: start={calls}"
    assert "2026-09-22" in set(out["Date"].dt.strftime("%Y-%m-%d"))


@pytest.mark.unit
def test_load_ohlcv_dividendo_troca_a_escala_do_historico_inteiro(tmp_path, monkeypatch):
    """Ex-dividendo: o Yahoo reescala TODO o histórico (auto_adjust). A série velha
    não sobrevive a nenhuma linha — nada de emenda com escala misturada."""
    monkeypatch.setattr(su, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    velho = pd.DataFrame({"Date": ["2026-08-06", "2026-08-07"], "Close": [200.0, 201.0]})
    _semeia(tmp_path, "AAPL", velho, su.ultimo_fechamento().timestamp() - 60)
    k = 1 - 0.27 / 201.0
    novo = pd.DataFrame({"Date": pd.to_datetime(["2026-08-06", "2026-08-07", "2026-08-10"]),
                         "Close": [200.0 * k, 201.0 * k, 199.0]})
    calls = []
    _fonte(monkeypatch, novo, calls)
    out = su.load_ohlcv("AAPL", "2026-08-10")
    assert calls
    assert out["Close"].tolist() == pytest.approx([200.0 * k, 201.0 * k, 199.0])

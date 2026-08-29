"""A DECLARAÇÃO de série vencida está LIGADA no caminho real do ``load_ohlcv``.

Por que este arquivo existe: a suíte do C4 provava a FUNÇÃO
(``_declara_serie_vencida``) e provava a ENTREGA (o aviso chegando ao
``degraded`` da run), mas o teste de entrega monkeypatchava um ``fetch_price_chart``
que ele mesmo chamava a declaração. Medido por mutação: apagar as QUATRO chamadas
reais — ``stockstats_utils.load_ohlcv`` (fail-open e final) e as duas cópias do
wrapper do ta_datacache — não quebrava teste nenhum. O caminho onde o bug L2 morava
podia sumir em silêncio.

Aqui o ``load_ohlcv`` roda de verdade, com a fonte simulada, e o teste morre se a
chamada sumir. Cobre as DUAS implementações: o wrapper estável do ta_datacache (o
que roda em produção) e o loader original de ``stockstats_utils`` — a mesma lógica
duplicada em dois arquivos, e uma correção que pegasse só uma delas seria pior que
nenhuma, porque pareceria feita.
"""
import os
import time

import pandas as pd
import pytest

from tradingagents.dataflows import data_notices, stockstats_utils as su

pytestmark = pytest.mark.unit

HOJE = pd.Timestamp("2026-08-28")
PEDIDO = "2026-08-27"          # análise histórica (ontem)
ULTIMA_BARRA = "2026-08-24"    # a série para 3 dias úteis antes: o buraco do L2
_TTL_VENCIDO = su.OHLCV_CACHE_TTL_SECONDS + 60


@pytest.fixture(autouse=True)
def _limpo():
    data_notices.reset()
    yield
    data_notices.reset()


def _loader_original():
    """O ``load_ohlcv`` de ``stockstats_utils`` ANTES do patch do ta_datacache.

    O patch substitui o atributo do módulo pelo wrapper estável, guardando o
    original numa closure. Sem alcançá-lo, metade da lógica duplicada ficaria sem
    teste — e é justamente a metade que o wrapper usa como fallback."""
    fn = su.load_ohlcv
    for cell in (fn.__closure__ or ()):
        try:
            valor = cell.cell_contents
        except ValueError:      # célula vazia
            continue
        if callable(valor) and getattr(valor, "__name__", "") == "load_ohlcv" and valor is not fn:
            return valor
    return fn                   # patch desativado: o atributo já É o original


def _loaders():
    """(rótulo, função) das duas implementações que precisam da declaração."""
    original = _loader_original()
    if original is su.load_ohlcv:
        return [("stockstats_utils", su.load_ohlcv)]
    return [("wrapper ta_datacache", su.load_ohlcv), ("stockstats_utils", original)]


def _semeia_cache(tmp_path, symbol, vencido=True):
    """Cache com a última barra em 24/08 — nos DOIS nomes de arquivo (o datado do
    loader original e o estável do wrapper), senão o teste exercita o fallback."""
    start = (HOJE - pd.DateOffset(years=5)).strftime("%Y-%m-%d")
    end = (HOJE + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    frame = pd.DataFrame({"Date": ["2026-08-20", "2026-08-21", ULTIMA_BARRA],
                          "Open": [1.0, 1.0, 1.0], "High": [1.0, 1.0, 1.0],
                          "Low": [1.0, 1.0, 1.0], "Close": [269.2, 269.2, 272.6],
                          "Volume": [1, 1, 1]})
    antigo = time.time() - _TTL_VENCIDO
    for name in (f"{symbol}-YFin-data-{start}-{end}.csv", f"{symbol}-YFin-5y.csv"):
        f = tmp_path / name
        frame.to_csv(f, index=False)
        if vencido:
            os.utime(f, (antigo, antigo))


@pytest.mark.parametrize("rotulo,loader", _loaders(), ids=lambda x: x if isinstance(x, str) else "")
def test_fail_open_de_cache_vencido_declara_no_caminho_real(tmp_path, monkeypatch, rotulo, loader):
    """A fonte cai, o cache vencido entra no lugar — e isso é DITO.

    DENTE: apagar a chamada de ``_declara_serie_vencida`` do ramo ``except`` faz
    este teste falhar. A declaração final (que também roda aqui) não salva o teste,
    porque o motivo "a atualização da fonte falhou" só existe naquela chamada.
    """
    _semeia_cache(tmp_path, "MCD")
    monkeypatch.setattr(su, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    monkeypatch.setattr(su.pd.Timestamp, "today", staticmethod(lambda: HOJE))

    def _fonte_fora_do_ar(*a, **k):
        raise RuntimeError("provedor fora do ar")

    monkeypatch.setattr(su.yf, "download", _fonte_fora_do_ar)

    out = loader("MCD", PEDIDO)
    assert not out.empty, "o fail-open tem que servir o cache, não estourar"

    avisos = data_notices.snapshot()
    assert avisos, f"[{rotulo}] cache vencido servido em SILÊNCIO — a declaração sumiu"
    assert any("falhou" in a["reason"] for a in avisos), avisos
    assert any("MCD" in a["label"] for a in avisos), avisos


@pytest.mark.parametrize("rotulo,loader", _loaders(), ids=lambda x: x if isinstance(x, str) else "")
def test_serie_que_nao_alcanca_a_data_declara_no_caminho_real(tmp_path, monkeypatch, rotulo, loader):
    """Download OK, mas a série entregue para 3 dias úteis antes da data pedida.

    É o buraco do L2 exatamente: passa longe do guard duro (10 dias) e muda a
    leitura sem mudar a aparência. DENTE: apagar a declaração final do
    ``load_ohlcv`` faz este teste falhar (aqui não há fail-open pra encobrir).
    """
    monkeypatch.setattr(su, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    monkeypatch.setattr(su.pd.Timestamp, "today", staticmethod(lambda: HOJE))

    def _fonte_atrasada(*a, **k):
        return pd.DataFrame({
            "Date": pd.to_datetime(["2026-08-20", "2026-08-21", ULTIMA_BARRA]),
            "Open": [1.0, 1.0, 1.0], "High": [1.0, 1.0, 1.0],
            "Low": [1.0, 1.0, 1.0], "Close": [269.2, 269.2, 272.6],
            "Volume": [1, 1, 1],
        }).set_index("Date")

    monkeypatch.setattr(su.yf, "download", _fonte_atrasada)

    out = loader("MCD", PEDIDO)
    assert not out.empty
    assert str(out["Date"].max().date()) == ULTIMA_BARRA

    avisos = data_notices.snapshot()
    assert avisos, f"[{rotulo}] série 3 dias atrás entregue sem uma palavra"
    assert any(ULTIMA_BARRA in a["reason"] and PEDIDO in a["reason"] for a in avisos), avisos
    assert all(a["kind"] == "suspect" for a in avisos), avisos


def test_serie_em_dia_nao_declara_nada(tmp_path, monkeypatch):
    """Contra-prova: o caminho real não vira gerador de ruído. Série que alcança a
    data pedida passa calada — senão o aviso perde o significado."""
    monkeypatch.setattr(su, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    monkeypatch.setattr(su.pd.Timestamp, "today", staticmethod(lambda: HOJE))
    monkeypatch.setattr(su.yf, "download", lambda *a, **k: pd.DataFrame({
        "Date": pd.to_datetime(["2026-08-26", PEDIDO]),
        "Open": [1.0, 1.0], "High": [1.0, 1.0], "Low": [1.0, 1.0],
        "Close": [270.0, 272.6], "Volume": [1, 1],
    }).set_index("Date"))

    su.load_ohlcv("MCD", PEDIDO)
    assert data_notices.snapshot() == []

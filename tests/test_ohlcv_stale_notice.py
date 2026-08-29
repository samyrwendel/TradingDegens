"""Série OHLCV vencida deixa de ser servida em SILÊNCIO (achado C4).

O guard duro (#1021) rejeita o absurdo — frame de um ano atrás. Mas o bug L2 era
sutil: um buraco de 3 dias na série diária do MCD, bem abaixo dos 10 dias do
limiar, servido pelo caminho de fail-open (o download falhou, o cache vencido
entrou no lugar). O ``drop_nature`` leu -1,3% onde a queda real era -4,6%, o
veredito mudou — e **nada disso chegou ao leitor**: nem ``degraded``, nem
relatório, nem uma linha.

Aqui a regra é: dado velho pode até ser servido (é melhor que nada), mas nunca
CALADO. O aviso viaja pelo mesmo ``degraded_sources`` que a UI já sabe nomear.
"""
import pandas as pd
import pytest

from tradingagents.dataflows import data_notices
from tradingagents.dataflows.stockstats_utils import (
    OHLCV_STALE_NOTICE_BDAYS,
    _bdays_atras,
    _declara_serie_vencida,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _limpo():
    data_notices.reset()
    yield
    data_notices.reset()


def _frame(*datas):
    return pd.DataFrame({
        "Date": [pd.Timestamp(d) for d in datas],
        "Open": [1.0] * len(datas), "High": [1.0] * len(datas),
        "Low": [1.0] * len(datas), "Close": [1.0] * len(datas),
        "Volume": [1] * len(datas),
    })


# ------------------------------------------------------------ o buraco do L2 ---
def test_o_buraco_de_3_dias_do_L2_e_declarado():
    """O caso real: série até 24/08 servida pra uma análise de 27/08.

    Três dias — passava calado pelo limiar de 10. Agora sai nomeado, com a última
    barra e a idade, porque é ele que muda a leitura sem mudar a aparência.
    """
    _declara_serie_vencida(_frame("2026-08-20", "2026-08-24"), "2026-08-27", "MCD")
    avisos = data_notices.snapshot()
    assert len(avisos) == 1, avisos
    a = avisos[0]
    assert "MCD" in a["label"]
    assert "2026-08-24" in a["reason"] and "2026-08-27" in a["reason"]
    assert a["kind"] == "suspect"    # o dado ESTÁ na análise, só não é confiável


def test_serie_que_alcanca_a_data_nao_gera_ruido():
    _declara_serie_vencida(_frame("2026-08-27"), "2026-08-27", "MCD")
    assert data_notices.snapshot() == []


def test_fim_de_semana_nao_vira_alarme_falso():
    """Sexta → segunda é 1 dia útil de distância (3 de calendário). Um painel que
    grita toda segunda-feira ensina o leitor a ignorar o aviso."""
    _declara_serie_vencida(_frame("2026-08-21"), "2026-08-24", "AAPL")   # sex → seg
    assert data_notices.snapshot() == []


def test_fail_open_declara_mesmo_com_a_serie_em_dia():
    """Servir cache porque o download FALHOU é degradação por si só — mesmo que a
    última barra alcance a data, ninguém garante que ela é a última que existe."""
    _declara_serie_vencida(
        _frame("2026-08-27"), "2026-08-27", "MCD",
        motivo="a atualização da fonte falhou e a série veio do cache")
    avisos = data_notices.snapshot()
    assert len(avisos) == 1
    assert "falhou" in avisos[0]["reason"]


def test_bdays_conta_o_buraco_nao_o_calendario():
    assert _bdays_atras(pd.Timestamp("2026-08-21"), pd.Timestamp("2026-08-24")) == 1
    assert _bdays_atras(pd.Timestamp("2026-08-24"), pd.Timestamp("2026-08-27")) == 3
    assert _bdays_atras(pd.Timestamp("2026-08-27"), pd.Timestamp("2026-08-27")) == 0
    # futuro (série além da data pedida) nunca é buraco
    assert _bdays_atras(pd.Timestamp("2026-08-28"), pd.Timestamp("2026-08-27")) == 0


def test_limiar_e_de_dias_nao_de_semanas():
    """A granularidade que o drop_nature exige. Com o limiar antigo (10 dias de
    calendário) o buraco do L2 não existia — este teste trava a coerência."""
    assert OHLCV_STALE_NOTICE_BDAYS <= 3


# ------------------------------------------------------------ o coletor --------
def test_avisos_deduplicam_dentro_da_run():
    """A mesma série degradada é lida N vezes por run (vários indicadores). O
    leitor precisa saber UMA vez."""
    for _ in range(5):
        _declara_serie_vencida(_frame("2026-08-24"), "2026-08-27", "MCD")
    assert len(data_notices.snapshot()) == 1


def test_drain_esvazia_e_merge_junta_no_estado():
    _declara_serie_vencida(_frame("2026-08-24"), "2026-08-27", "MCD")
    estado = {"degraded_sources": [{"label": "Reddit", "report_key": "",
                                    "reason": "fonte caiu", "kind": "missing"}]}
    data_notices.merge_into(estado)
    labels = [d["label"] for d in estado["degraded_sources"]]
    assert "Reddit" in labels and any("MCD" in x for x in labels)
    assert data_notices.snapshot() == []          # drenado


def test_reset_isola_uma_run_da_outra():
    _declara_serie_vencida(_frame("2026-08-24"), "2026-08-27", "MCD")
    data_notices.reset()
    assert data_notices.snapshot() == []


def test_merge_em_estado_sem_avisos_nao_mexe_em_nada():
    estado = {"degraded_sources": []}
    assert data_notices.merge_into(estado) is estado
    assert estado["degraded_sources"] == []


# ------------------------------------------- o aviso CHEGA ao resultado --------
def test_o_aviso_aparece_no_degraded_da_run_123(tmp_path, monkeypatch):
    """A fiação: registrado no fetch, drenado pelo worker, entregue no ``degraded``.

    É o pedaço que faltava — sem ela o aviso morreria num log e o leitor veria o
    número velho com cara de novo, que é o defeito inteiro do C4.
    """
    from tradingagents.webui import runner as rm
    from tradingagents.webui.runner import AnalysisRunner
    from tradingagents.webui.store import HistoryStore

    def _chart(t, d, tf="1d", method="padrao"):
        _declara_serie_vencida(_frame("2026-08-24"), "2026-08-27", "MCD")
        return {"candles": [{"c": 1.0}]}

    monkeypatch.setattr(rm, "fetch_price_chart", _chart)
    monkeypatch.setattr(rm, "fetch_actionable_plan",
                        lambda t, d, tf="1d", method="padrao":
                        {"price": 100.0, "pattern": None, "setup_state": "sem_setup"})
    monkeypatch.setattr(AnalysisRunner, "detect_asset_type", lambda self, t: "stock")

    r = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                       store=HistoryStore(tmp_path))
    run_id = r.start("MCD", "2026-08-27", method="setup123", reuse=False)
    run = r._runs[run_id]
    for _ in range(200):
        if run.status != "running":
            break
        import time as _t
        _t.sleep(0.02)
    assert run.status == "done", run.error
    deg = (run.result or {}).get("degraded") or []
    assert any("MCD" in d.get("label", "") for d in deg), deg
    assert all(d.get("kind") == "suspect" for d in deg), deg


# ---------------------------------------- o limiar diz o que o comentário diz ---
def test_buraco_de_EXATAMENTE_dois_dias_uteis_declara():
    """O off-by-one: o comentário chamava 2 dias úteis de "o menor buraco que já
    corrompeu uma leitura" e o código (``<=``) o deixava passar CALADO — só 3+
    declarava. Ter→qui é exatamente 2. DENTE: com ``<=`` de volta, some o aviso."""
    _declara_serie_vencida(_frame("2026-08-25"), "2026-08-27", "MCD")   # ter → qui
    avisos = data_notices.snapshot()
    assert len(avisos) == 1, "buraco de 2 dias úteis não pode passar calado"
    assert "2026-08-25" in avisos[0]["reason"]


def test_um_dia_util_segue_calado_porque_e_o_estado_normal_ao_vivo():
    """Contra-prova: 1 dia útil de atraso é a run ao vivo antes do fechamento (a
    barra de hoje ainda não publicou). Declarar aí seria aviso em toda run — e aviso
    que aparece sempre é aviso que ninguém lê."""
    _declara_serie_vencida(_frame("2026-08-26"), "2026-08-27", "MCD")   # qua → qui
    assert data_notices.snapshot() == []


def test_o_limiar_e_o_MENOR_buraco_que_declara():
    """Trava a semântica da constante contra o próximo leitor: ela é o piso do que
    declara (inclusive), não o teto do que cala."""
    assert OHLCV_STALE_NOTICE_BDAYS == 2

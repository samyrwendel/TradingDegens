"""Reversão à média em EXTREMO de Bollinger (task 20260904-015) — o detector do
vídeo Value Trade, medido pelo MESMO ledger que o Setup123 e o Storm.

Cada teste é um DENTE contra o jeito mais fácil de estragar a regra. O dente
central é a regra 2: **é o FECHAMENTO fora da banda que vale, não o pavio** — um
candle que fura a banda com a sombra e volta pra dentro NÃO é sinal.

O que é DO VÍDEO e o que é NOSSO está no cabeçalho de
``price_structure.build_bollinger_plan`` — os testes guardam o que é do vídeo
(bandas 20/3σ, fechamento, gatilho na estrutura, alvo na média) e o que é nosso
(ancoragem estacionária, sizing pelo ledger).
"""

import pandas as pd
import pytest

from tradingagents.dataflows import price_structure as ps
from tradingagents.webui import scanner as sc
from tradingagents.webui.scanner import _metodo_frame


def _df(rows):
    d = pd.DataFrame(rows)
    d["Date"] = pd.to_datetime(d["Date"])
    return d


def _bar(dia, o, h, lo, c):
    return {"Date": f"2026-03-{dia:02d}", "Open": o, "High": h, "Low": lo, "Close": c}


# 22 barras CALMAS (fechamento alternando 100,0 / 100,4): banda de 3σ apertada em
# torno de ~[99,6 · 100,8]. Sobre elas se pendura UM candle de teste no dia 23.
_CALMAS = [_bar(d, 100.2, 100.6, 99.8, 100.0 if d % 2 else 100.4) for d in range(1, 23)]


# ───────────────────────────── regra 1: bandas 20/3σ ────────────────────────
def test_banda_e_20_periodos_3_desvios_populacional():
    """A banda é a média SIMPLES de 20 ± 3× o desvio POPULACIONAL (ddof=0, a
    convenção das plataformas). Confere-se contra o numpy cru na última barra."""
    df = _df(_CALMAS)
    central, superior, inferior = ps._bollinger_bands(df)
    closes = df["Close"].astype(float).values[-20:]
    media = closes.mean()
    desvio = closes.std(ddof=0)                       # POPULACIONAL, não amostral
    assert central.iloc[-1] == pytest.approx(media, abs=1e-9)
    assert superior.iloc[-1] == pytest.approx(media + 3.0 * desvio, abs=1e-9)
    assert inferior.iloc[-1] == pytest.approx(media - 3.0 * desvio, abs=1e-9)
    assert ps._BOLL_PERIODO == 20 and ps._BOLL_DESVIOS == 3.0


def test_barras_sem_20_periodos_nao_tem_banda_nem_sinal():
    """Sem os 20 períodos a banda é NaN — e uma barra sem banda nunca vira sinal."""
    curto = _df([_bar(d, 100, 101, 99, 90) for d in range(1, 6)])   # 5 barras
    assert ps._bollinger_extremo(curto, "%Y-%m-%d") is None


# ──────────────── regra 2: FECHAMENTO fora da banda, não o pavio ─────────────
def test_pavio_que_fura_e_volta_NAO_e_sinal():
    """O DENTE CENTRAL. Um candle com sombra ENORME abaixo da banda (mínima 80) mas
    que FECHA dentro (100,4) não conta — 'é ele fechar lá fora que vale'. Sem
    fechamento fora, o detector não acha extremo nenhum."""
    pavio = _df(_CALMAS + [_bar(23, 100.4, 100.6, 80.0, 100.4)])
    assert ps._bollinger_extremo(pavio, "%Y-%m-%d") is None


def test_fechamento_abaixo_da_banda_e_COMPRA():
    """Fechou abaixo da inferior → candidato a COMPRA. O mesmo candle do teste do
    pavio, mas agora FECHANDO fora (90) — aí sim é sinal."""
    df = _df(_CALMAS + [_bar(23, 95.0, 96.0, 88.0, 90.0)])
    pat = ps._bollinger_extremo(df, "%Y-%m-%d")
    assert pat is not None and pat.direction == "compra"


def test_fechamento_acima_da_banda_e_VENDA():
    df = _df(_CALMAS + [_bar(23, 105.0, 112.0, 104.0, 110.0)])
    pat = ps._bollinger_extremo(df, "%Y-%m-%d")
    assert pat is not None and pat.direction == "venda"


# ──────────────── regras 4 e 5: gatilho na estrutura, stop oposto ────────────
def test_compra_gatilho_na_MAXIMA_stop_na_MINIMA():
    """COMPRA entra na SUPERAÇÃO DA MÁXIMA do extremo ([28:29] do vídeo); o stop
    fica logo além da MÍNIMA. Trocar os dois é o erro clássico."""
    df = _df(_CALMAS + [_bar(23, 95.0, 96.0, 88.0, 90.0)])
    pat = ps._bollinger_extremo(df, "%Y-%m-%d")
    assert pat.trigger == 96.0, "gatilho = máxima do candle extremo"
    assert pat.stop_price == 88.0, "stop = mínima do candle extremo"


def test_venda_gatilho_na_MINIMA_stop_na_MAXIMA():
    """VENDA entra na PERDA DA MÍNIMA ([26:06]); stop logo além da MÁXIMA. É o
    espelho exato da compra."""
    df = _df(_CALMAS + [_bar(23, 105.0, 112.0, 104.0, 110.0)])
    pat = ps._bollinger_extremo(df, "%Y-%m-%d")
    assert pat.trigger == 104.0, "gatilho = mínima do candle extremo"
    assert pat.stop_price == 112.0, "stop = máxima do candle extremo"


# ──────────────────────── regra 6: alvo é a banda central ────────────────────
def test_alvo_e_a_banda_central_a_media():
    """O alvo é a MÉDIA DO MEIO (banda central) na barra do extremo — a volta ao
    equilíbrio, nunca a banda que rompeu."""
    df = _df(_CALMAS + [_bar(23, 95.0, 96.0, 88.0, 90.0)])
    pat = ps._bollinger_extremo(df, "%Y-%m-%d")
    central, _, inferior = ps._bollinger_bands(df)
    assert pat.media == pytest.approx(central.iloc[-1], abs=1e-9)
    assert pat.media != pytest.approx(inferior.iloc[-1]), "alvo é a média, não a banda"


# ───────────────── barra em formação NÃO pode ser o extremo ──────────────────
def test_barra_em_formacao_nao_vira_extremo():
    """'Fechou fora' só é fato numa barra FECHADA. Com a última em formação, o
    fechamento dela ainda muda — não pode ser o extremo (mesma regra do ponto 3 do
    Storm). Fechada, ela é o extremo."""
    df = _df(_CALMAS + [_bar(23, 95.0, 96.0, 88.0, 90.0)])
    assert ps._bollinger_extremo(df, "%Y-%m-%d", ultima_em_formacao=True) is None
    assert ps._bollinger_extremo(df, "%Y-%m-%d", ultima_em_formacao=False) is not None


def test_extremo_mais_recente_vence():
    """Dois fechamentos fora na janela → o MAIS RECENTE é o extremo lido (é o que
    decide agora)."""
    df = _df(_CALMAS + [_bar(23, 95.0, 96.0, 88.0, 90.0),    # compra
                        _bar(24, 100.2, 100.6, 99.8, 100.4),  # calma
                        _bar(25, 105.0, 112.0, 104.0, 110.0)])  # venda, mais recente
    pat = ps._bollinger_extremo(df, "%Y-%m-%d")
    assert pat.direction == "venda" and pat.extremo["date"] == "2026-03-25"


# ─────────────────────── o plano completo (build_*) ─────────────────────────
def test_build_bollinger_plan_monta_niveis_e_parcial(monkeypatch):
    """O plano da tela: pattern + stop + alvo (média) + R:R + a PARCIAL declarada
    (regra 6, o vídeo cita parcial em 100% do risco — exposta, nunca escondida)."""
    df = _df(_CALMAS + [_bar(23, 95.0, 96.0, 88.0, 90.0)])
    monkeypatch.setattr(ps, "_prep", lambda *a, **k: df)
    plano = ps.build_bollinger_plan("XPTO", "2026-03-24", "1d")
    pat = plano["pattern"]
    assert pat["direction"] == "compra" and pat["trigger"] == 96.0
    assert plano["stop"]["price"] == 88.0
    central, _, _ = ps._bollinger_bands(df)
    assert plano["target"]["price"] == pytest.approx(round(central.iloc[-1], 2), abs=1e-9)
    assert plano["risk_reward"] is not None and plano["risk_reward"]["rr"] is not None
    # PARCIAL em 1R = gatilho + (gatilho − stop) = 96 + 8 = 104 (declarada, não é o alvo)
    assert plano["parcial"]["tipo"] == "1R" and plano["parcial"]["price"] == 104.0
    assert plano["periodo"] == 20 and plano["desvios"] == 3.0


def test_plan_dict_nunca_levanta(monkeypatch):
    """O wrapper de UI engole a falha e devolve o motivo — nunca derruba a tela."""
    def _boom(*a, **k):
        raise RuntimeError("fonte fora do ar")
    monkeypatch.setattr(ps, "build_bollinger_plan", _boom)
    plano = ps.build_bollinger_plan_dict("XPTO", "2026-03-24", "1d")
    assert plano["pattern"] is None and plano["motivo"]


# ─────────────────── integração no LEDGER e na grade método×frame ────────────
def test_bollinger_e_setup_medido_mas_nao_do_card():
    """O ledger RECONHECE e MEDE o bollinger (SETUPS_MEDIDOS), mas ele NÃO sobe pro
    card de decisão (SETUPS_DO_LEDGER) — medido primeiro, decidido quando provar. Sem
    o reconhecimento, um gatilho bollinger seria lido como '123' e contaminaria a
    célula do outro setup."""
    assert "bollinger" in sc.SETUPS_MEDIDOS
    assert "bollinger" not in sc.SETUPS_DO_LEDGER, "não decide antes de ter amostra"
    assert sc._setup_da_entrada({"setup": "bollinger"}) == "bollinger"


def test_bollinger_row_traduz_o_plano_pra_celula_do_scan(monkeypatch):
    """A célula do scan: o plano do detector vira estado/gatilho/stop/alvo na linha.
    Preço no gatilho (dist ≤ tol) → ``em_gatilho``, e a parcial declarada viaja."""
    plano = {
        "pattern": {"direction": "compra", "trigger": 96.0, "state": "formando",
                    "ciclo": "nunca_acionou", "desfecho": None,
                    "extremo": {"date": "2026-03-23"}},
        "stop": {"price": 88.0}, "target": {"price": 99.7},
        "risk_reward": {"rr": 0.46, "note": None},
        "parcial": {"tipo": "1R", "price": 104.0},
    }
    monkeypatch.setattr(sc, "build_bollinger_plan_dict", lambda *a, **k: plano)
    row = sc._bollinger_row("XPTO", "2026-03-24", "1d", price=96.1)   # ~0,1% do gatilho
    assert row["estado"] == "em_gatilho" and row["direction"] == "compra"
    assert row["trigger"] == 96.0 and row["sl"] == 88.0 and row["tp"] == 99.7
    assert row["rr"] == 0.46 and row["opera"] is True
    assert row["parcial"] == 104.0, "a parcial declarada viaja na linha"


def test_bollinger_row_sem_extremo_e_sem_setup(monkeypatch):
    monkeypatch.setattr(sc, "build_bollinger_plan_dict",
                        lambda *a, **k: {"pattern": None, "motivo": "nada"})
    row = sc._bollinger_row("XPTO", "2026-03-24", "1d", price=100.0)
    assert row["estado"] == "sem_setup" and row["opera"] is False


def test_bollinger_aparece_na_grade_metodo_frame():
    """MESMA régua dos outros setups: um trade Bollinger fechado vira célula na grade
    método×frame — com o MESMO sizing (posição/risco fixos), pra comparar. É o ponto
    da task: transformar a alegação do vídeo em célula medida."""
    verdicts = [
        {"veredito": "bateu_tp", "setup": "bollinger", "frame": "1d", "ticker": "AAA",
         "direction": "compra", "trigger": 96.0, "sl": 88.0, "tp": 100.0, "rr": 0.5,
         "ts": "2026-09-04T10:00:00+00:00", "fechado_em": "2026-09-04"},
        {"veredito": "bateu_sl", "setup": "bollinger", "frame": "1d", "ticker": "BBB",
         "direction": "venda", "trigger": 104.0, "sl": 112.0, "tp": 100.0, "rr": 0.5,
         "ts": "2026-09-04T11:00:00+00:00", "fechado_em": "2026-09-04"},
        # um 123 no mesmo lote, pra provar que os setups NÃO se misturam na grade
        {"veredito": "bateu_tp", "setup": "123", "frame": "1d", "ticker": "CCC",
         "direction": "compra", "trigger": 100.0, "sl": 95.0, "tp": 110.0, "rr": 2.0,
         "ts": "2026-09-04T12:00:00+00:00", "fechado_em": "2026-09-04"},
    ]
    mf = _metodo_frame(verdicts, banca=100.0, marco="2026-09-03T00:00:00+00:00")
    bloco = mf["desde_marco"]["bollinger"]["1d"]
    assert bloco["n"] == 2 and bloco["taxa_acerto"] == 0.5
    assert bloco["banca_por_trade"] == 100.0, "MESMO sizing dos outros setups"
    # separado do 123, e presente no recorte por classe de ativo (DA-184)
    assert "123" in mf["desde_marco"] and mf["desde_marco"]["123"]["1d"]["n"] == 1
    assert mf["por_classe"]["desde_marco"]["bollinger"]["stock"]["1d"]["n"] == 2

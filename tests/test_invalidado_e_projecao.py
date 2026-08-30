"""Padrão MORTO é fantasma, e o ponto 3 tem endereço (task 20260830-013).

Pedido do Samyr: *"deve mudar a cor do 123 se invalidou (tipo um fantasma) e avisar no
card com detalhes que invalidou, se tiver em formação de 123, marcar onde deve ser a
nova formação do 3 do 123, tipo uma preparação para acompanhar a hora de entrar"*.

O estado real antes disto: ``Pattern123.state`` só conhecia ``acionado`` /
``rompeu_retracou`` / ``formando``. O nível de invalidação era calculado e desenhado,
mas **ninguém comparava o preço contra ele** — então um 1-2-3 morto seguia na tela com
a mesma cor e o mesmo peso de um vivo, e o card não dizia nada.

Dois cuidados que estes testes trancam:

  * a morte é MEDIDA e DATADA (a primeira barra que FECHA além do ponto 3), não
    inferida de vista nem do último preço — o padrão que morreu e voltou continua
    morto, porque quem volta forma OUTRO padrão;
  * a faixa do ponto 3 sai da regra do MÉTODO ABERTO, nunca a do outro: a semântica
    do ponto 2 é invertida entre os dois, e no Storm o ponto 3 é o PRÓXIMO CANDLE, não
    um swing futuro qualquer. Onde a regra não delimita, declara-se ausente.
"""

import pandas as pd
import pytest

from tradingagents.dataflows import price_structure as ps


def _df(precos, datas=None):
    """Série mínima com OHLC coerente — o detector lê máxima/mínima/fechamento."""
    n = len(precos)
    if datas is None:                      # `or` num DatetimeIndex é ambíguo
        datas = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "Date": datas,
        "Open": list(precos),
        "High": [p + 1.0 for p in precos],
        "Low": [p - 1.0 for p in precos],
        "Close": list(precos),
    })


# ────────────────────── (1) a morte é medida, e datada ─────────────────────────
@pytest.mark.unit
def test_a_invalidacao_e_a_primeira_barra_que_FECHA_alem_do_ponto_3():
    """Por fechamento e não por pavio — é a mesma régua do stop, que leva folga de
    ATR justamente pra não ser tirado por sombra."""
    df = _df([100, 100, 100, 100])
    # nível 99: o fechamento de 98 na terceira barra é o primeiro que passa
    df.loc[2, "Close"] = 98.0
    quando = ps._primeira_barra_alem(df, 0, 99.0, True, "%Y-%m-%d")
    assert quando == "2026-01-03", quando
    # e um PAVIO abaixo, sem fechamento, não mata
    df2 = _df([100, 100, 100])
    df2.loc[1, "Low"] = 90.0
    assert ps._primeira_barra_alem(df2, 0, 99.0, True, "%Y-%m-%d") is None


@pytest.mark.unit
def test_na_venda_a_morte_e_para_CIMA():
    df = _df([100, 100, 100])
    df.loc[2, "Close"] = 105.0
    assert ps._primeira_barra_alem(df, 0, 101.0, False, "%Y-%m-%d") == "2026-01-03"
    # a MESMA série, lida como compra contra um nível abaixo dos fechamentos: nada
    # morreu — a direção decide de que lado o nível é perdido
    assert ps._primeira_barra_alem(df, 0, 95.0, True, "%Y-%m-%d") is None


@pytest.mark.unit
def test_quem_morreu_e_voltou_CONTINUA_morto():
    """O ponto 3 não "desperde": o padrão morreu naquela barra. Quem volta forma
    OUTRO padrão — e é por isso que a morte não pode sair do último preço."""
    df = _df([100, 100, 100, 100])
    df.loc[1, "Close"] = 95.0        # morreu aqui
    df.loc[3, "Close"] = 110.0       # e voltou depois
    assert ps._primeira_barra_alem(df, 0, 99.0, True, "%Y-%m-%d") == "2026-01-02"


@pytest.mark.unit
def test_o_padrao_carrega_invalidado_e_a_data(monkeypatch):
    """Fim a fim no detector real: os campos existem e viajam no dict que a tela lê.
    Sem isto, o front teria que inferir a morte de vista — que é o defeito."""
    import math

    n = 160
    precos = [100 + 12 * math.sin(i / 7) + i * 0.05 for i in range(n)]
    df = _df(precos, pd.date_range("2026-01-01", periods=n, freq="D"))
    monkeypatch.setattr(ps, "load_ohlcv", lambda s, d: df.copy())
    p = ps.build_actionable_plan_dict("SYN", "2026-06-09")
    assert p["pattern"] is not None, p
    assert "invalidado" in p["pattern"] and "invalidado_em" in p["pattern"], p["pattern"]
    assert isinstance(p["pattern"]["invalidado"], bool), p["pattern"]
    # e a chave da projeção existe SEMPRE — a tela não pode ter que adivinhar se o
    # campo sumiu porque não se aplica ou porque o plano esqueceu
    assert "projecao_p3" in p, sorted(p)


# ─────────────── (4) a faixa do ponto 3 — 1-2-3 de SWINGS ─────────────────────
@pytest.mark.unit
def test_projecao_de_COMPRA_fica_entre_o_ponto_1_e_o_ponto_2():
    """A regra do detector, e nada além dela: o ponto 3 de compra é um fundo
    ASCENDENTE — acima da mínima do ponto 1 (perdê-la mata a formação) e abaixo da
    máxima do ponto 2 (acima dela já rompeu, não há recuo a esperar)."""
    df = _df([100, 90, 100, 110, 105])          # fundo em 90, topo em 110
    lows, highs = [1], [3]
    pj = ps._projecao_p3(df, lows, highs, 105.0, None)
    assert pj is not None, pj
    assert pj["direcao"] == "compra", pj
    assert pj["low"] == 89.0 and pj["high"] == 111.0, pj   # low do p1, high do p2
    assert "FUNDO acima de 89" in pj["condicao"], pj["condicao"]
    assert "mata a formação" in pj["condicao"], pj["condicao"]
    assert pj["gatilho_futuro"] == 111.0, pj
    assert pj["caso"] == "gestacao", pj


@pytest.mark.unit
def test_projecao_de_VENDA_e_o_espelho_exato():
    df = _df([100, 110, 100, 90, 95])           # topo em 110, fundo em 90
    lows, highs = [3], [1]
    pj = ps._projecao_p3(df, lows, highs, 95.0, None)
    assert pj is not None and pj["direcao"] == "venda", pj
    assert pj["low"] == 89.0 and pj["high"] == 111.0, pj
    assert "TOPO abaixo de 111" in pj["condicao"], pj["condicao"]
    assert pj["gatilho_futuro"] == 89.0, pj


@pytest.mark.unit
def test_com_padrao_VIVO_nao_ha_projecao():
    """Ali o ponto 3 já existe — o que falta é o gatilho, que a tela já marca.
    Desenhar uma faixa de espera diria que falta o que já está lá."""
    df = _df([100, 90, 110, 95, 105])
    pat = ps.Pattern123({"date": "d", "price": 89.0}, {"date": "d", "price": 111.0},
                        {"date": "d", "price": 94.0}, 111.0, "formando", "compra")
    assert ps._projecao_p3(df, [1, 3], [2], 105.0, pat) is None


@pytest.mark.unit
def test_com_padrao_MORTO_a_projecao_volta_pro_par_que_sobrou():
    """É a "preparação" depois da morte: o par 1-2 ainda pode parir OUTRO ponto 3."""
    df = _df([100, 90, 110, 95, 100])
    pat = ps.Pattern123({"date": "d", "price": 89.0}, {"date": "d", "price": 111.0},
                        {"date": "d", "price": 94.0}, 111.0, "formando", "compra",
                        invalidado=True, invalidado_em="2026-01-05")
    pj = ps._projecao_p3(df, [1, 3], [2], 100.0, pat)
    assert pj is not None and pj["caso"] == "novo_apos_invalidacao", pj
    assert pj["low"] == 89.0 and pj["high"] == 111.0, pj


@pytest.mark.unit
def test_ponto_1_perdido_DECLARA_ausencia_em_vez_de_desenhar_chute():
    """O critério explícito: quando a regra não delimita, declara-se ausente.
    Desenhar a faixa mesmo assim seria inventar o nível mais perigoso da tela — o
    que diz "compre aqui"."""
    df = _df([100, 90, 110, 95, 80])
    pat = ps.Pattern123({"date": "d", "price": 89.0}, {"date": "d", "price": 111.0},
                        {"date": "d", "price": 94.0}, 111.0, "formando", "compra",
                        invalidado=True, invalidado_em="2026-01-05")
    pj = ps._projecao_p3(df, [1, 3], [2], 80.0, pat)     # preço abaixo do ponto 1
    assert pj is not None and pj["low"] is None, pj
    assert "ponto 1" in pj["motivo"] and "perdido" in pj["motivo"], pj["motivo"]


@pytest.mark.unit
def test_sem_par_de_swings_nao_ha_o_que_projetar():
    df = _df([100, 101, 102])
    assert ps._projecao_p3(df, [], [], 102.0, None) is None


# ───────────────── (4) a faixa do ponto 3 — STORM, outra regra ────────────────
@pytest.mark.unit
def test_a_projecao_do_STORM_e_do_PROXIMO_CANDLE_e_nao_de_um_swing():
    """O Storm lê TRÊS CANDLES CONSECUTIVOS: o ponto 3 é o candle seguinte ao fundo,
    e a condição é fechar acima do fechamento do ponto 2 FALHANDO em romper a máxima
    do ponto 1. Usar a régua do outro método poria na tela uma espera que a regra
    deste não sustenta."""
    df = _df([100, 95])                    # candle 1 lateral/alta, candle 2 é o fundo
    df.loc[0, "Open"], df.loc[0, "Close"] = 99.0, 100.0     # alta
    pj = ps._projecao_storm(df, "%Y-%m-%d")
    assert pj is not None and pj["direcao"] == "compra", pj
    assert pj["low"] == 95.0, ("o piso é o FECHAMENTO do ponto 2", pj)
    assert pj["high"] == 101.0, ("o teto é a MÁXIMA do ponto 1", pj)
    assert "PRÓXIMO candle" in pj["quando"], pj
    assert "FECHAR acima" in pj["condicao"] and "NÃO romper" in pj["condicao"], pj["condicao"]
    assert pj["gatilho_futuro"] is None, ("no Storm o gatilho só nasce com o ponto 3", pj)


@pytest.mark.unit
def test_a_projecao_do_STORM_de_venda_e_o_espelho():
    df = _df([100, 105])
    df.loc[0, "Open"], df.loc[0, "Close"] = 101.0, 100.0     # baixa
    pj = ps._projecao_storm(df, "%Y-%m-%d")
    assert pj is not None and pj["direcao"] == "venda", pj
    assert pj["high"] == 105.0 and pj["low"] == 99.0, pj
    assert "FECHAR abaixo" in pj["condicao"], pj["condicao"]


@pytest.mark.unit
def test_sem_o_comeco_do_padrao_o_STORM_nao_projeta_nada():
    """O caso comum, e dizer nada é melhor que desenhar espera pra um setup que não
    está nascendo."""
    df = _df([100, 101])
    df.loc[0, "Open"], df.loc[0, "Close"] = 99.0, 100.0
    assert ps._projecao_storm(df, "%Y-%m-%d") is None       # o candle 2 não é fundo


@pytest.mark.unit
def test_as_duas_projecoes_NAO_produzem_a_mesma_faixa():
    """O ponto do critério: a regra é do método aberto, nunca a do outro. Com a MESMA
    série as duas dão faixas diferentes — se dessem a mesma, uma delas estaria usando
    a régua alheia."""
    df = _df([100, 95, 100, 110, 105])
    df.loc[3, "Open"], df.loc[3, "Close"] = 109.0, 110.0
    swings = ps._projecao_p3(df, [1], [3], 105.0, None)
    storm = ps._projecao_storm(df, "%Y-%m-%d")
    if swings and storm:
        assert (swings["low"], swings["high"]) != (storm["low"], storm["high"]), (
            swings, storm)


# ─────────────── a lista e a análise não podem discordar da morte ─────────────
@pytest.mark.unit
def test_o_scan_le_a_morte_do_DETECTOR_e_nao_recalcula(monkeypatch):
    """Duas definições de "invalidado" fariam a lista dizer "em movimento" sobre o
    mesmo padrão que a análise desenha como FANTASMA. O caso que separa as duas: o
    padrão morreu e o preço voltou — pela conta local (só o último preço) ele
    ressuscita; pelo detector, continua morto, que é a verdade estrutural."""
    from tradingagents.webui import scanner as sc

    plano = {
        "price": 100.0, "setup_state": "aguardar_rompimento",
        # morreu lá atrás (fechou abaixo do ponto 3) e o preço VOLTOU pra cima dele
        "pattern": {"trigger": 110.0, "state": "acionado", "direction": "compra",
                    "invalidado": True, "invalidado_em": "2026-08-20"},
        "invalidation": {"price": 95.0}, "stop": {"price": 92.0},
        "target": {"price": 130.0, "low": None, "high": None},
        "risk_reward": {"rr": 1.2, "note": None, "entry": 110.0,
                        "entry_basis": "gatilho", "risk": 18.0, "reward": 20.0},
    }
    monkeypatch.setattr(sc, "build_actionable_plan_dict", lambda *a, **k: plano)
    monkeypatch.setattr(sc, "build_storm_plan_dict", lambda *a, **k: {"pattern": None})
    monkeypatch.setattr(sc, "_live_price", lambda *_a, **_k: None)
    linha = sc._frame_row("X", "2026-08-29", "1d", live_price=100.0)
    assert linha["estado"] == "invalidou", ("o preço voltou, mas o padrão morreu", linha)

    # e o plano ANTIGO (cache sem o campo) continua legível pela conta local
    velho = {**plano, "pattern": {k: v for k, v in plano["pattern"].items()
                                  if k not in ("invalidado", "invalidado_em")}}
    monkeypatch.setattr(sc, "build_actionable_plan_dict", lambda *a, **k: velho)
    antiga = sc._frame_row("X", "2026-08-29", "1d", live_price=100.0)
    assert antiga["estado"] != "invalidou", ("preço 100 acima da invalidação 95", antiga)

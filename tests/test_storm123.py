"""Setup 1-2-3 STORM (Alexandre Wolwacz) + filtro Éden dos Traders — task 022.

NÃO é variação do 1-2-3 que este projeto já tinha. É OUTRO padrão, com a MESMA
NUMERAÇÃO significando coisas DIFERENTES — e é exatamente por isso que este arquivo
existe: cada teste aqui é um DENTE contra a confusão entre os dois.

                      1-2-3 deste projeto           1-2-3 STORM
  pontos              swings confirmados (k=5)      3 CANDLES consecutivos
  ponto 2 (compra)    o TOPO do repique             o FUNDO (menor mínima dos 3)
  ponto 3 (compra)    fundo ASCENDENTE acima do p1  recuperação que FALHA em romper
                                                      a máxima do ponto 1
  stop                ponto 3 + folga de ATR        o PONTO 2
  alvo                swing anterior mais próximo   PROJEÇÃO DA AMPLITUDE dos 3
  filtro              nenhum                        ÉDEN (MME 8 × MME 80) — VETO

A semântica do ponto 2 está literalmente INVERTIDA entre os dois.

Task 023: o Storm tem DUAS ENTRADAS do MESMO padrão — a spec escreve "rompimento da
máxima do ponto 2 (ou 3)". Mesmos p1/p2/p3, mesmo stop, mesma amplitude; muda só o
GATILHO, e com ele o alvo (projetado dele) e o R:R (medido dele até o mesmo stop). A
022 colapsava as duas na mais conservadora — e colapsar escondia justamente a que
entra antes.
"""

import pandas as pd
import pytest

from tradingagents.dataflows import price_structure as ps


def _df(rows):
    d = pd.DataFrame(rows)
    d["Date"] = pd.to_datetime(d["Date"])
    return d


def _c(dia, o, h, lo, c):
    return {"Date": f"2026-01-{dia:02d}", "Open": o, "High": h, "Low": lo, "Close": c}


# Trio canônico de COMPRA:
#   1) candle de alta (100 → 108), máxima 110
#   2) o FUNDO: mínima 90, menor que a do 1 (99) e a do 3 (92)
#   3) recuperação (fecha 104 > 92) que FALHA em romper o ponto 1 (105 < 110)
_COMPRA = [_c(1, 100, 110, 99, 108), _c(2, 107, 108, 90, 92), _c(3, 93, 105, 92, 104)]
# Espelho de VENDA
_VENDA = [_c(1, 110, 111, 100, 101), _c(2, 102, 120, 101, 118), _c(3, 117, 118, 104, 106)]


# --------------------------------------------------------------- o detector ----
def test_ponto_2_e_o_FUNDO_e_nao_o_topo_do_repique():
    """O dente central: no 1-2-3 deste projeto o ponto 2 de uma compra é o TOPO
    entre os dois fundos; no Storm ele é O FUNDO. Trocar os dois é o jeito mais
    fácil de estragar isto — aqui o preço do ponto 2 é a MENOR mínima dos três."""
    pat = ps._storm_123(_df(_COMPRA))
    assert pat is not None
    assert pat.direction == "compra"
    assert pat.p2["low"] == 90.0
    assert pat.p2["price"] == 90.0, "o ponto 2 vale pela MÍNIMA na compra"
    assert pat.p2["low"] == min(c["Low"] for c in _COMPRA), "é a menor mínima dos 3"
    # e o ponto 1 vale pela MÁXIMA — é o teto que o ponto 3 tem que falhar em romper
    assert pat.p1["price"] == 110.0


def test_ponto_3_tem_que_FALHAR_em_romper_o_ponto_1():
    """Sem a falha não há Storm: é ela que diz que a recuperação não retomou a
    tendência anterior. Ponto 3 rompendo a máxima do ponto 1 → nenhum padrão."""
    rompe = [_COMPRA[0], _COMPRA[1], _c(3, 93, 112, 92, 111)]   # 112 > 110
    assert ps._storm_123(_df(rompe)) is None
    # e o limite: exatamente NA máxima do ponto 1 também não vale (não falhou)
    limite = [_COMPRA[0], _COMPRA[1], _c(3, 93, 110, 92, 108)]
    assert ps._storm_123(_df(limite)) is None


def test_ponto_3_tem_que_ser_recuperacao_nao_continuacao():
    """Fechar ABAIXO do ponto 2 é continuação da queda, não recuperação."""
    cai = [_COMPRA[0], _COMPRA[1], _c(3, 93, 105, 91, 90.5)]   # fecha 90,5 < 92
    assert ps._storm_123(_df(cai)) is None


def test_o_fundo_tem_que_ser_do_ponto_2_e_nao_do_ponto_3():
    """Se o ponto 3 faz mínima menor, o fundo é ele — e aí não é este padrão."""
    fundo_no_3 = [_COMPRA[0], _COMPRA[1], _c(3, 93, 105, 88, 104)]
    assert ps._storm_123(_df(fundo_no_3)) is None


def test_ponto_1_de_baixa_nao_serve_pra_compra():
    """A spec pede candle de alta OU lateral no ponto 1."""
    p1_baixa = [_c(1, 110, 110, 99, 100), _COMPRA[1], _COMPRA[2]]
    pat = ps._storm_123(_df(p1_baixa))
    assert pat is None or pat.direction != "compra"


def test_venda_e_o_espelho_exato_com_o_ponto_2_no_TOPO():
    pat = ps._storm_123(_df(_VENDA))
    assert pat is not None and pat.direction == "venda"
    assert pat.p2["high"] == 120.0 and pat.p2["price"] == 120.0
    assert pat.p2["high"] == max(c["High"] for c in _VENDA)
    assert pat.p1["price"] == 100.0, "o ponto 1 vale pela MÍNIMA na venda"


def _entradas(rows):
    return {e["entrada"]: e for e in ps._storm_123(_df(rows)).entradas}


def test_sao_DUAS_entradas_do_mesmo_padrao_nunca_um_gatilho_so():
    """A spec escreve "máxima do ponto 2 (ou 3)": são dois PONTOS DE ENTRADA do
    mesmo padrão. Colapsá-los num número (o que a 022 fazia, pegando o mais
    conservador) esconde a leitura que entra antes — que é justamente a que muda a
    conta do risco."""
    e = _entradas(_COMPRA)
    assert set(e) == {"ponto2", "ponto3"}, e
    assert e["ponto2"]["trigger"] == _COMPRA[1]["High"] == 108.0
    assert e["ponto3"]["trigger"] == _COMPRA[2]["High"] == 105.0
    # na venda os gatilhos são as MÍNIMAS
    ev = _entradas(_VENDA)
    assert ev["ponto2"]["trigger"] == _VENDA[1]["Low"]
    assert ev["ponto3"]["trigger"] == _VENDA[2]["Low"]


def test_a_ANTECIPADA_e_a_que_o_preco_alcanca_primeiro():
    """Qual é qual não é rótulo decorativo: numa compra é o gatilho mais BAIXO que
    dispara antes (na venda, o mais alto). É o que sustenta a frase "entra antes da
    confirmação"."""
    e = _entradas(_COMPRA)               # p3=105 < p2=108
    assert e["ponto3"]["ordem"] == "antecipada", e
    assert e["ponto2"]["ordem"] == "confirmada", e
    # invertendo os níveis, inverte quem é quem — sai do DADO, não do nome
    alto3 = [_COMPRA[0], _COMPRA[1], _c(3, 93, 109, 92, 108)]
    e2 = _entradas(alto3)                # p3=109 > p2=108
    assert e2["ponto2"]["ordem"] == "antecipada", e2
    assert e2["ponto3"]["ordem"] == "confirmada", e2
    # e na VENDA o mais alto é o que dispara antes
    ev = _entradas(_VENDA)               # p2=101 > p3=104? não: p2=101, p3=104
    assert ev["ponto3"]["ordem"] == "antecipada", ev


def test_entradas_no_MESMO_nivel_viram_UMA_leitura_e_a_tela_diz_isso():
    """Dois gatilhos que a tela mostra iguais não são duas leituras: repetir o mesmo
    número com dois nomes é a duplicata que a DA-077 proíbe. Comparação na precisão
    PUBLICADA (DA-072)."""
    igual = [_COMPRA[0], _COMPRA[1], _c(3, 93, 108, 92, 104)]   # h3 == h2 == 108
    ent = ps._storm_123(_df(igual)).entradas
    assert len(ent) == 1, ent
    assert ent[0]["entrada"] == "ponto2e3" and ent[0]["ordem"] == "unica", ent
    assert "mesmo nível" in ent[0]["label"], ent


def test_amplitude_e_a_maior_maxima_menos_a_menor_minima_dos_TRES():
    pat = ps._storm_123(_df(_COMPRA))
    esperado = max(c["High"] for c in _COMPRA) - min(c["Low"] for c in _COMPRA)
    assert pat.amplitude == round(esperado, 2) == 20.0


def test_o_triplo_mais_recente_vence():
    """Duas formações na série: vale a última — o setup é sobre o agora."""
    velho = [_c(1, 100, 110, 99, 108), _c(2, 107, 108, 90, 92), _c(3, 93, 105, 92, 104)]
    meio = [_c(4, 100, 106, 99.5, 105), _c(5, 104, 105, 96, 97), _c(6, 97, 103, 96.5, 101)]
    pat = ps._storm_123(_df(velho + meio))
    assert pat is not None
    assert pat.p3["date"] == "2026-01-06", pat.as_dict()


# ------------------------------------------------------------------- níveis ----
def _plano_compra(atr=4.0, price=104.0):
    pat = ps._storm_123(_df(_COMPRA))
    inval, stop, leituras = ps._storm_levels(pat, atr, price)
    return pat, inval, stop, {L["entrada"]: L for L in leituras}


def test_stop_fica_no_PONTO_2_e_nunca_no_ponto_3():
    """No 1-2-3 deste projeto o stop se ancora no ponto 3. No Storm, no ponto 2 —
    e é o ponto 2 EXATO: a folga de meio ATR do outro setup derruba a mediana de
    R:R de 1,13 pra 0,80 medida na watchlist real, porque meio ATR14 é enorme perto
    da amplitude de TRÊS candles."""
    pat, inval, stop, _leituras = _plano_compra()
    assert stop["anchor"] == pat.p2["low"] == 90.0
    assert stop["anchor"] != pat.p3["low"], "ancorar no ponto 3 é o OUTRO setup"
    assert stop["price"] == 90.0
    assert all(stop["price"] < e["trigger"] for e in pat.entradas)
    assert stop["slack"] == 0.0
    assert "ponto 2" in stop["basis"]
    # a invalidação é o mesmo nível estrutural, com a frase que diz o que ele é
    assert inval["price"] == 90.0 and "ponto 2" in inval["label"]


def test_alvo_e_a_projecao_da_amplitude_a_partir_do_gatilho():
    """Não é o swing anterior mais próximo (o alvo do outro setup): é a amplitude
    dos 3 candles LANÇADA do gatilho. Ancorar no gatilho e não no preço de agora é
    o que mantém o alvo um nível estrutural em vez de fugir junto com o preço."""
    pat, _i, _s, L = _plano_compra()
    # CADA leitura projeta do SEU gatilho — é isso que faz o alvo do ponto 3 ficar
    # mais perto, com o mesmo stop, e portanto o R:R melhor.
    assert L["ponto2"]["target"]["price"] == round(108.0 + pat.amplitude, 2) == 128.0
    assert L["ponto3"]["target"]["price"] == round(105.0 + pat.amplitude, 2) == 125.0
    assert L["ponto2"]["target"]["amplitude"] == 20.0
    assert "amplitude" in L["ponto2"]["target"]["label"]
    # venda: espelhado para baixo
    pv = ps._storm_123(_df(_VENDA))
    _i2, _s2, lv = ps._storm_levels(pv, 4.0, 106.0)
    for le in lv:
        assert le["target"]["price"] == round(le["trigger"] - pv.amplitude, 2)


def test_alvo_nao_se_move_com_o_preco_depois_de_acionado():
    """Projetar do preço corrente faria o alvo fugir e nunca ser atingido."""
    pat = ps._storm_123(_df(_COMPRA))
    a = [L["target"]["price"] for L in ps._storm_levels(pat, 4.0, 104.0)[2]]
    b = [L["target"]["price"] for L in ps._storm_levels(pat, 4.0, 999.0)[2]]
    assert a == b


def test_risco_retorno_sai_dos_niveis_reais():
    _pat, _i, stop, L = _plano_compra()
    for le in L.values():
        rr = le["risk_reward"]
        assert rr["entry"] == le["trigger"]
        assert rr["risk"] == round(le["trigger"] - stop["price"], 2)
        assert rr["reward"] == round(le["target"]["price"] - le["trigger"], 2)
        assert rr["rr"] == round(rr["reward"] / rr["risk"], 2)


def test_gatilho_mais_perto_com_o_MESMO_stop_da_R_R_melhor():
    """A consequência ARITMÉTICA das duas entradas, que é o ponto todo delas: o
    stop é o mesmo (o ponto 2), então o gatilho mais próximo tem risco menor — e
    como o alvo é a MESMA amplitude lançada de um ponto mais baixo, o retorno é
    igual. Risco menor com retorno igual = R:R melhor, ao custo de entrar antes da
    confirmação. Medido na watchlist real: mediana 1,44 (ponto 3) × 1,14 (ponto 2)."""
    _pat, _i, stop, L = _plano_compra()
    p2, p3 = L["ponto2"], L["ponto3"]
    assert p3["trigger"] < p2["trigger"], "no fixture o ponto 3 é o antecipado"
    assert p3["risk_reward"]["risk"] < p2["risk_reward"]["risk"], "mesmo stop, gatilho mais perto"
    assert p3["risk_reward"]["reward"] == p2["risk_reward"]["reward"], "mesma amplitude"
    assert p3["risk_reward"]["rr"] > p2["risk_reward"]["rr"]
    # e o stop é UM só: as duas entradas são do MESMO padrão
    assert stop["price"] == 90.0


# -------------------------------------------------------------------- Éden -----
def _serie_eden(n=200, subindo=True, preco_final=None):
    """Série longa o bastante pra MME 80 significar alguma coisa."""
    rows = []
    p = 100.0
    for i in range(n):
        p = p * (1.004 if subindo else 0.996)
        rows.append({"Date": pd.Timestamp("2025-01-01") + pd.Timedelta(days=i),
                     "Open": p, "High": p * 1.005, "Low": p * 0.995, "Close": p})
    if preco_final is not None:
        rows[-1]["Close"] = preco_final
        rows[-1]["High"] = max(rows[-1]["High"], preco_final)
        rows[-1]["Low"] = min(rows[-1]["Low"], preco_final)
    d = pd.DataFrame(rows)
    close = d["Close"].astype(float)
    for w in (ps._STORM_EMA_RAPIDA, ps._STORM_EMA_LENTA):
        d[f"EMA{w}"] = close.ewm(span=w, adjust=False).mean()
    return d


def test_eden_de_compra_exige_rapida_acima_da_lenta_E_preco_acima_das_duas():
    e = ps._eden(_serie_eden(subindo=True))
    assert e["disponivel"] and e["alinhado"] and e["direcao"] == "compra"
    assert e["ema_rapida"] > e["ema_lenta"] and e["preco"] > e["ema_rapida"]


def test_eden_de_venda_e_o_espelho():
    e = ps._eden(_serie_eden(subindo=False))
    assert e["alinhado"] and e["direcao"] == "venda"
    assert e["ema_rapida"] < e["ema_lenta"] and e["preco"] < e["ema_rapida"]


def _poe_candle(d, low, high):
    """Move o ÚLTIMO candle inteiro pra uma posição escolhida.

    Depois da task 016 o Éden compara o CANDLE com a média (proporção do range), não
    o fechamento com a média — então mexer só no Close deixaria a barra na posição
    antiga e o teste mediria outra coisa."""
    i = d.index[-1]
    d.loc[i, "Low"], d.loc[i, "High"] = low, high
    d.loc[i, "Open"] = d.loc[i, "Close"] = (low + high) / 2
    return d


def test_sem_alinhamento_o_eden_VETA_e_nao_apenas_desconta():
    """Tendência de alta, mas o CANDLE caiu abaixo das duas médias: não é Éden de
    compra (o candle não está acima das duas) nem de venda (a rápida segue acima da
    lenta). O resultado é VETO, não um sinal mais fraco."""
    d = _serie_eden(subindo=True)
    lenta = float(d["EMA80"].iloc[-1])
    _poe_candle(d, lenta * 0.88, lenta * 0.92)      # o candle INTEIRO abaixo das duas
    e = ps._eden(d)
    assert not e["alinhado"] and e["direcao"] is None
    assert not e["zona_neutra"], ("abaixo das DUAS não é a região entre elas", e)
    assert e["motivo"]


def test_o_candle_entre_as_medias_e_ZONA_NEUTRA_e_diz_isso():
    """Candle acima da MME 8 e ABAIXO da MME 80, em tendência de baixa: é a região
    que o Stormer chama de ZONA NEUTRA — "operar aqui é muito mais perigoso".

    ANTES da task 016 isto caía no balaio genérico do "sem Éden", e a tela dizia só
    "desalinhado" no caso mais caro. Agora é um TERCEIRO estado, com nome e frase; o
    que se faz com ele depende da direção do padrão, e quem decide é a qualidade."""
    d = _serie_eden(subindo=False)
    rapida, lenta = float(d["EMA8"].iloc[-1]), float(d["EMA80"].iloc[-1])
    assert rapida < lenta
    meio = (rapida + lenta) / 2
    _poe_candle(d, meio * 0.999, meio * 1.001)      # o candle inteiro entre as duas
    e = ps._eden(d)
    assert not e["alinhado"] and e["zona_neutra"] is True, e
    assert "ZONA NEUTRA" in e["motivo"], e["motivo"]
    assert "muito mais perigoso" in e["motivo"], e["motivo"]
    assert e["direcao_estrutural"] == "venda", e
    # e o nome ARMADILHA continua existindo — agora no VETO da qualidade, porque ele
    # só faz sentido contra uma DIREÇÃO de padrão (o mesmo lugar é recuo pra um lado)
    q = ps._storm_qualidade(_pat_compra(), e, None)
    assert q["opera"] is False and "ARMADILHA" in (q["veto"] or ""), q


def test_serie_curta_nao_finge_ter_MME_80():
    """A EMA recursiva devolve número desde a 1ª barra: uma MME 80 lida com 30
    candles PARECE média de 80 períodos e não é. Isso é indisponibilidade
    declarada, nunca um Éden inventado."""
    e = ps._eden(_serie_eden(n=30))
    assert e["disponivel"] is False and e["alinhado"] is False
    assert "80" in e["motivo"]


# --------------------------------------------------------------- qualidade ----
def _pat_compra():
    return ps._storm_123(_df(_COMPRA))


def _pat_venda():
    return ps._storm_123(_df(_VENDA))


def test_sem_eden_a_qualidade_e_ruim_e_o_veto_esta_escrito():
    q = ps._storm_qualidade(_pat_compra(),
                            {"alinhado": False, "direcao": None, "motivo": "sem Éden"}, None)
    assert q["qualidade"] == "ruim" and q["opera"] is False
    assert q["veto"] and "não opera" in q["veto"] or "sem Éden" in q["veto"]


def test_padrao_contra_o_eden_nao_opera():
    """1-2-3 de compra sob Éden de VENDA é o trade contra a tendência principal que
    a regra proíbe — e o motivo diz isso, não "qualidade baixa"."""
    q = ps._storm_qualidade(_pat_compra(),
                            {"alinhado": True, "direcao": "venda", "motivo": "..."}, 95.0)
    assert q["qualidade"] == "ruim" and q["opera"] is False
    assert "contra Éden" in q["veto"]


def test_perfeita_exige_o_ponto_3_inteiro_do_lado_certo_da_MME_80():
    eden = {"alinhado": True, "direcao": "compra", "motivo": "..."}
    # ponto 3 inteiro acima da lenta (mínima 92 > 80) -> perfeita
    assert ps._storm_qualidade(_pat_compra(), eden, 80.0)["qualidade"] == "perfeita"
    # a lenta cortando o ponto 3 (mínima 92 < 95) -> boa, e opera do mesmo jeito
    boa = ps._storm_qualidade(_pat_compra(), eden, 95.0)
    assert boa["qualidade"] == "boa" and boa["opera"] is True


@pytest.mark.parametrize("qualidade,opera", [("perfeita", True), ("boa", True), ("ruim", False)])
def test_so_perfeita_e_boa_operam(qualidade, opera):
    """A regra da spec, travada: ruim nunca opera."""
    eden_ok = {"alinhado": True, "direcao": "compra", "motivo": "..."}
    eden_veto = {"alinhado": False, "direcao": None, "motivo": "sem Éden"}
    if qualidade == "ruim":
        q = ps._storm_qualidade(_pat_compra(), eden_veto, None)
    else:
        q = ps._storm_qualidade(_pat_compra(), eden_ok, 80.0 if qualidade == "perfeita" else 95.0)
    assert q["qualidade"] == qualidade and q["opera"] is opera


# ------------------------------------------------------------------ o plano ----
def test_plano_sem_padrao_ainda_declara_o_eden():
    """"Por que não opera" é informação: sumir com ela seria a tela ficar muda
    justamente no caso em que o filtro fez o seu trabalho."""
    q = ps._storm_qualidade(None, {"alinhado": True, "direcao": "compra"}, None)
    assert q["qualidade"] is None and q["opera"] is False and q["veto"] is None
    assert "nenhum" in q["motivo"].lower()


def test_wrapper_de_ui_nunca_levanta(monkeypatch):
    def explode(*_a, **_k):
        raise RuntimeError("fonte fora do ar")
    monkeypatch.setattr(ps, "build_storm_plan", explode)
    out = ps.build_storm_plan_dict("XYZ", "2026-08-29")
    assert out["pattern"] is None and out["opera"] is False
    assert out["eden"]["disponivel"] is False


def test_a_MME_80_do_eden_existe_na_serie_preparada():
    """A MME 80 não existia no projeto (_EMA_WINDOWS era 8/21/50) — sem ela o Éden
    seria um filtro sem a sua metade lenta."""
    assert ps._STORM_EMA_LENTA == 80
    assert ps._STORM_EMA_RAPIDA == 8


def test_a_MME_80_so_e_DESENHADA_no_metodo_storm():
    """Acrescentá-la a todos os métodos poria uma linha a mais em telas que não a
    usam pra nada."""
    assert 80 not in ps._chart_emas("padrao")
    assert 80 not in ps._chart_emas("erick")
    assert 80 in ps._chart_emas("storm")


# ─────────── ZONA NEUTRA + "acima da média" por proporção (task 016) ──────────
@pytest.mark.unit
@pytest.mark.parametrize("high,low,media,esperado,nome", [
    (110.0, 105.0, 100.0, 1.0, "candle inteiro acima"),
    (110.0, 100.0, 102.0, 0.8, "maioria acima — o caso que MUDA"),
    (110.0, 100.0, 108.0, 0.2, "maioria abaixo"),
    (110.0, 100.0, 105.0, 0.5, "cortado exatamente ao meio"),
    (110.0, 100.0, 100.0, 1.0, "só encosta na média por baixo"),
    (110.0, 100.0, 120.0, 0.0, "inteiro abaixo"),
])
def test_a_fracao_acima_e_do_RANGE_do_candle(high, low, media, esperado, nome):
    """A regra nova: "acima da média" deixou de ser um PONTO contra um nível e virou
    a PROPORÇÃO do candle. Medir pelo RANGE (máxima−mínima) e não pelo corpo é
    interpretação declarada — o corpo ignoraria pavios que são metade da barra."""
    assert ps._fracao_acima(high, low, media) == pytest.approx(esperado), nome


@pytest.mark.unit
def test_o_candle_com_MAIORIA_acima_conta_como_acima():
    """É o caso que a regra antiga errava: fechamento abaixo da média com 80% do
    candle acima dela saía como "abaixo"."""
    assert ps._candle_acima(110.0, 100.0, 102.0) is True
    assert ps._candle_abaixo(110.0, 100.0, 102.0) is False


@pytest.mark.unit
def test_o_EMPATE_exato_nao_e_nem_acima_nem_abaixo():
    """O desempate, definido e declarado: num filtro que AUTORIZA trade, meio a meio
    não autoriza. Os dois testes dão falso de propósito — a alternativa seria um
    critério que decide por arredondamento."""
    assert ps._candle_acima(110.0, 100.0, 105.0) is False
    assert ps._candle_abaixo(110.0, 100.0, 105.0) is False


@pytest.mark.unit
def test_quem_DECIDE_e_o_fechamento_a_proporcao_so_informa():
    """CORREÇÃO DE RUMO (task 017), medida contra uma implementação de referência: a
    proporção do candle e o fechamento COINCIDEM na prática, mas o critério operante
    é o FECHAMENTO contra as duas médias — determinístico e corroborado.

    Este é o caso que os separa: 80% do candle acima da MME 8, mas o fechamento
    ABAIXO dela. A proporção diria "alinhado"; o Éden diz que não. A fração continua
    MEDIDA e publicada (é a leitura visual, e se a evidência virar o número já está
    lá), mas não autoriza nada."""
    d = _serie_eden(subindo=True)
    rapida, lenta = float(d["EMA8"].iloc[-1]), float(d["EMA80"].iloc[-1])
    baixo = rapida - (rapida - lenta) * 0.05
    alto = rapida + (rapida - lenta) * 0.20
    _poe_candle(d, baixo, alto)
    d.loc[d.index[-1], "Close"] = rapida * 0.999      # fechamento ABAIXO da rápida
    e = ps._eden(d)
    assert e["fracao_acima_rapida"] > 0.5, ("a proporção continua medida", e)
    assert e["alinhado"] is False, ("mas quem decide é o fechamento", e)
    # e o mesmo candle com o fechamento ACIMA da rápida alinha
    d.loc[d.index[-1], "Close"] = rapida * 1.001
    assert ps._eden(d)["alinhado"] is True


@pytest.mark.unit
def test_na_zona_neutra_a_FAVOR_das_medias_o_setup_OPERA_com_aviso():
    """O terceiro estado, e o ponto dele: não é veto. "Operar aqui é muito mais
    perigoso" vira qualidade REBAIXADA com a frase escrita — o setup vale menos e
    exige seletividade extra, mas a tela não o esconde."""
    d = _serie_eden(subindo=False)
    rapida, lenta = float(d["EMA8"].iloc[-1]), float(d["EMA80"].iloc[-1])
    meio = (rapida + lenta) / 2
    _poe_candle(d, meio * 0.999, meio * 1.001)
    e = ps._eden(d)
    assert e["zona_neutra"] and e["direcao_estrutural"] == "venda", e
    q = ps._storm_qualidade(_pat_venda(), e, None)
    assert q["qualidade"] == "neutra", q
    assert q["opera"] is True, ("não é veto automático — é aviso", q)
    assert q["veto"] is None, q
    assert "muito mais perigoso" in q["motivo"], q["motivo"]
    assert "seletividade extra" in q["motivo"], q["motivo"]


@pytest.mark.unit
def test_na_zona_neutra_CONTRA_as_medias_continua_veto():
    """O outro lado, e é o que impede a regra nova de virar afrouxamento: o MESMO
    lugar do gráfico é recuo saudável a favor da tendência e ARMADILHA contra ela."""
    d = _serie_eden(subindo=False)
    rapida, lenta = float(d["EMA8"].iloc[-1]), float(d["EMA80"].iloc[-1])
    meio = (rapida + lenta) / 2
    _poe_candle(d, meio * 0.999, meio * 1.001)
    q = ps._storm_qualidade(_pat_compra(), ps._eden(d), None)
    assert q["opera"] is False and q["qualidade"] == "ruim", q
    assert "ARMADILHA" in q["veto"] and "repique" in q["veto"], q["veto"]


@pytest.mark.unit
def test_zona_neutra_NAO_e_a_mesma_coisa_que_sem_eden():
    """A distinção que a task pediu: hoje os dois caíam no mesmo balaio."""
    d = _serie_eden(subindo=True)
    lenta = float(d["EMA80"].iloc[-1])
    _poe_candle(d, lenta * 0.88, lenta * 0.92)     # abaixo das DUAS, em alta
    fora = ps._eden(d)
    assert fora["zona_neutra"] is False and fora["alinhado"] is False, fora
    assert "ZONA NEUTRA" not in fora["motivo"], fora["motivo"]

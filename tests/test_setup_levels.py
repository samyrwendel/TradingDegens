"""Onde INVALIDA, onde é o STOP e onde é o ALVO — o 1-2-3 tem que virar trade.

Queixa do dono (28/08): "vejo muito o 1-2-3 de compra e venda, mas não mostra onde
invalida e onde é o TP e o SL". O padrão sozinho é meia informação: sem o nível que
o mata, sem o stop e sem o alvo não dá para dimensionar risco nenhum.

Estes testes prendem a derivação determinística de :func:`_pattern_levels` sobre
séries sintéticas de estrutura CONHECIDA, offline, e cobrem as três regras duras:

* todo nível é preço real da série (ponto 3, swing anterior) — nunca percentual
  chutado nem número arredondado para parecer preciso;
* o 1-2-3 de VENDA usa o seu próprio lado da estrutura (topo que invalida, fundo
  anterior como alvo) — jamais o esqueleto invertido de um long;
* sem base, o nível é ``None`` e a tela diz "sem nível definido".
"""
import pandas as pd
import pytest

from tradingagents.dataflows import price_structure as ps


def _mk(closes: list[float]) -> pd.DataFrame:
    dates = pd.bdate_range("2025-01-01", periods=len(closes))
    c = pd.Series(closes, dtype=float)
    return pd.DataFrame({
        "Date": dates.strftime("%Y-%m-%d"),
        "Open": c.shift(1).fillna(c).values,
        "High": (c * 1.01).values,
        "Low": (c * 0.99).values,
        "Close": c.values,
        "Volume": [1000] * len(c),
    })


def _long_frame() -> pd.DataFrame:
    """Alta até um topo em 180, queda, e um 1-2-3 de COMPRA ainda em formação
    (L 124 → H 150 → L 136, preço parado abaixo do gatilho). O topo de 180 fica
    acima do gatilho, então existe alvo real à frente da entrada."""
    closes: list[float] = []
    closes += [100 + i for i in range(60)]
    closes += [164, 170, 176, 180, 176, 170, 164, 158, 152, 146, 140, 134, 128, 124]
    closes += [126, 132, 138, 144, 148, 150, 146, 140, 136, 138]
    closes += [140, 142, 144, 146, 145, 144, 146, 145, 147, 146]
    return _mk(closes)


def _short_frame() -> pd.DataFrame:
    """Queda até um fundo em 120, repique, e um 1-2-3 de VENDA já acionado
    (H 170 → L 140 → H 158, rompeu para baixo). O fundo de 120 fica abaixo do
    preço, então existe alvo real (fundo anterior) para o short."""
    closes: list[float] = []
    closes += [200 - i for i in range(60)]
    closes += [136, 130, 124, 120, 124, 130, 136, 142, 148, 154, 160, 166, 170]
    closes += [166, 160, 152, 146, 142, 140, 144, 150, 156, 158]
    closes += [154, 148, 142, 136, 132, 128, 130, 133, 131, 130]
    return _mk(closes)


CURR = "2026-12-31"


@pytest.fixture
def longf(monkeypatch):
    df = _long_frame()
    monkeypatch.setattr(ps, "load_ohlcv", lambda s, d: df.copy())
    return df


@pytest.fixture
def shortf(monkeypatch):
    df = _short_frame()
    monkeypatch.setattr(ps, "load_ohlcv", lambda s, d: df.copy())
    return df


# ------------------------------------------------------------------ compra ----
@pytest.mark.unit
def test_compra_invalida_no_ponto_3(longf):
    """A invalidação de um 1-2-3 de compra é PERDER o ponto 3 — o fundo ascendente
    que sustenta o padrão — e vem com a frase do que aquilo significa."""
    p = ps.build_actionable_plan("SYN", CURR)
    assert p.pattern is not None and p.pattern["direction"] == "compra"
    assert p.invalidation is not None
    assert p.invalidation["price"] == p.pattern["p3"]["price"]  # nível real, do ponto 3
    txt = p.invalidation["meaning"].lower()
    assert "morre se perder" in txt and "ponto 3" in txt


@pytest.mark.unit
def test_compra_stop_e_estrutura_mais_folga_de_atr(longf):
    """Stop = invalidação − 0,5·ATR14, abaixo dela. A folga é DECLARADA e vem da
    série; nada de "2% abaixo" chutado."""
    p = ps.build_actionable_plan("SYN", CURR)
    df = ps._prep("SYN", CURR)
    atr = ps._atr(df)
    assert atr is not None and atr > 0
    inval = p.invalidation["price"]
    assert p.stop["price"] == round(inval - ps._STOP_ATR_SLACK * atr, 2)
    assert p.stop["price"] < inval          # numa compra o stop fica ABAIXO
    assert p.stop["anchor"] == inval
    assert p.stop["atr"] == atr
    assert "ATR" in p.stop["basis"]


@pytest.mark.unit
def test_compra_alvo_e_topo_real_acima_da_entrada(longf):
    """O alvo é um topo anterior REAL — e medido a partir da ENTRADA, não do preço:
    num 1-2-3 ainda não acionado o topo mais próximo do preço é o próprio gatilho
    (a máxima do ponto 2), e ter o gatilho como alvo não é trade nenhum."""
    p = ps.build_actionable_plan("SYN", CURR)
    assert p.pattern["state"] != "acionado"           # entrada = gatilho
    highs = set(round(float(v), 2) for v in longf["High"])
    assert p.target is not None
    assert p.target["price"] in highs                  # topo real da série
    assert p.target["price"] > p.pattern["trigger"]    # à frente da ENTRADA
    assert p.target["price"] != p.pattern["trigger"]


@pytest.mark.unit
def test_compra_risco_retorno_bate_com_os_niveis(longf):
    """R:R = retorno/risco calculado sobre entrada, stop e alvo já publicados —
    conferível na tela, sem número solto."""
    p = ps.build_actionable_plan("SYN", CURR)
    rr = p.risk_reward
    assert rr is not None and rr["rr"] is not None
    assert rr["entry"] == p.pattern["trigger"]
    assert "gatilho" in rr["entry_basis"]
    assert rr["risk"] == round(rr["entry"] - p.stop["price"], 2)
    assert rr["reward"] == round(p.target["price"] - rr["entry"], 2)
    assert rr["rr"] == round(rr["reward"] / rr["risk"], 2)


@pytest.mark.unit
def test_realizacao_que_e_o_gatilho_nao_vira_alvo(longf):
    """A região de realização que coincide com o gatilho do 1-2-3 é carimbada como
    tal — a tela não desenha o mesmo nível duas vezes nem o chama de alvo."""
    p = ps.build_actionable_plan("SYN", CURR)
    assert p.realize_zone is not None
    assert p.realize_zone["price"] == p.pattern["trigger"]
    assert p.realize_zone["role"] == "gatilho"
    assert p.target["same_as_realize"] is False


# ------------------------------------------------------------------- venda ----
@pytest.mark.unit
def test_venda_nao_herda_esqueleto_de_long(shortf):
    """Espelho exato: a venda invalida para CIMA (retomada do ponto 3), o stop fica
    ACIMA e o alvo é um FUNDO anterior abaixo — nunca o topo overhead do long."""
    p = ps.build_actionable_plan("SYN", CURR)
    assert p.pattern is not None and p.pattern["direction"] == "venda"
    assert p.invalidation["price"] == p.pattern["p3"]["price"]
    assert "voltar acima" in p.invalidation["meaning"]
    assert p.stop["price"] > p.invalidation["price"]        # stop ACIMA
    lows = set(round(float(v), 2) for v in shortf["Low"])
    assert p.target is not None and p.target["price"] in lows
    assert p.target["price"] < p.price                       # alvo ABAIXO
    assert p.target["label"].startswith("fundo anterior")
    # o topo overhead continua existindo, mas como RESISTÊNCIA — não como alvo
    assert p.realize_zone is not None
    assert p.realize_zone["role"] == "resistencia"
    assert p.realize_zone["price"] > p.price


@pytest.mark.unit
def test_venda_risco_retorno_usa_preco_quando_acionado(shortf):
    """Padrão já acionado: o gatilho ficou para trás, então a entrada de referência
    é o preço atual — e isso fica escrito, não subentendido."""
    p = ps.build_actionable_plan("SYN", CURR)
    assert p.pattern["state"] == "acionado"
    rr = p.risk_reward
    assert rr["entry"] == p.price
    assert "preço atual" in rr["entry_basis"]
    assert rr["risk"] == round(p.stop["price"] - rr["entry"], 2)
    assert rr["reward"] == round(rr["entry"] - p.target["price"], 2)
    assert rr["rr"] == round(rr["reward"] / rr["risk"], 2)


# ------------------------------------------------------------- sem base -------
@pytest.mark.unit
def test_sem_padrao_nao_inventa_nivel(monkeypatch):
    """Série sem 1-2-3 nenhum: os quatro campos ficam None. Sem padrão não há o que
    invalidar — e um stop "genérico" seria número inventado."""
    df = _mk([100 + i * 0.5 for i in range(120)])  # rampa limpa, sem reversão
    monkeypatch.setattr(ps, "load_ohlcv", lambda s, d: df.copy())
    p = ps.build_actionable_plan("SYN", CURR)
    assert p.pattern is None
    assert p.invalidation is None and p.stop is None
    assert p.target is None and p.risk_reward is None


@pytest.mark.unit
def test_alvo_em_ar_de_novo_extremo_e_sem_nivel(monkeypatch):
    """Preço em ar de fundo novo (venda sem fundo anterior abaixo): alvo None — a
    tela mostra "sem nível definido", não um alvo plausível.

    E o R:R **não some calado**. DENTE (task 20260830-006): aqui ele devolvia
    ``None``, a linha de risco/retorno desaparecia do card e o frame ficava
    indistinguível de um que nem padrão tem. Era o defeito dos prints do Samyr — o
    R:R só aparecia no diário. Agora vem ``rr=None`` COM o motivo escrito, e o
    risco, que existe, continua medido.
    """
    import tests.test_price_structure as tps
    df = tps._top_frame()
    monkeypatch.setattr(ps, "load_ohlcv", lambda s, d: df.copy())
    p = ps.build_actionable_plan("SYN", "2025-12-31")
    assert p.pattern is not None and p.pattern["direction"] == "venda"
    assert p.invalidation is not None and p.stop is not None   # estrutura existe
    assert p.target is None                                     # alvo não
    rr = p.risk_reward
    assert rr is not None and rr["rr"] is None, rr
    assert "sem alvo estrutural" in rr["note"], rr
    assert "fundo anterior abaixo" in rr["note"], ("a venda procura fundo, não topo", rr)
    assert rr["risk"] is not None and rr["reward"] is None, (
        "o risco existe (há stop) e o retorno não — cada perna diz a verdade", rr)


@pytest.mark.unit
def test_stop_sem_atr_e_a_invalidacao_exata(monkeypatch, longf):
    """Sem base de ATR o stop NÃO ganha folga inventada: ele é a própria
    invalidação, e o motivo fica declarado."""
    monkeypatch.setattr(ps, "_atr", lambda df, period=ps._ATR_PERIOD: None)
    p = ps.build_actionable_plan("SYN", CURR)
    assert p.stop["price"] == p.invalidation["price"]
    assert p.stop["atr"] is None
    assert "sem base de ATR" in p.stop["basis"]


# ----------------------------------------------------------------- relatório --
@pytest.mark.unit
def test_secao_do_relatorio_publica_os_tres_niveis(longf):
    section = ps.build_price_structure_section("SYN", CURR)
    assert "Níveis operáveis do padrão" in section
    for label in ("**Invalidação**", "**Stop (SL)**", "**Alvo (TP)**", "**Risco/retorno**"):
        assert label in section


@pytest.mark.unit
def test_secao_sem_alvo_diz_sem_nivel_definido(monkeypatch):
    import tests.test_price_structure as tps
    df = tps._top_frame()
    monkeypatch.setattr(ps, "load_ohlcv", lambda s, d: df.copy())
    section = ps.build_price_structure_section("SYN", "2025-12-31")
    assert "**Alvo (TP)**: sem nível definido" in section
    assert "**Invalidação**: " in section  # a invalidação existe e sai com número


@pytest.mark.unit
def test_json_serializavel(longf):
    import json
    d = ps.build_actionable_plan_dict("SYN", CURR)
    json.dumps(d)
    for key in ("invalidation", "stop", "target", "risk_reward"):
        assert key in d


# --------------------------------------------- método Erick + grounding do chat --
_PLAN_FOR_SURFACES = {
    "setup_state": "ativo",
    "price": 140.0,
    "realize_zone": {"label": "topo anterior 2026-05-13", "low": 108.0,
                     "high": 112.0, "price": 110.0, "role": "alvo",
                     "role_label": "realização (alvo)"},
    "pattern": {"p1": {"date": "2026-01-05", "price": 118.0},
                "p2": {"date": "2026-02-10", "price": 152.0},
                "p3": {"date": "2026-03-08", "price": 131.0},
                "trigger": 152.0, "state": "formando", "direction": "compra"},
    "invalidation": {"label": "perda do ponto 3 (2026-03-08)", "price": 131.0,
                     "meaning": "o setup morre se perder 131.00 — abaixo do ponto 3 ..."},
    "stop": {"label": "stop (SL)", "price": 128.5, "anchor": 131.0, "atr": 5.0,
             "basis": "invalidação + folga de 0.5·ATR14"},
    "target": {"label": "topo anterior 2025-11-20", "price": 176.0, "low": 173.5,
               "high": 178.5, "band_basis": "±0.5·ATR14", "same_as_realize": False},
    "risk_reward": {"entry": 152.0, "entry_basis": "gatilho — rompimento da máxima do ponto 2",
                    "risk": 23.5, "reward": 24.0, "rr": 1.02, "note": None},
}


@pytest.mark.unit
def test_metodo_erick_publica_stop_alvo_e_rr():
    """O método é "perda de estrutura", não stop percentual — a leitura do Erick tem
    que carregar a invalidação, o stop com a folga declarada e o R:R."""
    from tradingagents.agents.utils import erick_method as em
    line = em._levels_line(_PLAN_FOR_SURFACES)
    assert line is not None
    assert "invalida em 131.00" in line
    assert "stop 128.50" in line and "ATR14" in line
    assert "alvo 176.00" in line
    assert "R:R 1.02:1" in line


@pytest.mark.unit
def test_metodo_erick_sem_padrao_nao_emite_linha():
    from tradingagents.agents.utils import erick_method as em
    assert em._levels_line({"setup_state": "ativo"}) is None
    assert em._levels_line(None) is None


@pytest.mark.unit
def test_chat_recebe_os_niveis_como_fato_ancorado():
    """O grounding do chat enumera invalidação/stop/alvo/R:R — sem isso o modelo
    preenche a lacuna quando perguntam "e o stop?"."""
    from tradingagents.webui.ask import price_facts
    facts = "\n".join(price_facts(_PLAN_FOR_SURFACES, {}))
    # _num formata em pt-BR (vírgula decimal), como o resto do grounding
    assert "Invalidação do padrão: 131,00" in facts
    assert "Stop (SL): 128,50" in facts
    assert "Alvo (TP) do padrão: 176,00" in facts
    assert "Risco/retorno: 1,02:1" in facts


@pytest.mark.unit
def test_chat_declara_ausencia_em_vez_de_inventar():
    from tradingagents.webui.ask import price_facts
    plan = {**_PLAN_FOR_SURFACES, "target": None, "risk_reward": None}
    facts = "\n".join(price_facts(plan, {}))
    assert "Alvo (TP) do padrão: sem nível definido." in facts
    assert "Risco/retorno: sem base" in facts

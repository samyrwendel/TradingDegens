"""SINAIS DE ENTRADA: confluência, conflito e a janela derivada (task 20260831-017).

Três coisas que a tabela ativo×frame não conseguia dizer, e um dataset REAL para
provar cada uma. O fixture ``tests/data/scan_real_20260831.json`` é um recorte do
último conhecido de produção de 31/08 — não um sintético conveniente: os casos
que o Samyr apontou no print dele estão ali com os números que a fonte devolveu.

* **CRWD** — 4h e 1h de compra concordando: confluência, uma oportunidade só.
* **AAPL** — venda no 1d contra compra no 4h e 1h: CONFLITO declarado, sem níveis.
* **MSFT** — três frames de compra concordando, e nenhuma janela: o R:R no gatilho
  é 0,06, ou seja o setup não paga em preço de entrada nenhum.
* **MRVL** — os três frames do 1-2-3 invalidados: nenhuma oportunidade daquele
  método. É o "sem sinal nenhum", e ele tem de sair da lista sem virar linha vazia.
* **GOOGL** — o caso que corrige a intuição: o 1d de COMPRA está INVALIDADO, então
  não vota. Sobra venda no 4h e no 1h — confluência, não conflito. Um padrão morto
  contando como voto criaria conflito fantasma justo onde o sinal está limpo.

A janela tem dente próprio: ela é verificada RECALCULANDO o R:R no limite, não
comparando com uma constante decorada. Se a fórmula sair do lugar, o R:R no limite
deixa de ser exatamente o mínimo e o teste cai.
"""

import json
from pathlib import Path

import pytest

from tradingagents.webui import sinais

pytestmark = pytest.mark.unit

_REAL = json.loads((Path(__file__).parent / "data" / "scan_real_20260831.json")
                   .read_text(encoding="utf-8"))


def _ops(scan=None, **kw):
    return sinais.oportunidades(scan if scan is not None else _REAL, **kw)


def _uma(ops, ticker, metodo):
    return next((o for o in ops if o["ticker"] == ticker and o["metodo"] == metodo), None)


def _rr(entrada, sl, tp):
    return abs(tp - entrada) / abs(entrada - sl)


# ------------------------------------------------------------ a janela, pura ----
@pytest.mark.parametrize("direcao,trigger,sl,tp", [
    ("compra", 100.0, 90.0, 130.0),      # R:R no gatilho = 3,0
    ("venda", 100.0, 110.0, 70.0),       # R:R no gatilho = 3,0
])
def test_no_limite_o_rr_e_exatamente_o_minimo(direcao, trigger, sl, tp):
    """O dente da fórmula: no limite, R:R == m. Recalculado, não decorado."""
    for m in (0.5, 1.0, 2.0):
        limite = sinais.limite_da_janela(sl, tp, m)
        assert _rr(limite, sl, tp) == pytest.approx(m, rel=1e-9)


def test_a_janela_da_compra_sobe_e_a_da_venda_desce():
    """Mesma fórmula, lados opostos — entrar mais caro piora a compra, mais barato
    piora a venda. Trocar os lados inverteria o conselho na metade dos cards."""
    lc = sinais.limite_da_janela(90.0, 130.0, 1.0)
    lv = sinais.limite_da_janela(110.0, 70.0, 1.0)
    assert lc > 100.0 and lv < 100.0


@pytest.mark.parametrize("preco,esperado", [
    (99.0, "nao_abriu"), (100.0, "aberta"), (105.0, "aberta"),
    (110.0, "aberta"), (110.1, "fechada"), (125.0, "fechada"),
])
def test_estado_da_janela_na_compra(preco, esperado):
    j = sinais.janela_de_entrada(100.0, 90.0, 130.0, preco, "compra", 1.0)
    assert j["existe"] and j["de"] == 100.0
    assert j["ate"] == pytest.approx(110.0)
    assert j["estado"] == esperado


@pytest.mark.parametrize("preco,esperado", [
    (101.0, "nao_abriu"), (100.0, "aberta"), (95.0, "aberta"),
    (90.0, "aberta"), (89.9, "fechada"), (75.0, "fechada"),
])
def test_estado_da_janela_na_venda(preco, esperado):
    j = sinais.janela_de_entrada(100.0, 110.0, 70.0, preco, "venda", 1.0)
    assert j["existe"] and j["ate"] == 100.0
    assert j["de"] == pytest.approx(90.0)
    assert j["estado"] == esperado


def test_sem_janela_quando_nem_o_gatilho_paga():
    """R:R 0,25 no gatilho: não há preço de entrada que chegue a 1:1 — e o card
    tem de dizer o motivo, não sumir com a linha."""
    j = sinais.janela_de_entrada(100.0, 90.0, 102.5, 100.0, "compra", 1.0)
    assert j["existe"] is False
    assert j["rr_gatilho"] == pytest.approx(0.25)
    assert "0.25" in j["motivo"] and "1:1" in j["motivo"]


def test_sem_niveis_devolve_None_e_nao_zero():
    """"Não sei calcular" e "não há janela" são coisas diferentes."""
    assert sinais.janela_de_entrada(100.0, 90.0, None, 100.0, "compra") is None
    assert sinais.janela_de_entrada(None, 90.0, 110.0, 100.0, "compra") is None


def test_risco_zero_nao_vira_divisao_por_zero():
    assert sinais.rr_no_gatilho(100.0, 100.0, 120.0) is None
    assert sinais.janela_de_entrada(100.0, 100.0, 120.0, 100.0, "compra") is None


# --------------------------------------------- confluência, conflito, mortos ----
def test_CRWD_dois_frames_de_compra_viram_UMA_oportunidade():
    o = _uma(_ops(), "CRWD", "storm")
    assert o["direcao"] == "compra"
    assert o["frames"] == ["4h", "1h"] and o["confluencia"] == 2
    # e o 1-2-3 do mesmo ativo continua sendo OUTRO card (DA-077)
    outro = _uma(_ops(), "CRWD", "123")
    assert outro is not None and outro["metodo_rotulo"] == "Setup123"


def test_MSFT_tres_frames_concordam_e_ainda_assim_nao_ha_janela():
    """Confluência não é permissão: 3 frames de compra com R:R 0,06 no gatilho."""
    o = _uma(_ops(), "MSFT", "123")
    assert o["confluencia"] == 3 and o["frames"] == ["1d", "4h", "1h"]
    assert o["janela"]["existe"] is False
    assert o["rr_gatilho"] < 0.1


def test_AAPL_frames_opostos_viram_CONFLITO_sem_niveis():
    """DENTE: somar os lados daria "compra 2×1" e um card operável — inventando
    convicção onde o método está dividido."""
    o = _uma(_ops(), "AAPL", "123")
    assert o["estado"] == "conflito" and o["direcao"] is None
    lados = {x["direcao"]: x["frames"] for x in o["lados"]}
    assert lados["compra"] == ["4h", "1h"] and lados["venda"] == ["1d"]
    for campo in ("gatilho", "sl", "tp", "janela"):
        assert campo not in o, f"conflito publicou {campo} — não há um lado a operar"


def test_GOOGL_o_1d_invalidado_NAO_vota_e_nao_cria_conflito_fantasma():
    """O caso que corrige a intuição.

    No print, GOOGL parece conflito: 1d de compra contra 4h e 1h de venda. Mas o
    1d está INVALIDADO — a premissa da compra rompeu porque o preço caiu, que é
    CONFIRMAÇÃO da venda, não contradição. Ele vira dissidente declarado no card.
    """
    o = _uma(_ops(), "GOOGL", "123")
    assert o["estado"] != "conflito"
    assert o["direcao"] == "venda" and o["frames"] == ["4h", "1h"]
    assert {(d["frame"], d["direcao"], d["estado"]) for d in o["dissidentes"]} == \
        {("1d", "compra", "invalidou")}


def test_MRVL_com_os_tres_frames_invalidados_nao_gera_oportunidade():
    """Sem leitura viva não há oportunidade — e nem linha vazia inventada."""
    assert _uma(_ops(), "MRVL", "123") is None


def test_INTC_e_a_unica_entrada_aberta_do_dataset():
    entradas = [o for o in _ops() if o["estado"] == "entrada"]
    assert [(o["ticker"], o["metodo"]) for o in entradas] == [("INTC", "storm")]
    o = entradas[0]
    assert o["direcao"] == "venda" and o["confluencia"] == 2
    j = o["janela"]
    assert j["estado"] == "aberta"
    assert j["de"] < o["preco"] < j["ate"] or j["de"] <= o["preco"] <= j["ate"]
    # a janela é MAIS ESTREITA que a tolerância de "está no gatilho" (0,5%)
    assert j["largura_pct"] < 0.005


# ------------------------------------------------------------------ o veto ----
def _um_frame(estado="em_gatilho", direcao="compra", **storm):
    st = {"estado": estado, "direction": direcao, "trigger": 100.0, "sl": 90.0,
          "tp": 130.0, "dist_pct": 0.0, "opera": True}
    st.update(storm)
    return {"frame": "1d", "estado": "sem_setup", "price": 100.0, "storm": st}


def _scan(frames):
    return {"ativos": [{"ticker": "X", "frames": frames}]}


def test_storm_vetado_pelo_Eden_nao_vira_oportunidade():
    """DA-079: gatilho que a regra proíbe operar não é trade."""
    ops = _ops(_scan([_um_frame(estado="vetado", opera=False)]))
    assert ops == []


def test_zona_neutra_opera_mas_avisa():
    ops = _ops(_scan([_um_frame(estado="zona_neutra", zona_neutra=True)]))
    assert len(ops) == 1
    assert ops[0]["estado"] == "entrada"
    assert "zona neutra" in ops[0]["aviso"]


# ------------------------------------------------- o outro método e a ordem ----
def test_cada_card_diz_o_que_o_OUTRO_metodo_le_no_mesmo_ativo():
    """Sem colapsar (DA-077): é menção, não fusão."""
    o = _uma(_ops(), "GOOGL", "123")
    assert o["outro_metodo"]["metodo"] == "storm"
    assert o["outro_metodo"]["direcao"] == "venda"


def test_ordem_poe_o_acionavel_primeiro_e_o_conflito_por_ultimo():
    estados = [o["estado"] for o in _ops()]
    assert estados[0] == "entrada"
    assert estados[-1] == "conflito"
    assert estados == sorted(estados, key=lambda e: sinais._ORDEM_ESTADO[e])


def test_ter_janela_pesa_mais_que_confluencia():
    """3 frames que não pagam não podem aparecer acima de 2 que pagam."""
    a_caminho = [o for o in _ops() if o["estado"] == "a_caminho"]
    com = [i for i, o in enumerate(a_caminho) if (o["janela"] or {}).get("existe")]
    sem = [i for i, o in enumerate(a_caminho) if not (o["janela"] or {}).get("existe")]
    assert com and sem and max(com) < min(sem)


def test_a_chave_do_sinal_carrega_o_GATILHO():
    """Sem o gatilho na chave, um padrão que morreu e outro que nasceu no mesmo
    ativo e direção seriam "o mesmo sinal", e o novo nunca se anunciaria."""
    o = _uma(_ops(), "CRWD", "storm")
    assert o["chave"].startswith("CRWD|storm|compra|")
    assert str(round(o["gatilho"], 2)).split(".")[0] in o["chave"]


def test_o_minimo_de_RR_e_parametro_e_muda_o_resultado():
    """A escolha de 1:1 é declarada, não estrutural — baixá-la abre mais janelas."""
    com1 = sum(1 for o in _ops() if (o.get("janela") or {}).get("existe"))
    com05 = sum(1 for o in _ops(rr_min=0.5) if (o.get("janela") or {}).get("existe"))
    assert com05 > com1

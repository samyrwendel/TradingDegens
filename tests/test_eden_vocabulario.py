"""UM VOCABULÁRIO DO ÉDEN — um lugar decide como o filtro se escreve (task 036).

*"nos cards de texto onde usamos Éden, identifica Éden de Alta e de Baixa na menção."*

O dado sempre existiu (``direcao``, ``alinhado``, ``armadilha``, ``zona_neutra``) e
nunca chegava ao texto: o card mostrava "MME 8 × MME 80" com dois números e não dizia
de que Éden se tratava. É o mesmo padrão da DA-095 — o cálculo certo, o nome ausente.

E o rótulo é **Alta/Baixa**, não compra/venda: o Éden é filtro de REGIME (as duas
médias alinhadas mais a posição do preço), não sinal de entrada. A equivalência com a
doutrina do Stormer ("Éden de compra") viaja no campo ``doutrina``, pro ``title``.

O que se trava aqui: **um vocabulário só**. Cada estado tem UM nome, ele viaja pronto
no payload, e nenhuma superfície escreve o seu.
"""

import pandas as pd
import pytest

from tradingagents.dataflows import price_structure as ps

pytestmark = pytest.mark.unit

_ESTADOS = ("alta", "baixa", "armadilha", "neutra", "desalinhado", "indisponivel")


def _serie(precos, n=120):
    """Série com candles suficientes pra MME 80 significar alguma coisa."""
    datas = pd.date_range("2026-01-01", periods=n, freq="D")
    df = pd.DataFrame({"Date": datas, "Open": precos, "High": [p * 1.005 for p in precos],
                       "Low": [p * 0.995 for p in precos], "Close": precos})
    for j in (ps._STORM_EMA_RAPIDA, ps._STORM_EMA_LENTA):
        df[f"EMA{j}"] = df["Close"].ewm(span=j, adjust=False).mean()
    return df


def test_cada_estado_tem_UM_nome_e_ele_e_unico():
    """Dois estados com o mesmo nome é um estado só na tela — e a ARMADILHA some
    dentro de "zona neutra" justamente no caso mais caro."""
    assert set(ps._EDEN_ROTULO) == set(_ESTADOS), ps._EDEN_ROTULO
    longos = [r for r, _ in ps._EDEN_ROTULO.values()]
    assert len(set(longos)) == len(longos), ("nome repetido entre estados", longos)


def test_o_rotulo_de_direcao_e_ALTA_e_BAIXA_nao_compra_e_venda():
    """Palavra do Samyr, e ela é mais correta: o Éden é filtro de REGIME, não sinal de
    entrada. A doutrina do Stormer fica na equivalência, não no rótulo."""
    assert ps._EDEN_ROTULO["alta"][0] == "Éden de Alta"
    assert ps._EDEN_ROTULO["baixa"][0] == "Éden de Baixa"
    assert ps._eden_nomes("alta")["doutrina"] == "Éden de compra"
    assert ps._eden_nomes("baixa")["doutrina"] == "Éden de venda"
    # e os estados sem direção NÃO ganham equivalência inventada
    for e in ("armadilha", "neutra", "desalinhado", "indisponivel"):
        assert "doutrina" not in ps._eden_nomes(e), e


@pytest.mark.parametrize("estado", _ESTADOS)
def test_todo_estado_viaja_com_nome_pronto(estado):
    """A tela LÊ o rótulo; se ele não viaja, ela volta a escrever o seu."""
    n = ps._eden_nomes(estado)
    assert n["estado"] == estado and n["rotulo"] and n["rotulo_curto"], n


def test_alta_e_baixa_saem_nomeados_da_serie_real():
    """Da série ao rótulo, sem escala intermediária."""
    sobe = _serie([100 + i * 0.8 for i in range(120)])
    e = ps._eden(sobe)
    assert e["direcao"] == "compra" and e["estado"] == "alta", e
    assert e["rotulo"] == "Éden de Alta", e

    desce = _serie([200 - i * 0.8 for i in range(120)])
    e = ps._eden(desce)
    assert e["direcao"] == "venda" and e["estado"] == "baixa", e
    assert e["rotulo"] == "Éden de Baixa", e


def test_os_estados_SEM_direcao_nao_viram_um_generico_so():
    """"desalinhado", "ARMADILHA" e "ZONA NEUTRA" são coisas diferentes: a armadilha é
    repique dentro de tendência contrária e é o caso mais caro da lista."""
    nomes = {e: ps._EDEN_ROTULO[e][0] for e in ("armadilha", "neutra", "desalinhado")}
    assert len(set(nomes.values())) == 3, nomes
    assert "ARMADILHA" in nomes["armadilha"], nomes
    assert "ZONA NEUTRA" in nomes["neutra"], nomes


def test_a_prosa_do_veto_usa_o_MESMO_nome():
    """DENTE: o veto dizia "sem Éden alinhado" enquanto o motivo da mesma leitura dizia
    "ZONA NEUTRA" — dois nomes pro mesmo estado, na mesma tela."""
    pat = ps.StormPattern(
        p1={"date": "2026-08-24", "price": 100.0, "high": 100.0, "low": 98.0},
        p2={"date": "2026-08-25", "price": 96.0, "high": 99.0, "low": 96.0},
        p3={"date": "2026-08-26", "price": 99.0, "high": 99.0, "low": 97.0},
        direction="compra", amplitude=4.0)
    eden = {"alinhado": True, "direcao": "venda", "motivo": "…",
            **ps._eden_nomes("baixa")}
    q = ps._storm_qualidade(pat, eden, 95.0)
    assert q["opera"] is False, q
    assert "Éden de Baixa" in q["veto"], q["veto"]
    assert "Éden de Baixa" in q["motivo"], q["motivo"]
    # e nada de "de venda" solto: o rótulo de tela é Alta/Baixa
    assert "Éden de venda" not in q["veto"], q["veto"]


def test_eden_montado_a_mao_nao_produz_prosa_quebrada():
    """Plano em cache antigo (sem os campos de nome) ainda tem ``direcao`` — e o nome
    sai do MESMO vocabulário, nunca de um "sem Éden" genérico que mente."""
    assert ps._eden_nome_curto({"direcao": "compra"}) == "Éden de Alta"
    assert ps._eden_nome_curto({"direcao": "venda"}) == "Éden de Baixa"
    assert ps._eden_nome_curto({"zona_neutra": True, "armadilha": True}) == "armadilha"
    assert ps._eden_nome_curto({"zona_neutra": True, "armadilha": False}) == "zona neutra"
    assert ps._eden_nome_curto({"disponivel": False}) == "Éden indisponível"
    assert ps._eden_nome_curto({}) == "sem Éden"

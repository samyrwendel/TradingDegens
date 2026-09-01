"""A ORDEM dos eventos decide o resultado — e a tela passa a contá-la (task 20260831-024).

*"pq invalidado se ele atingiu o alvo?"* — o Samyr, no LINK-USD 1h.

**Cronometrado na série, ele tem razão** (análise de 30/08, frame 1h):

===============  ==========================================================
30/08 09:00      nasce o ponto 3 (11,34) — o padrão passa a existir
30/08 13:00      gatilho 11,52 TOCADO — a entrada aconteceu
30/08 15:00      alvo 11,63 TOCADO — duas barras depois da entrada
30/08 23:00      stop 11,27 tocado e o fechamento cai além do ponto 3: INVALIDA
===============  ==========================================================

O alvo foi alcançado com o padrão VIVO; a morte veio oito horas depois. Um rótulo
"invalidado" sozinho escondia que o setup **pagou**.

O caminho inverso também existe e engana ao contrário: preço tocando o nível do
alvo **depois** da morte, com quem entrou já stopado. Sem timestamps os dois são
indistinguíveis — e é por isso que a leitura natural erra num dos dois sentidos.

Mesma disciplina da task 008, que tirou o veredito de fechamento do track record
da inspeção de nível e o pôs na SÉRIE, com direção e ordem: **"o preço passou pelo
nível" não é "o trade ganhou"**.

Os dentes:

* o caso REAL do LINK-USD (alvo ANTES) é classificado como alvo alcançado;
* o caso INVERSO (alvo DEPOIS) é classificado como "não é alvo alcançado";
* alvo tocado sem o GATILHO ter sido rompido antes não vira vitória — não houve
  entrada a realizar, e é o gatilho na lista que impede essa conclusão;
* padrão VIVO não recebe ordem nenhuma: não há morte a que se referir;
* o toque é por AMPLITUDE da barra (o critério do ``_primeiro_toque`` do ledger).
"""

import pandas as pd
import pytest

from tradingagents.dataflows.price_structure import (
    Pattern123,
    _cronologia_do_padrao,
    _primeiro_toque_na_serie,
)

pytestmark = pytest.mark.unit


def _serie(barras):
    """``barras`` = [(data, low, high)] — o resto do OHLC não entra no toque."""
    return pd.DataFrame({
        "Date": pd.to_datetime([b[0] for b in barras]),
        "Low": [b[1] for b in barras],
        "High": [b[2] for b in barras],
        "Close": [(b[1] + b[2]) / 2 for b in barras],
    })


_FMT = "%Y-%m-%d %H:%M"


def _pat(p3_data, trigger, invalidado_em=None, direction="compra"):
    return Pattern123(
        p1={"date": "2026-08-30 05:00", "price": 11.0},
        p2={"date": "2026-08-30 07:00", "price": 11.5},
        p3={"date": p3_data, "price": 11.34},
        trigger=trigger, state="acionado", direction=direction,
        invalidado=invalidado_em is not None, invalidado_em=invalidado_em,
    )


# A série REAL do LINK-USD 1h em 30/08, nos pontos que importam.
_LINK = _serie([
    ("2026-08-30 09:00", 11.30, 11.40),   # nasce o ponto 3
    ("2026-08-30 13:00", 11.45, 11.55),   # gatilho 11,52
    ("2026-08-30 15:00", 11.55, 11.65),   # alvo 11,63
    ("2026-08-30 19:00", 11.40, 11.60),
    ("2026-08-30 23:00", 11.20, 11.45),   # stop 11,27 e a invalidação
])


def _crono(df, pat, alvo, stop):
    return _cronologia_do_padrao(df, pat, {"price": alvo}, {"price": stop}, _FMT)


# ------------------------------------------------------------- o toque, puro ----
def test_o_toque_e_por_AMPLITUDE_da_barra_nao_por_fechamento():
    """Um alvo não precisa de FECHAMENTO além dele para ser tocado — é o mesmo
    critério do ``_primeiro_toque`` do ledger (task 008)."""
    assert _primeiro_toque_na_serie(_LINK, 11.63, _FMT) == "2026-08-30 15:00"
    # o fechamento daquela barra é 11,60 — abaixo do alvo, e ainda assim tocou
    assert float(_LINK["Close"].iloc[2]) < 11.63


def test_nivel_nunca_tocado_devolve_None():
    assert _primeiro_toque_na_serie(_LINK, 99.0, _FMT) is None


# ------------------------------------------------------- o caso REAL do Samyr ----
def test_LINK_USD_o_alvo_veio_ANTES_da_invalidacao_e_o_setup_PAGOU():
    """DENTE: antes, a tela dizia só "invalidado" — escondendo que o alvo foi
    alcançado com o padrão vivo, oito horas antes da morte."""
    c = _crono(_LINK, _pat("2026-08-30 09:00", 11.52, "2026-08-30 23:00"), 11.63, 11.27)
    assert c["desde"] == "2026-08-30 09:00"
    assert c["invalidado_em"] == "2026-08-30 23:00"
    por_nome = {e["nome"]: e for e in c["eventos"]}
    assert por_nome["gatilho"]["quando"] == "2026-08-30 13:00"
    assert por_nome["gatilho"]["ordem"] == "antes"
    assert por_nome["alvo (TP)"]["quando"] == "2026-08-30 15:00"
    assert por_nome["alvo (TP)"]["ordem"] == "antes", "o alvo foi tocado com o padrão VIVO"
    assert por_nome["stop (SL)"]["ordem"] == "junto"
    # e a lista sai em ordem CRONOLÓGICA — é o que a tela desenha
    assert [e["quando"] for e in c["eventos"]] == sorted(e["quando"] for e in c["eventos"])


# ------------------------------------------------------------- o caso INVERSO ----
def test_o_caso_INVERSO_alvo_tocado_DEPOIS_da_invalidacao():
    """O engano ao contrário: o preço passa pelo nível do alvo com o trade já
    encerrado. Sem timestamp, indistinguível do caso acima."""
    serie = _serie([
        ("2026-08-29 03:00", 11.30, 11.40),   # ponto 3
        ("2026-08-29 07:00", 11.45, 11.55),   # gatilho
        ("2026-08-29 11:00", 11.20, 11.35),   # stop e invalidação
        ("2026-08-30 15:00", 11.55, 11.65),   # o alvo, DEPOIS da morte
    ])
    c = _crono(serie, _pat("2026-08-29 03:00", 11.52, "2026-08-29 11:00"), 11.63, 11.27)
    por_nome = {e["nome"]: e for e in c["eventos"]}
    assert por_nome["alvo (TP)"]["ordem"] == "depois"
    assert por_nome["alvo (TP)"]["quando"] == "2026-08-30 15:00"
    assert por_nome["gatilho"]["ordem"] == "antes"


def test_os_dois_casos_sao_DISTINGUIVEIS_pelo_mesmo_campo():
    """A distinção não depende de o leitor comparar datas de cabeça."""
    antes = _crono(_LINK, _pat("2026-08-30 09:00", 11.52, "2026-08-30 23:00"), 11.63, 11.27)
    serie2 = _serie([
        ("2026-08-29 03:00", 11.30, 11.40), ("2026-08-29 07:00", 11.45, 11.55),
        ("2026-08-29 11:00", 11.20, 11.35), ("2026-08-30 15:00", 11.55, 11.65),
    ])
    depois = _crono(serie2, _pat("2026-08-29 03:00", 11.52, "2026-08-29 11:00"), 11.63, 11.27)
    alvo = lambda c: next(e["ordem"] for e in c["eventos"] if e["nome"].startswith("alvo"))  # noqa: E731
    assert alvo(antes) == "antes" and alvo(depois) == "depois"


# ----------------------------------------------- o gatilho é o que impede a mentira ----
def test_alvo_tocado_SEM_o_gatilho_ter_rompido_nao_e_vitoria():
    """"Tocou o alvo" só vira "ganhou" se houve ENTRADA — e é o gatilho que diz.

    Aqui o preço bateu no nível do alvo sem nunca ter passado pelo gatilho: não há
    trade nenhum a realizar. Sem o gatilho na lista, a leitura seria "alcançou".
    """
    serie = _serie([
        ("2026-08-30 09:00", 11.30, 11.40),
        ("2026-08-30 15:00", 11.60, 11.65),   # tocou o alvo sem passar pelo gatilho
        ("2026-08-30 23:00", 11.20, 11.45),
    ])
    c = _crono(serie, _pat("2026-08-30 09:00", 99.0, "2026-08-30 23:00"), 11.63, 11.27)
    nomes = [e["nome"] for e in c["eventos"]]
    assert "gatilho" not in nomes, "o gatilho nunca foi tocado — não pode virar evento"
    assert any(e["nome"].startswith("alvo") and e["ordem"] == "antes" for e in c["eventos"])


# ------------------------------------------------------------ padrão VIVO -------
def test_padrao_VIVO_nao_recebe_ordem_nenhuma():
    """Sem morte não há a que se referir — e inventar "antes" seria afirmar uma
    invalidação que não existe."""
    c = _crono(_LINK, _pat("2026-08-30 09:00", 11.52, None), 11.63, 11.27)
    assert c["invalidado_em"] is None
    assert all(e["ordem"] is None for e in c["eventos"]), c


def test_sem_padrao_nao_ha_cronologia():
    assert _cronologia_do_padrao(_LINK, None, {"price": 1}, {"price": 2}, _FMT) is None


def test_serie_que_nao_alcanca_o_ponto_3_nao_inventa_linha_do_tempo():
    vazia = _serie([("2026-08-20 09:00", 11.0, 11.1)])
    assert _cronologia_do_padrao(vazia, _pat("2026-08-30 09:00", 11.52), {"price": 11.63},
                                 {"price": 11.27}, _FMT) is None


def test_nivel_ausente_nao_vira_evento():
    """Alvo recusado (DA-123) não pode virar um evento sem preço."""
    c = _cronologia_do_padrao(_LINK, _pat("2026-08-30 09:00", 11.52, "2026-08-30 23:00"),
                              None, {"price": 11.27}, _FMT)
    assert [e["nome"] for e in c["eventos"]] == ["gatilho", "stop (SL)"]


# ============ O DESFECHO ENCERRA O TRADE (DA-125) =============================
#
# O defeito grave que isto mata, com o dado REAL da run 20260830-232525-ca31d7
# (LINK-USD 1h): gatilho 11,52 rompido às 13:00, ALVO 11,63 ATINGIDO às 15:00, e
# às 23:00 o preço desabou para 10,99 fechando além do ponto 3. O detector marcou
# `invalidado=True` e a tela disse "INVALIDADO" sobre um trade que tinha GANHO
# oito horas antes — o veredito INVERTIDO em relação ao dinheiro.

from tradingagents.dataflows.price_structure import _desfecho_do_padrao  # noqa: E402


def test_ALVO_apos_o_gatilho_ENCERRA_o_trade_e_a_invalidacao_posterior_NAO_vale():
    """O caso real. DENTE: antes, `invalidado` saía True e o trade ganho virava perda."""
    pat = _pat("2026-08-30 09:00", 11.52, "2026-08-30 23:00")
    c = _crono(_LINK, pat, 11.63, 11.27)
    d = _desfecho_do_padrao(c)
    assert d and d["tipo"] == "alvo"
    assert d["em"] == "2026-08-30 15:00" and d["price"] == 11.63
    assert d["entrada_em"] == "2026-08-30 13:00" and d["entrada"] == 11.52

    pat.desfecho = d
    saiu = pat.as_dict()
    assert saiu["encerrado"] is True
    assert saiu["invalidado"] is False, "trade encerrado no alvo NÃO se invalida depois"
    # o FATO estrutural continua registrado — ele aconteceu, só não decide mais
    assert saiu["invalidado_em"] == "2026-08-30 23:00"


def test_o_INVERSO_stop_antes_do_alvo_encerra_no_STOP():
    serie = _serie([
        ("2026-08-30 09:00", 11.30, 11.40),
        ("2026-08-30 13:00", 11.45, 11.55),   # gatilho
        ("2026-08-30 17:00", 11.20, 11.35),   # stop 11,27
        ("2026-08-30 21:00", 11.55, 11.70),   # o alvo, DEPOIS do stop
    ])
    d = _desfecho_do_padrao(_crono(serie, _pat("2026-08-30 09:00", 11.52), 11.63, 11.27))
    assert d["tipo"] == "stop" and d["em"] == "2026-08-30 17:00"


def test_alvo_e_stop_na_MESMA_barra_conta_o_STOP_e_declara_o_empate():
    """Sem tick não dá pra saber a ordem dentro da barra — a leitura pessimista é
    a mesma do ``_primeiro_toque`` do ledger, e o empate é DECLARADO."""
    serie = _serie([
        ("2026-08-30 09:00", 11.30, 11.40),
        ("2026-08-30 13:00", 11.45, 11.55),   # gatilho
        ("2026-08-30 17:00", 11.20, 11.70),   # a barra toca alvo E stop
    ])
    d = _desfecho_do_padrao(_crono(serie, _pat("2026-08-30 09:00", 11.52), 11.63, 11.27))
    assert d["tipo"] == "stop" and d["empate_na_barra"] is True


def test_SEM_gatilho_rompido_nao_ha_desfecho():
    """Sem entrada não há trade a encerrar — e um alvo roçado por preço que nunca
    acionou o setup não pode virar vitória."""
    serie = _serie([
        ("2026-08-30 09:00", 11.30, 11.40),
        ("2026-08-30 15:00", 11.60, 11.70),   # tocou o alvo sem passar pelo gatilho
    ])
    assert _desfecho_do_padrao(_crono(serie, _pat("2026-08-30 09:00", 99.0), 11.63, 11.27)) is None


def test_padrao_que_MORRE_SEM_ter_acionado_continua_invalidado():
    """A invalidação não perdeu força — ela só deixou de valer DEPOIS do desfecho.

    DENTE do exagero oposto: se a correção zerasse o `invalidado` sempre, um padrão
    que perdeu o ponto 3 sem nunca ter acionado passaria a parecer vivo.
    """
    serie = _serie([
        ("2026-08-30 09:00", 11.30, 11.40),
        ("2026-08-30 15:00", 11.10, 11.20),   # nunca tocou o gatilho
    ])
    pat = _pat("2026-08-30 09:00", 11.52, "2026-08-30 15:00")
    pat.desfecho = _desfecho_do_padrao(_crono(serie, pat, 11.63, 11.27))
    saiu = pat.as_dict()
    assert saiu["encerrado"] is False
    assert saiu["invalidado"] is True, "sem desfecho, a invalidação continua valendo"


def test_desfecho_DEPOIS_da_invalidacao_nao_encerra_nada():
    """Se o padrão morreu ANTES de o preço chegar ao alvo, não houve desfecho de
    trade nenhum — quem entrou já tinha sido encerrado no stop.

    (A ordem é medida pela cronologia; aqui o stop vem antes e é ELE o desfecho.)
    """
    serie = _serie([
        ("2026-08-29 03:00", 11.30, 11.40),
        ("2026-08-29 07:00", 11.45, 11.55),   # gatilho
        ("2026-08-29 11:00", 11.20, 11.35),   # stop → o trade fecha aqui
        ("2026-08-30 15:00", 11.55, 11.70),   # o alvo, muito depois
    ])
    d = _desfecho_do_padrao(_crono(serie, _pat("2026-08-29 03:00", 11.52, "2026-08-29 11:00"),
                                   11.63, 11.27))
    assert d["tipo"] == "stop", "o PRIMEIRO desfecho manda — o alvo posterior não conta"


# ============ PARIDADE ENTRE OS DOIS DETECTORES (DA-126) ======================
#
# A DA-125 corrigiu o veredito invertido só no 1-2-3 deste módulo. O Storm123 tem o
# SEU caminho de morte (`build_storm_plan`, que sobrescreve `invalidado` no dict do
# padrão) e ficou com o defeito de pé. Corrigir um só é PIOR que o bug: os dois
# métodos passariam a discordar sobre a MESMA sequência de preço.
#
# A régua agora é uma função só. Estes testes travam as duas coisas que fazem isso
# continuar verdade: os dois CHAMAM a régua, e a régua decide igual.

import inspect  # noqa: E402

from tradingagents.dataflows import price_structure as _ps  # noqa: E402
from tradingagents.dataflows.price_structure import _morte_e_desfecho  # noqa: E402


def test_os_DOIS_detectores_chamam_a_MESMA_regua():
    """DENTE: uma segunda política de morte é como o defeito sobreviveu à DA-125."""
    storm = inspect.getsource(_ps.build_storm_plan)
    plano = inspect.getsource(_ps.build_actionable_plan)
    assert "_morte_e_desfecho(" in storm, "o Storm voltou a decidir a morte sozinho"
    assert "_morte_e_desfecho(" in plano, "o 1-2-3 voltou a decidir a morte sozinho"
    # e nenhum dos dois volta a cravar `invalidado` direto do `_primeira_barra_alem`
    for fonte, nome in ((storm, "Storm"), (plano, "1-2-3")):
        assert '"invalidado": em is not None,' not in fonte, nome


@pytest.mark.parametrize("caso,esperado", [
    pytest.param("alvo_antes", ("alvo", False), id="alvo_antes_da_morte_ENCERRA_ganhando"),
    pytest.param("stop_antes", ("stop", False), id="stop_antes_da_morte_ENCERRA_perdendo"),
    pytest.param("morte_antes_da_entrada", (None, True), id="morreu_sem_acionar_INVALIDA"),
    pytest.param("sem_gatilho", (None, True), id="alvo_sem_entrada_nao_encerra"),
])
def test_a_REGUA_decide_igual_para_qualquer_chamador(caso, esperado):
    """A mesma sequência, a mesma resposta — venha do 1-2-3 ou do Storm.

    A régua não sabe qual detector a chamou: recebe série, ponto 3, nível de
    invalidação, gatilho, alvo e stop. É por isso que a paridade é estrutural, e
    não uma coincidência a manter à mão.
    """
    series = {
        # ponto3 · gatilho 11,52 · alvo 11,63 · stop 11,27 · invalidação 11,34
        "alvo_antes": _serie([
            ("2026-08-30 09:00", 11.30, 11.40), ("2026-08-30 13:00", 11.45, 11.55),
            ("2026-08-30 15:00", 11.55, 11.65), ("2026-08-30 23:00", 11.00, 11.45)]),
        "stop_antes": _serie([
            ("2026-08-30 09:00", 11.30, 11.40), ("2026-08-30 13:00", 11.45, 11.55),
            ("2026-08-30 17:00", 11.20, 11.35), ("2026-08-30 21:00", 11.55, 11.70)]),
        "morte_antes_da_entrada": _serie([
            ("2026-08-30 09:00", 11.30, 11.40), ("2026-08-30 11:00", 11.10, 11.20),
            ("2026-08-30 15:00", 11.45, 11.70)]),
        "sem_gatilho": _serie([
            ("2026-08-30 09:00", 11.30, 11.40), ("2026-08-30 15:00", 11.60, 11.65),
            ("2026-08-30 23:00", 11.00, 11.20)]),
    }
    df = series[caso]
    gatilho = 11.52 if caso != "sem_gatilho" else 99.0
    em, desf = _morte_e_desfecho(df, 0, 11.34, True, _FMT, gatilho, 11.63, 11.27)
    tipo_esperado, invalida_esperado = esperado
    assert (desf or {}).get("tipo") == tipo_esperado, (caso, em, desf)
    # o veredito EFETIVO: invalidado só quando não houve desfecho
    assert (em is not None and desf is None) is invalida_esperado, (caso, em, desf)


def test_o_FATO_da_invalidacao_sobrevive_ao_desfecho():
    """`invalidado_em` continua sendo devolvido — ele aconteceu, só não decide mais.

    Apagá-lo esconderia que o ponto 3 foi perdido, que é história real da série e é
    o que a linha do tempo mostra ao lado do desfecho.
    """
    df = _serie([
        ("2026-08-30 09:00", 11.30, 11.40), ("2026-08-30 13:00", 11.45, 11.55),
        ("2026-08-30 15:00", 11.55, 11.65), ("2026-08-30 23:00", 11.00, 11.45)])
    em, desf = _morte_e_desfecho(df, 0, 11.34, True, _FMT, 11.52, 11.63, 11.27)
    assert desf["tipo"] == "alvo"
    assert em == "2026-08-30 23:00", "o fato estrutural tem de continuar registrado"


def test_a_leitura_de_referencia_do_Storm_e_a_mais_PROXIMA_do_preco():
    """As duas entradas do Storm são leituras independentes (DA-079); o desfecho
    descreve a que a lista mostra, senão card e linha falariam de trades diferentes."""
    leituras = [{"entrada": "ponto2", "trigger": 11.44}, {"entrada": "ponto3", "trigger": 11.45}]
    assert _ps._leitura_de_referencia(leituras, 11.46)["entrada"] == "ponto3"
    assert _ps._leitura_de_referencia(leituras, 11.30)["entrada"] == "ponto2"
    assert _ps._leitura_de_referencia([], 11.0) is None

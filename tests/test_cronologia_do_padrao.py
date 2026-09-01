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

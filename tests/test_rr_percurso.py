"""O R:R baixo não é o método — é o percurso (task 20260830-008).

Os quatro R:R que o Samyr viu (0,05 · 0,21 · 0,30 · 0,31) não são erro de conta nem
alvo conservador. Depois que o padrão ACIONA, ``_entry_ref`` passa a medir a entrada
a partir do PREÇO ATUAL — honesto, é o que ainda resta de trade — enquanto o stop
continua ancorado na invalidação (ponto 3). A consequência é aritmética: quanto mais
o preço anda na direção do movimento, mais perto do alvo e mais longe do stop ele
fica, e **o R:R desaba à medida que o trade amadurece**.

O caso do print, conferido aqui: venda, stop 526,92 · alvo 460,21 · preço 465,58 →
risco 61,34 contra retorno 5,37 = **0,09**. No gatilho (517,35) o mesmo setup
oferecia **5,97**. O percurso andou **91%**.

A tela mostrava só o 0,09, e a conclusão natural de quem lê é "o método dá trades
ruins" — quando o que houve foi chegar tarde. Estes testes travam a correção:

  * os DOIS números quando o padrão acionou (agora × no gatilho);
  * a régua do percurso, que é MEDIDA e não faixa arbitrária;
  * o motivo escrito, pra o número baixo nunca aparecer sozinho;
  * e o silêncio quando o padrão NÃO acionou — ali a entrada É o gatilho, não há dois
    números a comparar e inventar um segundo seria repetir o mesmo.
"""

import pytest

from tradingagents.dataflows.price_structure import (
    _com_percurso,
    _percurso,
    _risk_reward,
)

_GAT = "gatilho — perda da mínima do ponto 2"

# Os números do print (venda, ação de 465).
STOP = {"price": 526.92}
ALVO = {"price": 460.21}
GATILHO = 517.35


def _rr(preco, state="acionado", stop=STOP, alvo=ALVO, gat=GATILHO, compra=False):
    base = _risk_reward(preco, "preço atual (padrão já acionado)", stop, alvo, compra)
    return _com_percurso(base, gat, state, preco, stop, alvo, compra, _GAT)


# ───────────────────────────── acionado e ESTICADO ───────────────────────────
@pytest.mark.unit
def test_acionado_esticado_mostra_os_dois_rr():
    """O caso do print. O 0,09 continua sendo o número certo do que RESTA — e vem
    acompanhado do 5,97 que o setup ofereceu, que é o que o método entregou."""
    rr = _rr(465.58)
    assert rr["rr"] == 0.09, rr
    assert rr["risk"] == 61.34 and rr["reward"] == 5.37, rr
    assert rr["no_gatilho"]["rr"] == 5.97, rr["no_gatilho"]
    assert rr["no_gatilho"]["entry"] == 517.35, rr["no_gatilho"]
    # o risco do gatilho é MENOR (o stop estava perto) e o retorno MAIOR — é a
    # aritmética inteira do problema, num par de números
    assert rr["no_gatilho"]["risk"] < rr["risk"], rr
    assert rr["no_gatilho"]["reward"] > rr["reward"], rr


@pytest.mark.unit
def test_o_percurso_e_medido_e_o_motivo_e_escrito():
    rr = _rr(465.58)
    assert rr["andado_pct"] == 90.6, rr
    assert rr["sobra_pct"] == 9.4, rr
    assert "andou 91%" in rr["motivo"], rr["motivo"]
    assert "sobra 9%" in rr["motivo"], rr["motivo"]
    assert "o que RESTA" in rr["motivo"], ("a frase tem de dizer o que o número mede",
                                           rr["motivo"])


@pytest.mark.unit
def test_o_percurso_e_a_fracao_gatilho_ate_alvo_e_nada_mais():
    """Medida pura — nenhuma faixa arbitrária no meio. Meio caminho é 50, o gatilho
    é 0 e o alvo é 100, na compra e na venda."""
    alvo_c, gat_c = {"price": 120.0}, 100.0
    assert _percurso(gat_c, 100.0, alvo_c, True) == 0.0
    assert _percurso(gat_c, 110.0, alvo_c, True) == 50.0
    assert _percurso(gat_c, 120.0, alvo_c, True) == 100.0
    alvo_v, gat_v = {"price": 80.0}, 100.0
    assert _percurso(gat_v, 100.0, alvo_v, False) == 0.0
    assert _percurso(gat_v, 90.0, alvo_v, False) == 50.0
    assert _percurso(gat_v, 80.0, alvo_v, False) == 100.0


@pytest.mark.unit
def test_alvo_ja_batido_passa_de_100_e_diz_isso():
    """Nada se arredonda pra caber num rótulo bonito: >100 é fato, e a frase muda —
    "não sobra movimento a projetar" é outra coisa de "sobra pouco"."""
    rr = _rr(455.0)
    assert rr["andado_pct"] > 100, rr
    assert "alvo já foi alcançado" in rr["motivo"], rr["motivo"]


@pytest.mark.unit
def test_preco_voltou_atras_do_gatilho_e_percurso_negativo():
    """O outro extremo: acionou e o preço voltou. O percurso fica NEGATIVO e a frase
    diz que o R:R mede uma entrada a mercado, não a do rompimento."""
    rr = _rr(525.0)
    assert rr["andado_pct"] < 0, rr
    assert "voltou para trás do gatilho" in rr["motivo"], rr["motivo"]


# ──────────────────────────── acionado e RECENTE ─────────────────────────────
@pytest.mark.unit
def test_acionado_recente_tem_os_dois_numeros_e_eles_quase_batem():
    """O contraponto que impede a leitura preguiçosa "acionado = ruim": logo após o
    rompimento os dois R:R são próximos, o percurso é pequeno, e o setup segue
    aproveitável. É por isso que o corte não pode ser o ESTADO, e sim a medida."""
    rr = _rr(512.0)                      # 5,35 abaixo do gatilho, numa perna de 57
    assert rr["andado_pct"] < 10, rr
    assert rr["sobra_pct"] > 90, rr
    assert rr["rr"] > 3, ("acionado há pouco ainda oferece R:R de verdade", rr)
    assert rr["no_gatilho"]["rr"] == 5.97, rr["no_gatilho"]
    assert "sobra 91%" in rr["motivo"], rr["motivo"]


# ────────────────────────────── NÃO acionado ─────────────────────────────────
@pytest.mark.unit
def test_nao_acionado_nao_ganha_segundo_numero():
    """Ali a entrada É o gatilho: um "no gatilho" seria o mesmo número duas vezes, e
    repetir dado é o defeito que a DA-077 combate."""
    for estado in ("formando", "rompeu_retracou"):
        rr = _rr(517.35, state=estado)
        assert "no_gatilho" not in rr, (estado, rr)
        assert "andado_pct" not in rr, (estado, rr)
        assert "motivo" not in rr, (estado, rr)


@pytest.mark.unit
def test_sem_alvo_nao_ha_percurso_a_medir_e_o_rr_continua_dizendo_por_que():
    """Sem alvo o caminho não existe. O que NÃO pode é sumir a explicação — o
    ``note`` da task 006 continua lá."""
    rr = _rr(465.58, alvo=None)
    assert rr["rr"] is None and "sem alvo estrutural" in rr["note"], rr
    assert "andado_pct" not in rr, rr
    # e com o alvo ATRÁS do gatilho o percurso também não se inventa
    rr2 = _rr(465.58, alvo={"price": 530.0})
    assert "andado_pct" not in rr2, rr2


@pytest.mark.unit
def test_o_rr_de_agora_nao_mudou_de_valor():
    """Não-regressão do que já existia: a correção ACRESCENTA contexto, não mexe no
    número que a tela já mostrava (nem no ``note``, nem na base da entrada)."""
    cru = _risk_reward(465.58, "preço atual (padrão já acionado)", STOP, ALVO, False)
    rico = _rr(465.58)
    for k in ("entry", "entry_basis", "risk", "reward", "rr", "note"):
        assert rico[k] == cru[k], (k, rico[k], cru[k])


# ─────────────────────── o scan não empilha esticado com fresco ───────────────
@pytest.mark.unit
def test_o_scan_ordena_pelo_que_SOBRA_dentro_de_em_movimento():
    """Critério do Samyr: "em_gatilho (aproveitável) não pode competir de igual pra
    igual com acionado e esticado". A urgência já separava os ESTADOS; dentro de
    ``em_movimento`` a lista empilhava pela distância do gatilho — que num acionado
    mede o quanto ele já FUGIU, ou seja, ordenava ao contrário do interesse.

    Agora ordena pela medida: quem tem mais movimento pela frente vem antes. Sem
    faixa arbitrária — é o próprio percurso que ordena.
    """
    from tradingagents.webui.scanner import _URGENCIA, _resto

    esticado = {"estado": "em_movimento", "sobra_pct": 9.4, "dist_pct": 0.10}
    fresco = {"estado": "em_movimento", "sobra_pct": 91.0, "dist_pct": 0.01}
    gatilho = {"estado": "em_gatilho", "sobra_pct": None, "dist_pct": 0.002}

    def chave(r):
        return (_URGENCIA.get(r["estado"], 9), -_resto(r), r["dist_pct"])

    ordenado = sorted([esticado, fresco, gatilho], key=chave)
    assert ordenado[0] is gatilho, "entrada viva continua em primeiro"
    assert ordenado[1] is fresco, ("dentro de em_movimento, quem tem mais caminho "
                                   "pela frente vem antes", ordenado)
    assert ordenado[2] is esticado, ordenado
    # sem percurso (padrão não acionado) nada foi consumido: 100
    assert _resto({"sobra_pct": None}) == 100.0
    assert _resto({"sobra_pct": 9.4}) == 9.4

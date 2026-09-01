"""CICLO DE VIDA DO PADRÃO — forma, aciona, desfecha, e vira HISTÓRIA (DA-129).

Samyr: *"se já alcançou o alvo e voltou o setup continua válido? ou ele vira
história?"* — **vira história**. O padrão morre no PRIMEIRO desfecho depois do
rompimento do gatilho, e a partir dali nada posterior o reabre, o invalida ou o
reclassifica.

A DA-126 já tinha posto a régua do desfecho numa função só, chamada pelos dois
métodos. O que faltava é o que este módulo prova: **só o 1-2-3 tinha aprendido a
LER o resultado dela**. O Storm calculava o desfecho e o jogava fora em toda
superfície — a linha do scan dizia "vetado", o card dizia "NÃO OPERA", e um padrão
invalidado dele saía "formando" numa lista onde o do 1-2-3 saía "invalidado".

Uma linha que consulta o filtro do Éden sobre um padrão ENCERRADO está perguntando
"vale a pena entrar?" sobre um trade que não existe mais.
"""

import inspect

import pytest

from tests.test_scan_storm import _plano_storm
from tradingagents.dataflows import price_structure as _ps
from tradingagents.dataflows.price_structure import (
    CICLO,
    CICLO_ENCERRADO,
    ciclo_de_vida,
)
from tradingagents.webui import scanner as sc


# ------------------------------------------------- a autoridade, sozinha ------
@pytest.mark.parametrize("acionado,inval,desfecho,esperado", [
    (None, None, None, "nunca_acionou"),
    ("2026-08-30 13:00", None, None, "vivo"),
    ("2026-08-30 13:00", "2026-08-30 23:00", {"tipo": "alvo"}, "concluido_alvo"),
    ("2026-08-30 13:00", None, {"tipo": "stop"}, "concluido_stop"),
    (None, "2026-08-30 11:00", None, "invalidado_sem_acionar"),
    ("2026-08-30 13:00", "2026-08-30 17:00", None, "invalidado_operando"),
])
def test_os_SEIS_valores_do_ciclo(acionado, inval, desfecho, esperado):
    """O briefing pedia CINCO. O sexto existe na série e não podia ser dobrado
    dentro do quinto: um padrão que ENTROU e depois perdeu o ponto 3 sem tocar alvo
    nem stop não é a mesma coisa que um que morreu sem ninguém ter entrado — no
    primeiro alguém estava posicionado quando a premissa rompeu."""
    assert ciclo_de_vida(acionado_em=acionado, invalidado_em=inval,
                         desfecho=desfecho) == esperado
    assert esperado in CICLO


def test_o_DESFECHO_decide_ANTES_da_invalidacao():
    """DENTE: a ordem das perguntas É a regra. Invertida, o LINK-USD volta a sair
    'invalidado' oito horas depois de ter atingido o alvo."""
    assert ciclo_de_vida(acionado_em="13:00", invalidado_em="23:00",
                         desfecho={"tipo": "alvo"}) == "concluido_alvo"
    assert CICLO_ENCERRADO == ("concluido_alvo", "concluido_stop")


def test_uma_invalidacao_POSTERIOR_nao_reabre_nem_reclassifica():
    """'Nada posterior o reabre': o ciclo de um encerrado não muda por nada que a
    série faça depois — nem morte, nem novo toque no gatilho."""
    base = ciclo_de_vida(acionado_em="13:00", invalidado_em=None,
                         desfecho={"tipo": "alvo"})
    for depois in (None, "23:00", "2099-01-01 00:00"):
        assert ciclo_de_vida(acionado_em="13:00", invalidado_em=depois,
                             desfecho={"tipo": "alvo"}) == base


# ------------------------------- a paridade, na superfície que a pessoa lê ----
_ENCERRADO = {"tipo": "alvo", "em": "2026-08-30 15:00", "price": 11.63,
              "entrada_em": "2026-08-30 13:00", "entrada": 11.52,
              "empate_na_barra": False}


def _pat_encerrado(**over):
    p = {"p1": {}, "p2": {"low": 90.0}, "p3": {}, "direction": "compra",
         "amplitude": 20.0, "entradas": [], "invalidado": False,
         "invalidado_em": "2026-08-30 23:00", "desfecho": _ENCERRADO,
         "encerrado": True, "acionado_em": "2026-08-30 13:00",
         "ciclo": "concluido_alvo"}
    p.update(over)
    return p


def test_o_STORM_publica_concluido_na_linha_do_scan(monkeypatch):
    """O defeito que esta task veio matar: o Storm calculava o desfecho desde a
    DA-126 e a linha do scan nunca o dizia."""
    monkeypatch.setattr(sc, "build_storm_plan_dict",
                        lambda *a, **k: _plano_storm(pattern=_pat_encerrado()))
    linha = sc._storm_row("X", "2026-08-29", "1d", 107.5)
    assert linha["estado"] == "concluido", linha
    assert linha["desfecho"] == _ENCERRADO, "o desfecho tem de viajar na linha"
    assert linha["ciclo"] == "concluido_alvo"


def test_o_VETO_do_Eden_NAO_fala_sobre_historia(monkeypatch):
    """DENTE, e o caso mais insidioso: com o Éden desalinhado a linha saía 'vetado'
    — o filtro respondendo 'não vale a pena entrar' sobre um trade que já fechou."""
    monkeypatch.setattr(
        sc, "build_storm_plan_dict",
        lambda *a, **k: _plano_storm(opera=False, pattern=_pat_encerrado()))
    linha = sc._storm_row("X", "2026-08-29", "1d", 107.5)
    assert linha["estado"] == "concluido", ("o veto engoliu o desfecho", linha)


def test_o_STORM_invalidado_deixa_de_sair_FORMANDO(monkeypatch):
    """Antes disto a coluna do Storm não tinha como dizer 'morreu': um padrão
    invalidado saía 'formando' — e, com o preço de volta perto do nível, 'gatilho'.
    O do 1-2-3 já dizia 'invalidou' na mesma lista."""
    morto = _pat_encerrado(invalidado=True, desfecho=None, encerrado=False,
                           acionado_em=None, ciclo="invalidado_sem_acionar")
    monkeypatch.setattr(sc, "build_storm_plan_dict",
                        lambda *a, **k: _plano_storm(pattern=morto))
    assert sc._storm_row("X", "2026-08-29", "1d", 107.5)["estado"] == "invalidou"


@pytest.mark.parametrize("ciclo,estado", [
    ("concluido_alvo", "concluido"), ("concluido_stop", "concluido"),
    ("invalidado_sem_acionar", "invalidou"), ("invalidado_operando", "invalidou"),
    ("vivo", None), ("nunca_acionou", None),
])
def test_a_traducao_ciclo_ESTADO_e_uma_so(ciclo, estado):
    """PARIDADE ESTRUTURAL: os dois métodos passam pela mesma função. Enquanto for
    assim, não há caminho por onde um publique 'vetado' e o outro 'encerrado' sobre
    a mesma sequência de preço. ``None`` = o ciclo ainda não decide, e aí cada
    método usa a sua distância do gatilho — que é dele por natureza."""
    assert sc._estado_do_ciclo({"ciclo": ciclo}) == estado


def test_os_DOIS_row_builders_consultam_a_traducao():
    """DENTE de regressão: foi um caminho paralelo (o Storm decidindo o estado
    sozinho) que deixou a DA-126 pela metade."""
    for fn, nome in ((sc._frame_row, "1-2-3"), (sc._storm_row, "Storm")):
        assert "_estado_do_ciclo(" in inspect.getsource(fn), nome


def test_o_ciclo_viaja_nos_DOIS_planos():
    """O campo tem de existir nos dois lados: o Storm o monta no dict, o 1-2-3 no
    ``as_dict`` do padrão."""
    assert '"ciclo": ciclo_de_vida(' in inspect.getsource(_ps.build_storm_plan)
    assert '"ciclo": ciclo_de_vida(' in inspect.getsource(_ps.Pattern123.as_dict)


# ------------------------------------------------ ciclo do padrão ≠ saída -----
def test_ciclo_do_PADRAO_e_politica_de_SAIDA_sao_SEPARADOS():
    """Quem opera saída FRACIONADA realiza parte no alvo e deixa o resto correr —
    então há posição viva depois do alvo. Isso NÃO reabre o padrão: a régua do
    remanescente é BE/trailing, que mora em ``execucao.py``, e nasce desligada.

    O teste existe pra ninguém 'consertar' o ciclo enfiando essa nuance nele: a
    autoridade do ciclo não pode conhecer política de saída."""
    fonte = inspect.getsource(_ps.ciclo_de_vida)
    for palavra in ("break-even", "breakeven", "trailing", "parcial", "fracion"):
        assert palavra not in fonte.lower(), (
            "política de SAÍDA entrou na autoridade do CICLO", palavra)
    # e o lugar dela continua sendo o outro módulo, com os defaults intactos: BE e
    # trailing NASCEM DESLIGADOS porque o método compra o recuo à média, e ligá-los
    # ejetaria o trade no pullback em que ele estaria ADICIONANDO.
    from tradingagents.webui.execucao import protecao
    prot = protecao()
    assert prot["be"]["ligado"] is False and prot["trailing"]["ligado"] is False, prot
    # a régua do remanescente é ESTA, e não a invalidação do padrão
    assert "média" in prot["trailing"]["referencia"].lower()

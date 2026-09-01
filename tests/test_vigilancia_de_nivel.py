"""VIGILÂNCIA DE NÍVEL — o preço AVISA, o fechamento DECIDE (DA-138).

O buraco que o Samyr achou: *"deu um setup num certo horário, só que durante a
próxima hora ele conseguiu invalidar. A gente só vai ver 45 minutos depois."*

A parte que **não** tem buraco, e que este módulo não pode "consertar": a
invalidação ESTRUTURAL é medida por FECHAMENTO — ela não existe antes de o candle
fechar, então varrer a cada fechamento não atrasa nada.

A parte que tem: o **stop** é executado no PREÇO, intrabar, e o **alvo** pode ser
tocado e devolvido dentro da mesma hora — foi exatamente o LINK-USD da DA-125.

O risco desta ideia é um só, e os testes abaixo existem por causa dele: a
vigilância virar uma **segunda fonte de verdade** capaz de discordar da primeira.
Por isso ela devolve fato com hora e fonte declarada, e nunca estado de padrão.
"""

import inspect

import pytest

from tradingagents.webui import vigilancia as vg
from tradingagents.webui.vigilancia import cruzamentos


def _frames(**over):
    linha = {"frame": "1h", "estado": "em_movimento", "direction": "compra",
             "sl": 10.0, "tp": 12.0, "trigger": 11.0}
    linha.update(over)
    return [linha]


def test_o_STOP_perfurado_vira_aviso_com_hora_e_fonte():
    c = cruzamentos(_frames(), 9.5, 10.5, quando="2026-09-01 15:20")
    assert len(c) == 1, c
    assert c[0]["nivel"] == "stop" and c[0]["metodo"] == "Setup123"
    assert c[0]["quando"] == "2026-09-01 15:20"
    assert c[0]["fonte"] == "preço", "sem a fonte, isto se confunde com o veredito"


def test_o_MESMO_stop_nao_avisa_de_novo_a_cada_janela():
    """DENTE: sem a cotação anterior, o preço parado abaixo do stop geraria um
    aviso novo a cada 40 segundos — e o card viraria uma lista do mesmo fato."""
    assert cruzamentos(_frames(), 9.5, 10.5)          # atravessou agora
    assert not cruzamentos(_frames(), 9.4, 9.5)       # continua lá: nada novo
    assert not cruzamentos(_frames(), 9.0, 9.4)


@pytest.mark.parametrize("direcao,preco,esperado", [
    ("compra", 12.5, "alvo"),     # subiu até o alvo
    ("compra", 9.5, "stop"),      # caiu até o stop
    ("venda", 9.5, "alvo"),       # numa VENDA, cair é ir na direção do trade
    ("venda", 12.5, "stop"),
])
def test_a_DIRECAO_decide_o_que_e_alvo_e_o_que_e_stop(direcao, preco, esperado):
    """Sem isto, uma venda teria alvo e stop invertidos — o pior erro possível
    numa leitura que existe pra avisar de perda."""
    # NUMA VENDA os níveis invertem de lado: o stop fica ACIMA da entrada e o alvo
    # ABAIXO. (Os números da primeira versão deste teste punham o stop da venda em
    # 13,0 e mandavam o preço a 12,5 — que não alcança nada; o teste estava certo,
    # a fixture é que descrevia um trade impossível.)
    f = _frames(direction=direcao,
                sl=10.0 if direcao == "compra" else 12.0,
                tp=12.0 if direcao == "compra" else 10.0)
    c = cruzamentos(f, preco, 11.0)
    assert [x["nivel"] for x in c] == [esperado], (direcao, preco, c)


def test_padrao_ENCERRADO_ou_INVALIDADO_nao_tem_nivel_a_vigiar():
    """DA-129: um trade que terminou é história — o preço passando por ali de novo
    não é evento dele. Vigiar isso reabriria pelo preço o que a régua fechou."""
    for morto in ("concluido", "invalidou"):
        assert cruzamentos(_frames(estado=morto), 9.5, 10.5) == []


def test_os_DOIS_metodos_sao_vigiados_com_os_SEUS_niveis():
    f = _frames(storm={"estado": "em_gatilho", "direction": "venda",
                       "sl": 13.0, "tp": 9.0, "trigger": 11.5})
    # anterior 11,8: acima do gatilho do Storm (11,5), pra que ele seja ATRAVESSADO
    c = cruzamentos(f, 9.5, 11.8)
    por = {x["metodo"]: x["nivel"] for x in c}
    assert por.get("Setup123") == "stop", c      # compra caindo = stop
    assert por.get("Storm123") == "gatilho", c   # venda: 11,5 foi atravessado
    assert len({x["metodo"] for x in c}) == 2, c


def test_SEM_preco_nao_ha_o_que_vigiar():
    """Fail-open honesto: sem cotação, silêncio — nunca um aviso inventado."""
    assert cruzamentos(_frames(), None, 10.5) == []
    assert cruzamentos([], 9.5, 10.5) == []


def test_a_vigilancia_NAO_recalcula_estrutura():
    """O guard que impede ela de virar uma segunda varredura: o módulo não importa
    detector, não carrega série e não chama a régua do desfecho. Se um dia
    precisar disso, deixou de ser barata — e deixou de ser vigilância."""
    fonte = inspect.getsource(vg)
    for proibido in ("load_ohlcv", "load_intraday", "detect_price_structure",
                     "build_actionable_plan", "build_storm_plan", "_morte_e_desfecho",
                     "fetch_live_price", "import pandas"):
        assert proibido not in fonte, ("a vigilância passou a recalcular", proibido)


def test_ela_nao_produz_ESTADO_de_padrao():
    """Ela AVISA; o fechamento DECIDE. Um campo de estado aqui seria o começo da
    segunda fonte de verdade — o card leria dois vereditos e teria de escolher."""
    c = cruzamentos(_frames(), 9.5, 10.5)
    for chave in ("estado", "ciclo", "invalidado", "desfecho", "veredito"):
        assert chave not in c[0], (chave, c[0])
    assert set(c[0]) == {"frame", "metodo", "nivel", "preco_nivel", "preco",
                         "quando", "direcao", "texto", "fonte"}

"""Os dois alertas do Telegram — e a separação de destino (DA-149).

O que este arquivo protege não é o formato da mensagem: é **para onde cada fonte
pode ir**. A carteira do Erick é conteúdo de assinatura paga; mandá-la a um grupo
seria redistribuir conteúdo pago de terceiro. Por isso o destino é regra, não
configuração — e há teste para o dia em que alguém "só trocar o chat_id no .env".
"""

import pytest

from tradingagents.webui import alertas_tg as A

pytestmark = pytest.mark.unit


def _cart(ativos, feed=None, atualizado="27/08/2026"):
    return {"carteira": {"atualizado": atualizado, "ativos": ativos,
                         "feed": feed or []}}


_ANTES = _cart([
    {"ticker": "MSFT", "nome": "Microsoft", "classe": "Acao", "qtd": 60.0, "precoMedio": 381.93},
    {"ticker": "IREN", "nome": "IREN", "classe": "Acao", "qtd": 240.0, "precoMedio": 41.79},
    {"ticker": "CASH", "nome": "Caixa", "classe": "Caixa", "qtd": 40000.0, "precoMedio": 1},
])
_DEPOIS = _cart(
    [
        {"ticker": "MSFT", "nome": "Microsoft", "classe": "Acao", "qtd": 22.108, "precoMedio": 381.93},
        {"ticker": "ASTS", "nome": "AST SpaceMobile", "classe": "Acao", "qtd": 147.45, "precoMedio": 67.82},
        {"ticker": "CASH", "nome": "Caixa", "classe": "Caixa", "qtd": 84829.22, "precoMedio": 1},
    ],
    feed=[{"ticker": "IREN", "resumo": "Zerei a posição. O motivo é timing, não tese."}],
)


# ── (A) a separação de destino ────────────────────────────────────────────────
@pytest.mark.parametrize("chat", [-1001234567890, -100200300, -1])
def test_DENTE_a_carteira_do_Erick_NUNCA_vai_pra_grupo(chat):
    """É conteúdo de assinatura paga. O Samyr é aluno e pode consumir; a comunidade
    dele não comprou — mandar isso a um grupo é REDISTRIBUIR conteúdo de terceiro.
    A regra é mecânica de propósito: id negativo é grupo no Telegram, e a recusa
    não depende de ninguém lembrar do motivo meses depois."""
    ok, motivo = A.destino_valido(A.FONTE_CARTEIRA, chat)
    assert ok is False
    assert "assinatura" in motivo and "grupo" in motivo, motivo


def test_a_carteira_vai_pra_DM_do_dono(_=None):
    ok, _motivo = A.destino_valido(A.FONTE_CARTEIRA, 30289486)
    assert ok is True


@pytest.mark.parametrize("chat", [-1001234567890, 30289486])
def test_os_SINAIS_DO_PRODUTO_podem_ir_aos_dois(chat):
    """São gerados pelo sistema DELE sobre dado de mercado público: ele distribui o
    que é dele. O teto de distribuição é outro porque a origem é outra."""
    ok, _ = A.destino_valido(A.FONTE_SINAIS, chat)
    assert ok is True


def test_chat_id_invalido_e_recusa_e_nao_exceção():
    ok, motivo = A.destino_valido(A.FONTE_SINAIS, "nao-e-numero")
    assert ok is False and "inválido" in motivo


# ── (A) o que mudou ───────────────────────────────────────────────────────────
def test_detecta_entrada_saida_e_reducao_com_o_pct_do_CAPITAL():
    m = {x["ticker"]: x for x in A.mudancas(_ANTES, _DEPOIS)}
    assert set(m) == {"MSFT", "IREN", "ASTS", "CASH"}, m
    assert m["IREN"]["tipo"] == "saiu"
    assert m["ASTS"]["tipo"] == "entrou"
    assert m["MSFT"]["tipo"] == "reduziu"
    assert m["CASH"]["tipo"] == "aumentou"
    # o % é sobre o capital de HOJE — é o que responde "o quanto isso importa"
    for x in m.values():
        assert 0 < x["pct_capital"] < 1, x
    # e a redução de MSFT (2/3 de uma posição grande) pesa mais que a entrada nova
    assert m["MSFT"]["pct_capital"] > m["ASTS"]["pct_capital"], m


def test_a_PRIMEIRA_leitura_nao_e_mudanca():
    """DENTE: sem isto, o primeiro alerta anunciaria a carteira inteira como
    novidade — e ensinaria o leitor a ignorar o canal no primeiro dia."""
    assert A.mudancas(None, _DEPOIS) == []
    assert A.mudancas(_DEPOIS, None) == []


def test_quantidade_igual_NAO_e_mudanca():
    assert A.mudancas(_ANTES, _ANTES) == []


def test_nada_mudou_e_SILENCIO_nao_e_mensagem_vazia_enviada():
    """"Nenhuma mudança hoje" todo dia é o ruído que faz o alerta deixar de ser
    lido. String vazia é o contrato com quem envia: não envie."""
    assert A.formata_carteira([], _DEPOIS) == ""


def test_a_mensagem_traz_ATIVO_o_QUE_mudou_e_o_PCT_e_o_racional_dele():
    txt = A.formata_carteira(A.mudancas(_ANTES, _DEPOIS), _DEPOIS)
    assert "IREN" in txt and "SAIU" in txt and "🔴⬇" in txt
    assert "% do capital" in txt
    # o racional é o que dá sentido: "saiu de IREN" sem o porquê é um número
    assert "timing, não tese" in txt
    assert "27/08/2026" in txt
    # DA-033: sem tabela markdown, e com respiro entre blocos
    assert "|---" not in txt and "\n\n" in txt


# ── DA-034: o formato lúdico aprovado pelo Samyr, com dente ────────────────────
def test_DENTE_a_carteira_nao_tem_UM_asterisco_sequer():
    """Telegram mostra o asterisco cru — markdown aqui não vira negrito, vira
    poluição visual. Ênfase é emoji e caixa alta, não `*texto*` (DA-034)."""
    txt = A.formata_carteira(A.mudancas(_ANTES, _DEPOIS), _DEPOIS)
    assert "*" not in txt


def test_DENTE_nenhuma_linha_passa_de_60_chars():
    """Racional pode chegar a 400 chars — sem quebra isso é parede de texto no
    Telegram. formata_carteira tem que embrulhar, não despejar (DA-033)."""
    racional_longo = ("Reduzi bastante essa posição porque o setup técnico de "
                      "curto prazo enfraqueceu muito depois do resultado "
                      "trimestral, e prefiro esperar confirmação antes de voltar.")
    depois_com_racional = _cart(_DEPOIS["carteira"]["ativos"],
                                feed=[{"ticker": "IREN", "resumo": racional_longo}])
    txt = A.formata_carteira(A.mudancas(_ANTES, depois_com_racional), depois_com_racional)
    for linha in txt.split("\n"):
        assert len(linha) <= 60, linha


def test_DENTE_blocos_separados_por_linha_em_branco():
    """Cabeçalho, movimentos e o fecho de ação são blocos DIFERENTES — sem respiro
    entre eles vira parede de texto (DA-033)."""
    txt = A.formata_carteira(A.mudancas(_ANTES, _DEPOIS), _DEPOIS)
    assert txt.count("\n\n") >= 2


def test_a_mensagem_fecha_com_a_ACAO_em_linha_propria():
    """Fecho com 👉 aponta o que olhar primeiro — o maior movimento do lote."""
    txt = A.formata_carteira(A.mudancas(_ANTES, _DEPOIS), _DEPOIS)
    ultima = txt.strip().split("\n")[-1]
    assert ultima.startswith("👉")


def test_o_cabecalho_traz_total_e_caixa():
    txt = A.formata_carteira(A.mudancas(_ANTES, _DEPOIS), _DEPOIS)
    assert "Total:" in txt and "caixa" in txt


# ── (B) sinais ────────────────────────────────────────────────────────────────
def _f(frame, estado, direction="compra", rr=2.0, trigger=101.0):
    return {"frame": frame, "estado": estado, "direction": direction, "rr": rr,
            "trigger": trigger, "sl": 99.0, "tp": 105.0}


def test_sinal_exige_gatilho_confluencia_E_RR_acima_do_piso():
    scan = {"ativos": [
        {"ticker": "BOM", "frames": [_f("1d", "em_gatilho"), _f("4h", "em_gatilho")]},
        {"ticker": "SO1FRAME", "frames": [_f("1d", "em_gatilho"), _f("4h", "formando")]},
        {"ticker": "RRBAIXO", "frames": [_f("1d", "em_gatilho", rr=0.4),
                                         _f("4h", "em_gatilho", rr=0.9)]},
        {"ticker": "DIRDIFERENTE", "frames": [_f("1d", "em_gatilho", "compra"),
                                              _f("4h", "em_gatilho", "venda")]},
    ]}
    tickers = [s["ticker"] for s in A.sinais_dignos(scan)]
    assert tickers == ["BOM"], tickers


def test_o_piso_de_RR_NAO_e_1_e_isso_e_a_regra_e_nao_o_acaso():
    """Medido neste produto: a maioria dos 1-2-3 não paga 1:1 nem no próprio
    gatilho. Mandar R:R 0,4 pra uma comunidade é distribuir um trade que perde
    dinheiro por construção."""
    assert A.RR_MINIMO > 1.0
    scan = {"ativos": [{"ticker": "X", "frames": [_f("1d", "em_gatilho", rr=1.1),
                                                  _f("4h", "em_gatilho", rr=1.2)]}]}
    assert A.sinais_dignos(scan) == []


def test_o_piso_incide_sobre_o_MELHOR_frame_nao_sobre_a_media():
    """Média esconde um 0,3 atrás de um 2,7 — e o que se anuncia é o melhor."""
    scan = {"ativos": [{"ticker": "X", "frames": [_f("1d", "em_gatilho", rr=0.3),
                                                  _f("4h", "em_gatilho", rr=2.7)]}]}
    s = A.sinais_dignos(scan)
    assert len(s) == 1 and s[0]["rr"] == 2.7 and s[0]["frame_lider"] == "4h"


def test_a_CHAVE_do_sinal_dedupa_o_mesmo_gatilho():
    """O mesmo gatilho no mesmo frame é o MESMO sinal, quantas vezes a agenda passe
    por ele. Sem isto o grupo recebe o mesmo alerta de hora em hora — a maneira
    mais rápida de ensinar todo mundo a ignorar o canal."""
    a = A.chave_do_sinal("NVDA", _f("4h", "em_gatilho", trigger=101.0))
    b = A.chave_do_sinal("NVDA", _f("4h", "em_gatilho", trigger=101.0))
    c = A.chave_do_sinal("NVDA", _f("4h", "em_gatilho", trigger=102.0))
    d = A.chave_do_sinal("NVDA", _f("1d", "em_gatilho", trigger=101.0))
    assert a == b and a != c and a != d


def test_sem_sinal_a_mensagem_e_vazia_e_ninguem_manda_nada():
    assert A.formata_sinais([]) == ""


def test_a_mensagem_de_sinal_DECLARA_o_criterio_que_a_produziu():
    """Um grupo que recebe "NVDA COMPRA" sem saber o que qualificou aquilo não tem
    como calibrar confiança — e é assim que sinal vira conselho disfarçado."""
    scan = {"ativos": [{"ticker": "NVDA", "frames": [_f("1d", "em_gatilho"),
                                                     _f("4h", "em_gatilho")]}]}
    txt = A.formata_sinais(A.sinais_dignos(scan), quando="2026-09-01 14:01")
    assert "*NVDA*" in txt and "COMPRA" in txt
    assert "Critério:" in txt and "R:R" in txt
    assert "2026-09-01 14:01" in txt
    assert "|---" not in txt

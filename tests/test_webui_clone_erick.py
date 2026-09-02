"""Carteira-espelho que CLONA as entradas/saídas do Erick em paper (task
20260902-055).

O que estes testes protegem, acima de tudo, é a REGRA INEGOCIÁVEL: o preço de
entrada do clone é o preço REAL do instante em que NÓS detectamos a mudança — nunca
o ``precoMedio`` dele. Há um teste-dente montado pra dar sinal TROCADO (lucro↔prejuízo)
se alguém um dia ligar a conta ao preço dele: :func:`test_dente_preco_dele_inverte_o_sinal`.
"""

from datetime import datetime, timezone

import pytest

from tradingagents.webui import clone_erick as C

pytestmark = pytest.mark.unit


def _atual(ativos, atualizado="27/08/2026", lido_em=None):
    if lido_em is None:
        lido_em = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc).timestamp()
    return {"lido_em": lido_em,
            "carteira": {"atualizado": atualizado, "ativos": ativos, "feed": []}}


def _mud(ticker, tipo, peso_agora, qtd_antes=None, qtd_agora=None, classe="Acao"):
    return {"ticker": ticker, "tipo": tipo, "classe": classe,
            "peso_agora": peso_agora, "qtd_antes": qtd_antes,
            "qtd_agora": qtd_agora, "nome": ticker, "pct_capital": peso_agora}


# ── a regra: preço REAL da detecção, nunca o precoMedio dele ─────────────────────
def test_op_usa_preco_real_da_deteccao_nao_o_preco_medio_dele():
    atual = _atual([
        {"ticker": "MSFT", "classe": "Acao", "qtd": 22.1, "precoMedio": 381.93,
         "entrada": "jul/2026"}])
    m = _mud("MSFT", "entrou", 0.30, qtd_antes=None, qtd_agora=22.1)
    # A fonte de cotação REAL diz 520 AGORA; o preço médio DELE é 381.93.
    preco_real = {"price": 520.0, "sessao": "regular", "rotulo": "cotação agora"}
    op = C.op_de_mudanca(m, atual, preco_real)

    assert op["preco"] == 520.0                     # o REAL entrou na conta
    assert op["preco"] != op["dele_precoMedio"]     # NÃO é o preço dele
    assert op["dele_precoMedio"] == 381.93          # e o dele fica só como auditoria
    assert op["incluido"] is True


def test_dente_preco_dele_inverte_o_sinal():
    """O DENTE: se alguém trocar o preço real pelo `precoMedio` dele, este teste
    quebra — porque o clone passa de LUCRO a PREJUÍZO.

    BTC: ele tem preço médio 62.485 (entrou lá em cima); a cotação REAL quando NÓS
    detectamos é 50.000. Com o preço de agora em 55.000:
      • honesto  (entrada 50.000 → 55.000): +10% → não-realizado POSITIVO;
      • preço dele (entrada 62.485 → 55.000): −12% → não-realizado NEGATIVO.
    """
    atual = _atual([
        {"ticker": "BTC", "classe": "Cripto", "qtd": 0.02, "precoMedio": 62485.0,
         "entrada": "jul/2026"}])
    m = _mud("BTC", "entrou", 0.20, qtd_agora=0.02, classe="Cripto")
    op = C.op_de_mudanca(m, atual, {"price": 50000.0, "sessao": "24h",
                                    "rotulo": "cotação agora"})
    r = C.replay([op], precos_atuais={"BTC": 55000.0})

    assert op["preco"] == 50000.0
    assert r["nao_realizado"] > 0                    # honesto: LUCRO
    # o número exato: 0.20 * 70000 * (55000-50000)/50000 = 1400
    assert r["nao_realizado"] == pytest.approx(1400.0, rel=1e-6)


# ── replica o PESO, não a quantidade dele ───────────────────────────────────────
def test_replica_peso_nao_quantidade():
    atual = _atual([
        {"ticker": "ASTS", "classe": "Acao", "qtd": 147.44, "precoMedio": 67.82,
         "entrada": "ago/2026"}])
    m = _mud("ASTS", "entrou", 0.25, qtd_agora=147.44)   # ele tem 147,44 ações
    op = C.op_de_mudanca(m, atual, {"price": 40.0})
    r = C.replay([op])
    abertos = r["posicoes_abertas"]["ASTS"]
    # nossas unidades saem do PESO (0,25 * 70000 / 40 = 437,5), não das 147,44 dele
    assert abertos["units"] == pytest.approx(0.25 * C.CLONE_CAPITAL / 40.0)
    assert abertos["units"] != pytest.approx(147.44)


# ── cobre entrada / saída / aumento / redução ───────────────────────────────────
def test_registrar_cobre_os_quatro_eventos_e_pula_caixa(tmp_path):
    led = tmp_path / "operacoes.jsonl"
    atual = _atual([
        {"ticker": "MSFT", "classe": "Acao", "qtd": 30, "precoMedio": 381.93, "entrada": "jul/2026"},
        {"ticker": "BE", "classe": "Acao", "qtd": 10, "precoMedio": 181.38, "entrada": "jul/2026"},
        {"ticker": "GOOGL", "classe": "Acao", "qtd": 5, "precoMedio": 327.81, "entrada": "jul/2026"},
        {"ticker": "CASH", "classe": "Caixa", "qtd": 5000, "precoMedio": 1, "entrada": "-"},
    ])
    mudou = [
        _mud("MSFT", "entrou", 0.30, qtd_agora=30),
        _mud("BE", "saiu", 0.0, qtd_antes=10, qtd_agora=None),
        _mud("GOOGL", "aumentou", 0.15, qtd_antes=3, qtd_agora=5),
        _mud("ASTS", "reduziu", 0.05, qtd_antes=200, qtd_agora=147, classe="Acao"),
        _mud("CASH", "aumentou", 0.10, classe="Caixa"),      # caixa NÃO vira operação
    ]
    precos = {"MSFT": 500.0, "BE": 180.0, "GOOGL": 300.0, "ASTS": 40.0}
    ops = C.registrar(mudou, atual, preco_fn=lambda t, c: {"price": precos.get(t)},
                      path=led)

    tipos = {o["ticker"]: o["tipo"] for o in ops}
    assert tipos == {"MSFT": "entrou", "BE": "saiu",
                     "GOOGL": "aumentou", "ASTS": "reduziu"}   # caixa fora
    assert led.exists() and len(led.read_text().splitlines()) == 4
    assert C.carrega_ledger(led) == ops                        # relê o que gravou


def test_ciclo_entrou_e_saiu_realiza_com_nossos_precos():
    """entrou a 100, saiu a 150 (NOSSOS preços) → +50% do peso investido, realizado."""
    atual = _atual([{"ticker": "X", "classe": "Acao", "qtd": 1, "precoMedio": 999,
                     "entrada": "jul/2026"}])
    entrou = C.op_de_mudanca(_mud("X", "entrou", 0.40, qtd_agora=1), atual, {"price": 100.0})
    saiu = C.op_de_mudanca(_mud("X", "saiu", 0.0, qtd_antes=1), atual, {"price": 150.0})
    r = C.replay([entrou, saiu])
    # 0,40 * 70000 = 28000 investidos → +50% = 14000 realizados
    assert r["realizado"] == pytest.approx(14000.0)
    assert r["posicoes_abertas"] == {}
    assert r["retorno_pct"] == pytest.approx(14000.0 / C.CLONE_CAPITAL)


# ── a DEFASAGEM viaja em cada operação ──────────────────────────────────────────
def test_defasagem_registrada_por_operacao():
    atual = _atual([{"ticker": "MSFT", "classe": "Acao", "qtd": 22, "precoMedio": 381,
                     "entrada": "jul/2026"}],
                   lido_em=datetime(2026, 9, 2, tzinfo=timezone.utc).timestamp())
    op = C.op_de_mudanca(_mud("MSFT", "entrou", 0.3, qtd_agora=22), atual, {"price": 500.0})
    # entrada dele em jul/2026 (dia 1), detecção em 02/09/2026 → 63 dias de atraso
    assert op["defasagem_base"] == "entrada"
    assert op["defasagem_granularidade"] == "mes"
    assert op["defasagem_dias"] == 63
    assert op["dele_entrada"] == "jul/2026"


def test_defasagem_ajuste_usa_atualizado_do_snapshot():
    atual = _atual([{"ticker": "GOOGL", "classe": "Acao", "qtd": 5, "precoMedio": 327,
                     "entrada": "jul/2026"}],
                   atualizado="27/08/2026",
                   lido_em=datetime(2026, 9, 2, tzinfo=timezone.utc).timestamp())
    op = C.op_de_mudanca(_mud("GOOGL", "aumentou", 0.2, qtd_antes=3, qtd_agora=5),
                         atual, {"price": 300.0})
    assert op["defasagem_base"] == "atualizado"      # ajuste não tem data própria
    assert op["defasagem_granularidade"] == "dia"
    assert op["defasagem_dias"] == 6                 # 27/08 → 02/09


# ── sem preço real: op gravada, mas FORA da conta (não inventa entrada) ──────────
def test_sem_cotacao_real_op_fica_fora_do_pnl():
    atual = _atual([{"ticker": "MSFT", "classe": "Acao", "qtd": 22, "precoMedio": 381,
                     "entrada": "jul/2026"}])
    op = C.op_de_mudanca(_mud("MSFT", "entrou", 0.3, qtd_agora=22), atual, None)
    assert op["incluido"] is False
    assert op["preco"] is None
    assert op["motivo_exclusao"]
    assert C.replay([op])["n_ops_incluidas"] == 0


# ── resumo: os TRÊS estados (DA-157), nunca "0%" travestido de "sem dado" ────────
def test_resumo_sem_mudanca_e_amostra_insuficiente():
    r = C.resumo([])
    assert r["estado"] == "amostra_insuficiente"
    assert "nenhuma mudança" in r["motivo"]


def test_resumo_mudanca_sem_preco_e_amostra_insuficiente():
    atual = _atual([{"ticker": "MSFT", "classe": "Acao", "qtd": 22, "precoMedio": 381,
                     "entrada": "jul/2026"}])
    op = C.op_de_mudanca(_mud("MSFT", "entrou", 0.3, qtd_agora=22), atual, None)
    r = C.resumo([op])
    assert r["estado"] == "amostra_insuficiente"
    assert "sem" in r["motivo"].lower()              # detectou, mas sem cotação


def test_resumo_com_historico_reporta_retorno():
    atual = _atual([{"ticker": "X", "classe": "Acao", "qtd": 1, "precoMedio": 999,
                     "entrada": "jul/2026"}])
    entrou = C.op_de_mudanca(_mud("X", "entrou", 0.4, qtd_agora=1), atual, {"price": 100.0})
    saiu = C.op_de_mudanca(_mud("X", "saiu", 0.0, qtd_antes=1), atual, {"price": 150.0})
    r = C.resumo([entrou, saiu])
    assert r["estado"] == "ok"
    assert r["retorno_pct"] == pytest.approx(14000.0 / C.CLONE_CAPITAL)
    assert r["n_ops_incluidas"] == 2

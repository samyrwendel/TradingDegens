"""Carteira-espelho que CLONA as PRÓXIMAS entradas/saídas do Erick em paper
(tasks 20260902-055/056).

Dois dentes centrais:
  • :func:`test_dente_preco_dele_inverte_o_sinal` — dá sinal TROCADO (lucro↔prejuízo)
    se alguém ligar a conta ao ``precoMedio`` dele em vez do preço REAL da detecção;
  • :func:`test_dente_ativacao_nao_semeia_a_carteira_atual` — quebra se alguém semear
    a carteira com o snapshot atual dele (a carteira NASCE VAZIA, task 056).
"""

from datetime import datetime, timezone

import pytest

from tradingagents.webui import clone_erick as C

pytestmark = pytest.mark.unit

CAP = 70000.0  # capital de teste: é PARÂMETRO agora, sempre passado explícito.


def _atual(ativos, atualizado="27/08/2026", lido_em=None, degradado=False):
    if lido_em is None:
        lido_em = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc).timestamp()
    return {"lido_em": lido_em, "degradado": degradado,
            "carteira": {"atualizado": atualizado, "ativos": ativos, "feed": []}}


def _mud(ticker, tipo, peso_agora, qtd_antes=None, qtd_agora=None, classe="Acao"):
    return {"ticker": ticker, "tipo": tipo, "classe": classe,
            "peso_agora": peso_agora, "qtd_antes": qtd_antes,
            "qtd_agora": qtd_agora, "nome": ticker, "pct_capital": peso_agora}


def _ativo(ticker, qtd, preco_medio, classe="Acao", entrada="jul/2026"):
    return {"ticker": ticker, "classe": classe, "qtd": qtd,
            "precoMedio": preco_medio, "entrada": entrada}


# ── a regra: preço REAL da detecção, nunca o precoMedio dele ─────────────────────
def test_op_usa_preco_real_da_deteccao_nao_o_preco_medio_dele():
    atual = _atual([_ativo("MSFT", 22.1, 381.93)])
    m = _mud("MSFT", "entrou", 0.30, qtd_agora=22.1)
    preco_real = {"price": 520.0, "sessao": "regular", "rotulo": "cotação agora"}
    op = C.op_de_mudanca(m, atual, preco_real)

    assert op["preco"] == 520.0                     # o REAL entrou na conta
    assert op["preco"] != op["dele_precoMedio"]     # NÃO é o preço dele
    assert op["dele_precoMedio"] == 381.93          # e o dele fica só como auditoria
    assert op["incluido"] is True


def test_dente_preco_dele_inverte_o_sinal():
    """DENTE 1: se alguém trocar o preço real pelo `precoMedio` dele, o clone passa
    de LUCRO a PREJUÍZO. BTC — médio dele 62.485, cotação REAL na detecção 50.000,
    preço de agora 55.000: honesto = +10% (não-realizado POSITIVO); com o preço dele
    seria −12% (negativo)."""
    atual = _atual([_ativo("BTC", 0.02, 62485.0, classe="Cripto")])
    m = _mud("BTC", "entrou", 0.20, qtd_agora=0.02, classe="Cripto")
    op = C.op_de_mudanca(m, atual, {"price": 50000.0, "sessao": "24h",
                                    "rotulo": "cotação agora"})
    r = C.replay([op], CAP, precos_atuais={"BTC": 55000.0})

    assert op["preco"] == 50000.0
    assert r["nao_realizado"] > 0                    # honesto: LUCRO
    assert r["nao_realizado"] == pytest.approx(1400.0, rel=1e-6)  # 0.2*70000*0.1


# ── replica o PESO, não a quantidade dele ───────────────────────────────────────
def test_replica_peso_nao_quantidade():
    atual = _atual([_ativo("ASTS", 147.44, 67.82, entrada="ago/2026")])
    m = _mud("ASTS", "entrou", 0.25, qtd_agora=147.44)   # ele tem 147,44 ações
    op = C.op_de_mudanca(m, atual, {"price": 40.0})
    r = C.replay([op], CAP)
    abertos = r["posicoes_abertas"]["ASTS"]
    assert abertos["units"] == pytest.approx(0.25 * CAP / 40.0)  # do PESO
    assert abertos["units"] != pytest.approx(147.44)             # não da qtd dele


# ── capital é PARÂMETRO, sem default inventado (task 056) ────────────────────────
def test_sem_capital_o_clone_fica_armado(tmp_path):
    assert C.estado(dir=tmp_path)["estado"] == "armado"
    assert C.estado(dir=tmp_path)["capital"] is None


def test_configurar_capital_ativa_e_rearma_baseline(tmp_path):
    C.configurar_capital(12345.0, dir=tmp_path)
    est = C.estado(dir=tmp_path)
    assert est["estado"] == "ativo"
    assert est["capital"] == 12345.0
    assert est["baseline_definida"] is False        # (re)armada: história do zero


def test_capital_nao_positivo_e_recusado(tmp_path):
    with pytest.raises(ValueError):
        C.configurar_capital(0, dir=tmp_path)
    with pytest.raises(ValueError):
        C.configurar_capital(-100, dir=tmp_path)


def test_capital_do_env_semeia_quando_estado_nao_tem(tmp_path, monkeypatch):
    monkeypatch.setenv("CLONE_ERICK_CAPITAL", "5000")
    assert C.estado(dir=tmp_path)["estado"] == "ativo"
    assert C.estado(dir=tmp_path)["capital"] == 5000.0


# ── observar: armado não opera; ativação nasce vazia; só o futuro conta ──────────
def test_armado_observar_nao_grava_nada(tmp_path):
    atual = _atual([_ativo("MSFT", 22, 381)])
    r = C.observar(atual, dir=tmp_path, preco_fn=lambda t, c: {"price": 500.0})
    assert r["estado"] == "armado"
    assert r["ops"] == []
    assert C.carrega_ledger(C.ledger_path(tmp_path)) == []
    assert C.estado(dir=tmp_path)["baseline_definida"] is False   # nem baseline


def test_dente_ativacao_nao_semeia_a_carteira_atual(tmp_path):
    """DENTE 2: ligar o clone NÃO abre as posições que ele já tem. A primeira leitura
    pós-ativação é só a baseline — zero operação. Quebra se alguém semear do
    snapshot atual."""
    C.configurar_capital(CAP, dir=tmp_path)
    atual = _atual([_ativo("MSFT", 22, 381), _ativo("BE", 21, 181),
                    _ativo("GOOGL", 30, 327),
                    {"ticker": "CASH", "classe": "Caixa", "qtd": 5000,
                     "precoMedio": 1, "entrada": "-"}])
    r = C.observar(atual, dir=tmp_path, preco_fn=lambda t, c: {"price": 999.0})

    assert r["estado"] == "ativo"
    assert r["ops"] == []                            # NADA replicado do que ele já tem
    assert C.carrega_ledger(C.ledger_path(tmp_path)) == []
    assert C.estado(dir=tmp_path)["baseline_definida"] is True


def test_so_mudancas_apos_ativacao_viram_operacao(tmp_path):
    C.configurar_capital(CAP, dir=tmp_path)
    base = [_ativo("MSFT", 22, 381),
            {"ticker": "CASH", "classe": "Caixa", "qtd": 40000, "precoMedio": 1, "entrada": "-"}]
    C.observar(_atual(base), dir=tmp_path,
               preco_fn=lambda t, c: {"price": 500.0})           # baseline, 0 op

    # ele ENTRA em ASTS (posição nova) — só ISSO, pós-ativação, vira operação
    depois = base[:1] + [_ativo("ASTS", 100, 67.82, entrada="set/2026"),
                         {"ticker": "CASH", "classe": "Caixa", "qtd": 30000, "precoMedio": 1, "entrada": "-"}]
    r = C.observar(_atual(depois), dir=tmp_path,
                   preco_fn=lambda t, c: {"price": 45.0} if t == "ASTS" else {"price": 500.0})
    tickers = [o["ticker"] for o in r["ops"]]
    assert tickers == ["ASTS"]                        # e nada do que já existia
    assert r["ops"][0]["preco"] == 45.0              # preço REAL, não o 67,82 dele
    assert len(C.carrega_ledger(C.ledger_path(tmp_path))) == 1


def test_leitura_degradada_nao_move_o_clone(tmp_path):
    C.configurar_capital(CAP, dir=tmp_path)
    r = C.observar(_atual([_ativo("MSFT", 22, 381)], degradado=True), dir=tmp_path,
                   preco_fn=lambda t, c: {"price": 500.0})
    assert r["ops"] == []
    assert C.estado(dir=tmp_path)["baseline_definida"] is False


# ── cobre entrada / saída / aumento / redução (registrar, baixo nível) ──────────
def test_registrar_cobre_os_quatro_eventos_e_pula_caixa(tmp_path):
    led = tmp_path / "operacoes.jsonl"
    atual = _atual([_ativo("MSFT", 30, 381.93), _ativo("BE", 10, 181.38),
                    _ativo("GOOGL", 5, 327.81),
                    {"ticker": "CASH", "classe": "Caixa", "qtd": 5000, "precoMedio": 1, "entrada": "-"}])
    mudou = [
        _mud("MSFT", "entrou", 0.30, qtd_agora=30),
        _mud("BE", "saiu", 0.0, qtd_antes=10, qtd_agora=None),
        _mud("GOOGL", "aumentou", 0.15, qtd_antes=3, qtd_agora=5),
        _mud("ASTS", "reduziu", 0.05, qtd_antes=200, qtd_agora=147),
        _mud("CASH", "aumentou", 0.10, classe="Caixa"),      # caixa NÃO vira operação
    ]
    precos = {"MSFT": 500.0, "BE": 180.0, "GOOGL": 300.0, "ASTS": 40.0}
    ops = C.registrar(mudou, atual, preco_fn=lambda t, c: {"price": precos.get(t)},
                      path=led)
    tipos = {o["ticker"]: o["tipo"] for o in ops}
    assert tipos == {"MSFT": "entrou", "BE": "saiu",
                     "GOOGL": "aumentou", "ASTS": "reduziu"}
    assert led.exists() and len(led.read_text().splitlines()) == 4
    assert C.carrega_ledger(led) == ops


def test_ciclo_entrou_e_saiu_realiza_com_nossos_precos():
    """entrou a 100, saiu a 150 (NOSSOS preços) → +50% do peso investido, realizado."""
    atual = _atual([_ativo("X", 1, 999)])
    entrou = C.op_de_mudanca(_mud("X", "entrou", 0.40, qtd_agora=1), atual, {"price": 100.0})
    saiu = C.op_de_mudanca(_mud("X", "saiu", 0.0, qtd_antes=1), atual, {"price": 150.0})
    r = C.replay([entrou, saiu], CAP)
    assert r["realizado"] == pytest.approx(14000.0)   # 0,40*70000*50%
    assert r["posicoes_abertas"] == {}
    assert r["retorno_pct"] == pytest.approx(14000.0 / CAP)


# ── DEFASAGEM EM DUAS PERNAS, separadas e nomeadas (task 057) ───────────────────
def test_duas_pernas_de_defasagem_gravadas_separadas():
    det = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc).timestamp()
    ant = datetime(2026, 9, 2, 11, 0, tzinfo=timezone.utc).timestamp()   # leitura anterior
    atual = _atual([_ativo("MSFT", 22, 381)], atualizado="27/08/2026", lido_em=det)
    op = C.op_de_mudanca(_mud("MSFT", "entrou", 0.3, qtd_agora=22), atual,
                         {"price": 500.0}, deteccao_anterior=ant)
    assert op["defasagem_fonte_dias"] == 6            # PERNA 1: 27/08 → 02/09 (dias)
    assert op["defasagem_deteccao_horas"] == 1.0      # PERNA 2: janela de 1h
    assert op["fonte_atualizado"] == "27/08/2026"
    assert op["conflito_carimbo"] is False


def test_perna2_de_1h_nao_esconde_a_perna1_de_dias():
    """Gravar só a perna 2 (nossa detecção, ~1h) faria o clone parecer rápido; a
    perna 1 (a fonte publicando dias depois de operar) tem de aparecer inteira."""
    det = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc).timestamp()
    ant = datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc).timestamp()
    atual = _atual([_ativo("BE", 10, 181)], atualizado="20/08/2026", lido_em=det)
    op = C.op_de_mudanca(_mud("BE", "entrou", 0.2, qtd_agora=10), atual,
                         {"price": 200.0}, deteccao_anterior=ant)
    assert op["defasagem_fonte_dias"] == 21           # 20/08 → 10/09 (a fonte)
    assert op["defasagem_deteccao_horas"] == 1.0      # nós (limite superior)
    assert op["defasagem_fonte_dias"] > op["defasagem_deteccao_horas"] / 24.0


# ── CONFLITO DE CARIMBO: composição muda sem a data andar (task 057) ────────────
def _base_e_cash():
    return [_ativo("MSFT", 22, 381),
            {"ticker": "CASH", "classe": "Caixa", "qtd": 40000, "precoMedio": 1, "entrada": "-"}]


def test_dente_conflito_de_carimbo_detectado_e_registrado(tmp_path):
    """DENTE: ele mexe na carteira mas NÃO move o `atualizado` → a data da fonte é
    suspeita e a operação fica MARCADA; nunca se escolhe em silêncio qual crer."""
    C.configurar_capital(CAP, dir=tmp_path)
    d1 = datetime(2026, 9, 1, 10, tzinfo=timezone.utc).timestamp()
    d2 = datetime(2026, 9, 2, 10, tzinfo=timezone.utc).timestamp()
    C.observar(_atual(_base_e_cash(), atualizado="27/08/2026", lido_em=d1), dir=tmp_path,
               preco_fn=lambda t, c: {"price": 500.0})
    depois = _base_e_cash() + [_ativo("ASTS", 100, 67.82)]
    r = C.observar(_atual(depois, atualizado="27/08/2026", lido_em=d2), dir=tmp_path,   # MESMO carimbo
                   preco_fn=lambda t, c: {"price": 45.0})
    assert r["conflito_carimbo"] is True
    op = next(o for o in r["ops"] if o["ticker"] == "ASTS")
    assert op["conflito_carimbo"] is True
    assert "carimbo" in op["defasagem_fonte_nota"].lower()


def test_carimbo_que_anda_nao_e_conflito(tmp_path):
    C.configurar_capital(CAP, dir=tmp_path)
    d1 = datetime(2026, 9, 1, 10, tzinfo=timezone.utc).timestamp()
    d2 = datetime(2026, 9, 2, 10, tzinfo=timezone.utc).timestamp()
    C.observar(_atual(_base_e_cash(), atualizado="27/08/2026", lido_em=d1), dir=tmp_path,
               preco_fn=lambda t, c: {"price": 500.0})
    depois = _base_e_cash() + [_ativo("ASTS", 100, 67.82)]
    r = C.observar(_atual(depois, atualizado="02/09/2026", lido_em=d2), dir=tmp_path,   # carimbo ANDOU
                   preco_fn=lambda t, c: {"price": 45.0})
    assert r["conflito_carimbo"] is False


# ── PERDA POR DEFASAGEM: quanto custa seguir atrasado (task 057) ────────────────
def test_perda_por_defasagem_mede_compra_pior_que_o_nivel_dele():
    atual = _atual([_ativo("X", 1, 100)])            # ele ~100
    op = C.op_de_mudanca(_mud("X", "entrou", 0.5, qtd_agora=1), atual, {"price": 130.0})
    r = C.replay([op], CAP)                            # nós, atrasados, a 130
    units = 0.5 * CAP / 130.0
    assert r["perda_por_defasagem"] == pytest.approx((130.0 - 100.0) * units)
    assert r["n_ops_defasagem_medida"] == 1


def test_dente_perda_usa_precoMedio_so_pra_MEDIR_nao_pra_precificar():
    """DENTE: a POSIÇÃO é precificada pelo nosso preço REAL (130); o precoMedio dele
    (100) entra SÓ na perda por defasagem. Se alguém precificar pela precoMedio, o
    não-realizado deixa de ser 0 e este teste quebra."""
    atual = _atual([_ativo("X", 1, 100)])
    op = C.op_de_mudanca(_mud("X", "entrou", 0.5, qtd_agora=1), atual, {"price": 130.0})
    r = C.replay([op], CAP, precos_atuais={"X": 130.0})
    assert r["posicoes_abertas"]["X"]["preco_medio_clone"] == pytest.approx(130.0)
    assert r["nao_realizado"] == pytest.approx(0.0)   # marcado a 130 = nosso custo
    assert r["perda_por_defasagem"] > 0               # mas o custo do atraso existe


def test_veredito_defasagem_come_a_vantagem():
    atual = _atual([_ativo("A", 1, 100), _ativo("B", 1, 100), _ativo("D", 1, 100)])
    ops = [C.op_de_mudanca(_mud(t, "entrou", 0.2, qtd_agora=1), atual, {"price": 150.0})
           for t in ("A", "B", "D")]
    precos = {"A": 150.0, "B": 150.0, "D": 150.0}     # marcado a 150 = sem ganho
    r = C.resumo(ops, CAP, precos_atuais=precos)
    assert r["estado"] == "ok"
    assert "come a vantagem" in r["veredito_defasagem"].lower()
    assert r["perda_por_defasagem"] > 0


def test_veredito_defasagem_amostra_insuficiente():
    atual = _atual([_ativo("A", 1, 100)])
    op = C.op_de_mudanca(_mud("A", "entrou", 0.2, qtd_agora=1), atual, {"price": 150.0})
    r = C.resumo([op], CAP, precos_atuais={"A": 150.0})
    assert "amostra insuficiente" in r["veredito_defasagem"]


# ── sem preço real: op gravada, mas FORA da conta (não inventa entrada) ──────────
def test_sem_cotacao_real_op_fica_fora_do_pnl():
    atual = _atual([_ativo("MSFT", 22, 381)])
    op = C.op_de_mudanca(_mud("MSFT", "entrou", 0.3, qtd_agora=22), atual, None)
    assert op["incluido"] is False
    assert op["preco"] is None and op["motivo_exclusao"]
    assert C.replay([op], CAP)["n_ops_incluidas"] == 0


# ── resumo: os estados honestos (DA-157 + ARMADO da task 056) ───────────────────
def test_resumo_armado_nao_e_zero():
    r = C.resumo([], None)                            # capital None → armado
    assert r["estado"] == "armado"
    assert "0%" not in r["motivo"]


def test_resumo_ativo_sem_mudanca_e_amostra_insuficiente():
    r = C.resumo([], CAP)
    assert r["estado"] == "amostra_insuficiente"
    assert "nenhuma mudança" in r["motivo"]


def test_resumo_mudanca_sem_preco_e_amostra_insuficiente():
    atual = _atual([_ativo("MSFT", 22, 381)])
    op = C.op_de_mudanca(_mud("MSFT", "entrou", 0.3, qtd_agora=22), atual, None)
    r = C.resumo([op], CAP)
    assert r["estado"] == "amostra_insuficiente"
    assert "sem" in r["motivo"].lower()


def test_resumo_com_historico_reporta_retorno():
    atual = _atual([_ativo("X", 1, 999)])
    entrou = C.op_de_mudanca(_mud("X", "entrou", 0.4, qtd_agora=1), atual, {"price": 100.0})
    saiu = C.op_de_mudanca(_mud("X", "saiu", 0.0, qtd_antes=1), atual, {"price": 150.0})
    r = C.resumo([entrou, saiu], CAP)
    assert r["estado"] == "ok"
    assert r["retorno_pct"] == pytest.approx(14000.0 / CAP)
    assert r["n_ops_incluidas"] == 2

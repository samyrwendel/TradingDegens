"""O REGISTRO ANTIGO ganha o veredito certo NA LEITURA (DA-130).

O histórico é persistido inteiro e não se reescreve — é o que deixa reabrir a
análise de ontem e ver a tela daquele dia. Só que as runs gravadas ANTES da DA-125
guardam o veredito **invertido**: a run que originou toda esta série continua no
disco com ``invalidado: True`` e sem ``desfecho``.

Sem este módulo, o LINK-USD que o Samyr reclamou continuaria dizendo "INVALIDADO"
para sempre — a correção valeria só para runs futuras, e a tela do caso concreto
que motivou tudo seguiria errada.

A saída não é reescrever o arquivo (quebra o append-only) nem recalcular no front
(seria uma SEGUNDA régua, em JS — como o 1-2-3 e o Storm passaram a discordar):
é derivar na LEITURA, com a régua de sempre.
"""

import hashlib
import json
from pathlib import Path

import pytest

from tradingagents.webui.ciclo_legado import completa_ciclo

_REAL = Path("/home/clawd/.tradingagents/logs/webui/runs/20260830-232525-ca31d7.json")


@pytest.mark.skipif(not _REAL.exists(), reason="a run real não está neste disco")
def test_o_caso_REAL_do_LINK_USD_passa_a_ler_CONCLUIDO_NO_ALVO():
    """Os números do caso: gatilho 11,52 rompido às 13:00, ALVO 11,63 atingido às
    15:00, e a invalidação só às 23:00 — oito horas depois de o trade ter fechado."""
    rec = json.loads(_REAL.read_text(encoding="utf-8"))
    pat = rec["result"]["actionable"]["pattern"]
    assert pat["invalidado"] is True and pat.get("desfecho") is None, (
        "o registro no disco tem de continuar sendo o antigo", pat)

    completa_ciclo(rec["result"])
    pat = rec["result"]["actionable"]["pattern"]
    assert pat["ciclo"] == "concluido_alvo", pat
    assert pat["desfecho"]["tipo"] == "alvo"
    assert pat["desfecho"]["price"] == 11.63
    assert pat["desfecho"]["em"] == "2026-08-30 15:00"
    assert pat["desfecho"]["entrada"] == 11.52
    assert pat["desfecho"]["entrada_em"] == "2026-08-30 13:00"
    # o veredito EFETIVO vira falso, e o FATO da invalidação sobrevive
    assert pat["invalidado"] is False
    assert pat["invalidado_em"] == "2026-08-30 23:00"


@pytest.mark.skipif(not _REAL.exists(), reason="a run real não está neste disco")
def test_o_ARQUIVO_no_disco_nao_e_tocado():
    """DENTE do append-only: derivar na leitura não pode virar reescrever. Uma
    análise não é um número que se conserta — é o que o sistema disse naquele dia."""
    antes = hashlib.sha256(_REAL.read_bytes()).hexdigest()
    completa_ciclo(json.loads(_REAL.read_text(encoding="utf-8"))["result"])
    assert hashlib.sha256(_REAL.read_bytes()).hexdigest() == antes


def _rec(pattern, candles, **over):
    r = {"actionable": {"pattern": pattern, "trigger": pattern.get("trigger"),
                        "invalidation": {"price": 11.34},
                        "target": {"price": 11.63}, "stop": {"price": 11.27}},
         "price_chart": {"candles": candles}}
    r["actionable"].update(over)
    return r


_P3 = {"date": "2026-08-30 09:00", "price": 11.34}
_PAT = {"p1": {}, "p2": {}, "p3": _P3, "direction": "compra", "trigger": 11.52,
        "state": "acionado", "invalidado": True,
        "invalidado_em": "2026-08-30 23:00"}
_VELAS = [
    {"d": "2026-08-30 09:00", "o": 11.3, "h": 11.4, "l": 11.3, "c": 11.34},
    {"d": "2026-08-30 13:00", "o": 11.45, "h": 11.55, "l": 11.45, "c": 11.55},
    {"d": "2026-08-30 15:00", "o": 11.55, "h": 11.65, "l": 11.55, "c": 11.63},
    {"d": "2026-08-30 23:00", "o": 11.44, "h": 11.44, "l": 10.99, "c": 11.0},
]


def test_o_padrao_que_JA_tem_ciclo_nao_e_recalculado():
    """Registro NOVO não passa pela régua de novo: o valor que ele traz é o que o
    plano decidiu com a série inteira, e recalcular sobre as velas RECORTADAS do
    gráfico poderia dar outro."""
    pat = {**_PAT, "ciclo": "vivo", "desfecho": None}
    completa_ciclo(_rec(pat, _VELAS))
    assert pat["ciclo"] == "vivo", "o ciclo gravado foi sobrescrito"


@pytest.mark.parametrize("faltando", ["velas", "gatilho", "p3"])
def test_sem_material_NAO_inventa_desfecho(faltando):
    """DENTE do exagero oposto: um registro que não dá pra conferir tem de sair como
    entrou. Inventar aqui seria fabricar um resultado no track record."""
    pat = {**_PAT}
    if faltando == "gatilho":
        pat["trigger"] = None
    if faltando == "p3":
        pat["p3"] = {}
    completa_ciclo(_rec(pat, [] if faltando == "velas" else _VELAS))
    assert pat.get("ciclo") is None and pat.get("desfecho") is None, pat
    assert pat["invalidado"] is True, "o veredito antigo tem de ficar de pé"


def test_registro_ilegivel_nao_derruba_a_leitura():
    """A tela do histórico não pode morrer por causa de um registro torto."""
    for lixo in (None, "", [], {"result": 1}, {"actionable": "x"},
                 {"actionable": {"pattern": {"p3": {"date": "?"}}},
                  "price_chart": {"candles": [{"d": "nao-e-data"}]}}):
        assert completa_ciclo(lixo) is lixo

"""Task 20260902-040 (seniorbot) — o ponto 3 do Storm é um candle FECHADO.

O defeito medido no ledger (28/08→02/09/2026): o detector aceitava a ÚLTIMA barra da
série — ainda em formação numa leitura ao vivo — como ponto 3. A máxima dela ainda
está sendo escrita, então o gatilho "rompimento da máxima do ponto 3" PERSEGUE o
preço: a cada passada do scan ele está a <0,5% do preço (é a máxima corrente do
dia), vira ``em_gatilho``, e como a chave de dedup do ledger era só o TRIGGER, cada
passada gravou uma linha nova. UM padrão diário do BTC (p2 = 28/08, stop 76.909,35)
virou 12 linhas, 12 × −1R — o "0 de 15 no diário" do estudo Storm × Setup.
"""
from datetime import datetime, timezone

import pandas as pd

from tradingagents.dataflows import price_structure as ps
from tradingagents.webui import scanner


def _df(rows):
    d = pd.DataFrame(rows)
    d["Date"] = pd.to_datetime(d["Date"])
    return d


def _c(dia, o, h, lo, c):
    return {"Date": f"2026-01-{dia:02d}", "Open": o, "High": h, "Low": lo, "Close": c}


# p1 alta (máxima 110), p2 O FUNDO (mínima 90), p3 recupera (104 > 92) e falha (105 < 110)
_FECHADOS = [_c(1, 100, 110, 99, 108), _c(2, 107, 108, 90, 92), _c(3, 93, 105, 92, 104)]


def test_barra_em_formacao_nao_vira_ponto_3_e_o_gatilho_para_de_perseguir_o_preco():
    """Série AO VIVO: p1 e p2 fechados, a barra 3 ainda aberta. Hoje ela vira p3 e o
    gatilho anda com a máxima dela; com ``ultima_em_formacao`` não há p3 fechado."""
    aberta_cedo = _c(3, 93, 100, 92, 99)      # 06:00 — máxima corrente 100
    aberta_tarde = _c(3, 93, 103, 92, 101)    # 10:00 — a máxima subiu pra 103
    # comportamento de hoje (o defeito): o mesmo padrão, dois gatilhos
    assert ps._storm_123(_df(_FECHADOS[:2] + [aberta_cedo])).entradas[1]["trigger"] == 100.0
    assert ps._storm_123(_df(_FECHADOS[:2] + [aberta_tarde])).entradas[1]["trigger"] == 103.0
    # com a última barra declarada em formação: sem ponto 3 fechado, sem padrão
    assert ps._storm_123(_df(_FECHADOS[:2] + [aberta_cedo]), ultima_em_formacao=True) is None
    assert ps._storm_123(_df(_FECHADOS[:2] + [aberta_tarde]), ultima_em_formacao=True) is None


def test_com_p3_fechado_a_barra_aberta_so_pode_romper_o_gatilho():
    """p1, p2, p3 fechados + barra 4 aberta: o p3 fica em 3 e o gatilho é a máxima dele.
    A barra aberta ROMPE o gatilho — muda o ESTADO, nunca o nível."""
    aberta = _c(4, 104, 104.5, 103, 104)
    pat = ps._storm_123(_df(_FECHADOS + [aberta]), ultima_em_formacao=True)
    assert pat is not None and pat.p3["date"] == "2026-01-03"
    assert pat.entradas[1]["entrada"] == "ponto3" and pat.entradas[1]["trigger"] == 105.0
    assert pat.entradas[1]["state"] == "formando"
    rompeu = _c(4, 104, 106, 103, 105.5)
    pat2 = ps._storm_123(_df(_FECHADOS + [rompeu]), ultima_em_formacao=True)
    assert pat2.p3["date"] == "2026-01-03" and pat2.entradas[1]["trigger"] == 105.0
    assert pat2.entradas[1]["state"] == "acionado"


def test_sem_a_flag_nada_muda_para_quem_ja_chama_o_detector():
    """Backtest / chamadas antigas: default ``False`` = comportamento de sempre."""
    pat = ps._storm_123(_df(_FECHADOS))
    assert pat is not None and pat.p3["date"] == "2026-01-03"


def test_build_storm_plan_declara_a_formacao_pela_data(monkeypatch):
    hoje = datetime.now(timezone.utc).date().isoformat()
    serie = _df(_FECHADOS[:2] + [_c(3, 93, 100, 92, 99)])
    monkeypatch.setattr(ps, "_prep", lambda *a, **k: serie.copy())
    assert ps._ultima_barra_em_formacao(hoje) is True
    assert ps._ultima_barra_em_formacao("2026-01-03") is False
    assert ps._ultima_barra_em_formacao("lixo") is True, "data ilegível cai no lado seguro"
    assert ps.build_storm_plan("X", hoje, "1d")["pattern"] is None, "ao vivo: p3 aberto não vale"
    assert ps.build_storm_plan("X", "2026-01-03", "1d")["pattern"] is not None, "passado: fechou"


def test_estrutura_do_gatilho_e_a_mesma_quando_so_o_gatilho_anda():
    """As duas chaves do ledger: ``_chave`` separa LINHAS, ``_estrutura_do_gatilho``
    reconhece o MESMO PADRÃO — o caso real do BTC diário (12 linhas, um stop)."""
    base = {"setup": "storm", "ticker": "BTC-USD", "frame": "1d", "direction": "compra",
            "sl": 76909.35}
    a = {**base, "ts": "2026-08-30T14:34:05+00:00", "trigger": 78940.3}
    b = {**base, "ts": "2026-08-31T21:35:00+00:00", "trigger": 79218.19}
    assert scanner._chave(a) != scanner._chave(b)
    assert scanner._estrutura_do_gatilho(a) == scanner._estrutura_do_gatilho(b)
    assert scanner._estrutura_do_gatilho({**a, "sl": 77056.15}) != scanner._estrutura_do_gatilho(a)
    assert scanner._estrutura_do_gatilho({**a, "direction": "venda"}) != scanner._estrutura_do_gatilho(a)
    velha = {k: v for k, v in a.items() if k != "setup"}
    assert scanner._estrutura_do_gatilho(velha)[0] == "123", "linha sem carimbo é do 1-2-3"
    assert scanner._estrutura_do_gatilho({**a, "sl": None})[-1] is None

"""O STORM no SCAN do portfólio, e o track record que separa os dois setups (task 023).

Três coisas, e a terceira é a restrição do Samyr em letra:

1. **O Storm entra na linha com identidade PRÓPRIA.** Célula própria, estado próprio
   (inclusive o ``vetado``, que só ele tem) e o gatilho da entrada mais próxima —
   nunca somado às células do 1-2-3. Setup diferente com stop, alvo e R:R construídos
   por regras diferentes; misturar os dois num campo produziria um número que não
   descreve nenhum (a lição da task 008).
2. **O ledger distingue de qual setup veio cada gatilho.** Sem ``setup`` no
   ``scans.jsonl`` a taxa de acerto vira mistura de dois métodos. Linha antiga (sem o
   campo) é do 1-2-3: o ledger é append-only e não se reescreve — quem lê é que
   assume o default.
3. **"Sem desfazer o Setup123."** Nada do 1-2-3 muda de valor: os campos da linha, o
   estado e os níveis continuam os mesmos, e o Storm só ACRESCENTA uma chave.
"""

import json

import pytest

from tradingagents.webui import scanner as sc
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.store import HistoryStore


# --------------------------------------------------------------- a linha ------
def _plano_storm(opera=True, estado_gatilho=108.0, **over):
    base = {
        "symbol": "X", "as_of": "2026-08-29", "price": 107.5,
        "eden": {"disponivel": True, "alinhado": opera,
                 "direcao": "compra" if opera else None, "armadilha": not opera,
                 "ema_rapida": 101.2, "ema_lenta": 88.4, "preco": 107.5,
                 "motivo": "MME 8 acima da MME 80 e preço acima das duas" if opera
                           else "ARMADILHA: preço acima da MME 8 mas ABAIXO da MME 80"},
        "pattern": {"p1": {}, "p2": {"low": 90.0}, "p3": {}, "direction": "compra",
                    "amplitude": 20.0, "entradas": []},
        "invalidation": {"price": 90.0}, "stop": {"price": 90.0},
        "leituras": [
            {"entrada": "ponto2", "ordem": "confirmada", "trigger": estado_gatilho,
             "state": "formando", "target": {"price": 128.0},
             "risk_reward": {"rr": 1.11, "note": None}},
            {"entrada": "ponto3", "ordem": "antecipada", "trigger": 105.0,
             "state": "formando", "target": {"price": 125.0},
             "risk_reward": {"rr": 1.33, "note": None}},
        ],
        "qualidade": "perfeita" if opera else "ruim", "opera": opera,
        "veto": None if opera else "sem Éden alinhado — ARMADILHA",
        "motivo": "",
    }
    base.update(over)
    return base


def test_a_celula_do_storm_leva_a_entrada_mais_PROXIMA_do_preco(monkeypatch):
    """São DUAS entradas do mesmo padrão e a célula tem espaço para uma: vai a que
    decide AGORA. A outra não some — viaja inteira em ``leituras`` pro title."""
    monkeypatch.setattr(sc, "build_storm_plan_dict", lambda *a, **k: _plano_storm())
    # preço 107,5: o gatilho do ponto 2 (108,0) está a 0,5%; o do ponto 3 (105,0) a 2,3%
    linha = sc._storm_row("X", "2026-08-29", "1d", 107.5)
    assert linha["entrada"] == "ponto2", linha
    assert linha["trigger"] == 108.0
    assert len(linha["leituras"]) == 2, "a outra leitura continua no payload"
    assert {L["entrada"] for L in linha["leituras"]} == {"ponto2", "ponto3"}


def test_o_VETO_do_eden_vem_antes_do_estado_do_gatilho(monkeypatch):
    """Um padrão que a regra proíbe operar não é "em gatilho": é um trade que não se
    faz. O estado da célula diz isso com palavra própria."""
    monkeypatch.setattr(sc, "build_storm_plan_dict",
                        lambda *a, **k: _plano_storm(opera=False))
    linha = sc._storm_row("X", "2026-08-29", "1d", 108.0)   # em cima do gatilho
    assert linha["estado"] == "vetado", linha
    assert linha["opera"] is False and linha["veto"], linha


def test_em_gatilho_so_quando_o_eden_autoriza_E_o_preco_esta_no_nivel(monkeypatch):
    monkeypatch.setattr(sc, "build_storm_plan_dict", lambda *a, **k: _plano_storm())
    assert sc._storm_row("X", "2026-08-29", "1d", 108.0)["estado"] == "em_gatilho"
    assert sc._storm_row("X", "2026-08-29", "1d", 95.0)["estado"] == "formando"


def test_sem_padrao_storm_a_celula_declara_e_nao_inventa(monkeypatch):
    monkeypatch.setattr(sc, "build_storm_plan_dict",
                        lambda *a, **k: _plano_storm(pattern=None, leituras=[]))
    linha = sc._storm_row("X", "2026-08-29", "1d", 100.0)
    assert linha["estado"] == "sem_setup" and linha["opera"] is False
    assert "trigger" not in linha, "sem padrão não se publica gatilho nenhum"


def test_o_storm_nunca_derruba_o_scan_do_123(monkeypatch):
    def explode(*_a, **_k):
        raise RuntimeError("fonte fora do ar")
    monkeypatch.setattr(sc, "build_storm_plan_dict", explode)
    assert sc._storm_row("X", "2026-08-29", "1d", 100.0) == {"estado": "sem_dado"}


# ------------------------------------------------- não desfazer o Setup123 ----
_CAMPOS_DO_123 = (
    "frame", "estado", "direction", "pattern_state", "trigger", "price", "dist_pct",
    "dist_txt", "invalidacao", "sl", "tp", "tp_faixa", "rr", "rr_note", "rr_entry",
    "rr_basis", "rr_risco", "rr_retorno", "rr_residual",
    # percurso do setup (task 20260830-008): vazios enquanto o padrão não aciona,
    # mas a CHAVE existe sempre — a tela não pode ter que adivinhar se o campo
    # sumiu porque não se aplica ou porque o scan esqueceu.
    "andado_pct", "sobra_pct", "rr_gatilho", "rr_motivo",
    # DESFECHO do trade (DA-125): {tipo: alvo|stop, em, price, entrada_em, ...}
    # quando o gatilho rompeu e o preço chegou a um dos dois. Sem ele a lista diz
    # "encerrado" e não diz se ganhou ou perdeu — a única coisa que importa num
    # trade que terminou. Mesma disciplina das chaves acima: existe sempre, mesmo
    # vazia, pra "não se aplica" não se confundir com "o scan esqueceu".
    "desfecho",
)


def test_a_linha_do_123_nao_perdeu_nem_mudou_nenhum_campo(monkeypatch):
    """A restrição do Samyr, em teste: o Storm ACRESCENTA uma chave e não toca em
    nenhuma das que já existiam."""
    plano = {
        "price": 100.0, "setup_state": "aguardar_rompimento",
        "pattern": {"trigger": 110.0, "state": "formando", "direction": "compra"},
        "invalidation": {"price": 90.0}, "stop": {"price": 88.0},
        "target": {"price": 130.0, "low": None, "high": None},
        "risk_reward": {"rr": 0.9, "note": None, "entry": 110.0,
                        "entry_basis": "gatilho", "risk": 22.0, "reward": 20.0},
    }
    monkeypatch.setattr(sc, "build_actionable_plan_dict", lambda *a, **k: plano)
    monkeypatch.setattr(sc, "build_storm_plan_dict", lambda *a, **k: _plano_storm())
    monkeypatch.setattr(sc, "_live_price", lambda *_a, **_k: None)
    linha = sc._frame_row("X", "2026-08-29", "1d", live_price=100.0)
    for campo in _CAMPOS_DO_123:
        assert campo in linha, f"o campo {campo} do 1-2-3 sumiu da linha"
    assert linha["trigger"] == 110.0 and linha["sl"] == 88.0 and linha["tp"] == 130.0
    assert linha["estado"] == "formando"
    # e o Storm é UMA chave a mais, numa caixa só dele
    assert set(linha) - set(_CAMPOS_DO_123) == {"storm"}, set(linha)
    # padrão FORMANDO: o percurso não se aplica, e o campo diz isso com None em vez
    # de um zero que pareceria "não andou nada" medido
    assert linha["andado_pct"] is None and linha["rr_gatilho"] is None, linha


def test_sem_123_a_linha_ainda_reporta_o_storm(monkeypatch):
    """São setups DIFERENTES: o Storm pode estar formado onde este não está — é
    metade da razão de ele existir no scan."""
    monkeypatch.setattr(sc, "build_actionable_plan_dict",
                        lambda *a, **k: {"price": 100.0, "setup_state": "sem_setup",
                                         "pattern": None})
    monkeypatch.setattr(sc, "build_storm_plan_dict", lambda *a, **k: _plano_storm())
    monkeypatch.setattr(sc, "_live_price", lambda *_a, **_k: None)
    linha = sc._frame_row("X", "2026-08-29", "1d", live_price=100.0)
    assert linha["estado"] == "sem_setup"
    assert (linha.get("storm") or {}).get("estado") == "formando", linha


# ------------------------------------------------------------- track record ---
def test_o_ledger_carimba_de_qual_setup_veio_o_gatilho(tmp_path):
    log = sc.ScanLog(tmp_path / "scans.jsonl")
    log.record({"ticker": "AAA", "frame": "1d", "trigger": 10.0, "sl": 9.0,
                "tp": 12.0, "rr": 2.0, "direction": "compra", "setup": "123"})
    log.record({"ticker": "AAA", "frame": "1d", "trigger": 11.0, "sl": 9.5,
                "tp": 14.0, "rr": 1.5, "direction": "compra", "setup": "storm",
                "entrada": "ponto3"})
    e = log.entries()
    assert [x["setup"] for x in e] == ["123", "storm"], e
    assert e[1]["entrada"] == "ponto3", e
    # e o MESMO ativo/frame com gatilhos diferentes rende DUAS entradas, não uma
    assert len({(x["setup"], x["trigger"]) for x in e}) == 2


def test_linha_antiga_sem_carimbo_e_lida_como_123(tmp_path):
    """O ledger é append-only: a linha gravada antes do campo existir não se
    reescreve — quem lê é que assume o default."""
    p = tmp_path / "scans.jsonl"
    p.write_text(json.dumps({"ts": "2026-08-01T10:00:00+00:00", "ticker": "AAA",
                             "frame": "1d", "trigger": 10.0}) + "\n")
    e = sc.ScanLog(p).entries()[0]
    assert "setup" not in e, "nada foi reescrito no ledger"
    assert sc._setup_da_entrada(e) == "123"
    assert sc._setup_da_entrada({"setup": "storm"}) == "storm"
    assert sc._setup_da_entrada({"setup": "inventado"}) == "123", "valor desconhecido não vira setup"


def test_a_chave_do_fechamento_nao_mudou_de_forma(tmp_path):
    """DENTE: pôr o setup na CHAVE desamarraria de uma vez todos os fechamentos já
    gravados. O que distingue os setups é o campo, não a identidade da entrada."""
    e = {"ts": "2026-08-01T10:00:00+00:00", "ticker": "AAA", "frame": "1d",
         "trigger": 10.0}
    antes = sc._chave(e)
    assert sc._chave({**e, "setup": "storm"}) == antes


@pytest.mark.parametrize("setup,esperado", [("123", 1), ("storm", 1)])
def test_o_track_record_decompoe_por_setup(tmp_path, monkeypatch, setup, esperado):
    """Taxa de acerto de dois métodos somados não descreve nenhum: o painel passa a
    devolver o agregado E a decomposição, cada uma com a sua base."""
    p = tmp_path / "scans.jsonl"
    log = sc.ScanLog(p)
    log.record({"ticker": "AAA", "frame": "1d", "trigger": 10.0, "sl": 9.0,
                "tp": 12.0, "rr": 2.0, "direction": "compra", "setup": "123"})
    log.record({"ticker": "BBB", "frame": "1d", "trigger": 20.0, "sl": 19.0,
                "tp": 24.0, "rr": 4.0, "direction": "compra", "setup": "storm"})
    monkeypatch.setattr(sc, "_live_price", lambda *_a, **_k: None)
    monkeypatch.setattr(sc, "build_price_chart", lambda *_a, **_k: {"candles": []})
    out = sc.scan_verdicts(log, "2026-08-29")
    assert set(out["por_setup"]) == set(sc.SETUPS_DO_LEDGER), out["por_setup"]
    assert out["por_setup"][setup]["n"] == esperado, out["por_setup"]
    # o agregado continua existindo (é a leitura do painel inteiro)
    assert "taxa_acerto" in out and "verdicts" in out
    assert {v["setup"] for v in out["verdicts"]} == {"123", "storm"}


# ─────────── o Storm sobrevive à troca de frame (task 20260830-007) ───────────
@pytest.mark.unit
def test_o_api_chart_devolve_o_storm_quando_o_metodo_e_storm(tmp_path, monkeypatch):
    """DENTE: `/api/chart` montava o plano SEM `storm`, e trocar de timeframe numa
    run do Storm apagava a leitura inteira — card do veto do Éden, as duas entradas
    e as linhas do gráfico. Era a discordância entre os prints A e B do mesmo 4h: um
    veio do render da run (com Storm), o outro do /api/chart (sem).
    """
    from tradingagents.webui import runner as R

    r = AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "openai",
                     "deep_think_llm": "x", "quick_think_llm": "y"},
        store=HistoryStore(tmp_path))
    monkeypatch.setattr(R.AnalysisRunner, "detect_asset_type", lambda self, t: "stock")
    monkeypatch.setattr(R, "fetch_price_chart",
                        lambda *a, **k: {"candles": [{"d": "2026-08-28", "o": 1, "h": 2,
                                                      "l": 0.5, "c": 1.5}] * 5})
    monkeypatch.setattr(R, "fetch_actionable_plan",
                        lambda *a, **k: {"setup_state": "aguardar_rompimento"})
    chamadas = []

    def _storm(ticker, date, timeframe="1d"):
        chamadas.append(timeframe)
        return {"opera": True, "pattern": {"direction": "compra"}, "leituras": []}
    monkeypatch.setattr(R, "fetch_storm_plan", _storm)

    v = r.timeframe_view("AMD", "2026-08-29", "4h", method="storm123")
    assert v["actionable"].get("storm", {}).get("opera") is True, v["actionable"]
    assert chamadas == ["4h"], ("o Storm é lido no frame PEDIDO", chamadas)

    # NUM MÉTODO QUE NÃO É STORM, com a flag da tela no padrão da DA-184 (Storm123
    # desligado), ele NÃO viaja — nem a leitura roda (chamadas fica vazia). A regra
    # da task 033 (Storm sempre ao lado de qualquer método) só vale com a flag ON.
    chamadas.clear()
    v2 = r.timeframe_view("AMD", "2026-08-29", "4h", method="setup123")
    assert (v2["actionable"] or {}).get("storm") is None, v2["actionable"]
    assert chamadas == [], ("flag OFF: o Storm nem é lido pra um método que não é o dele", chamadas)

    # Com a flag LIGADA (o dono religou, DA-184) volta a regra da task 033: E NUM
    # MÉTODO QUE NÃO É STORM ELE TAMBÉM VEM — supersede "a leitura não se cola em
    # quem não a pediu", que produziu o defeito que o Samyr reportou (numa análise
    # Padrão o Storm não estava desligado, estava AUSENTE — sem payload não há
    # camada, e sem camada não há como anunciar nem ligar). Quem decide o que é
    # DESENHADO continua sendo a camada, na tela (DA-088).
    r.estrategias_set("storm", True)
    chamadas.clear()
    v3 = r.timeframe_view("AMD", "2026-08-29", "4h", method="setup123")
    assert (v3["actionable"] or {}).get("storm", {}).get("opera") is True, v3["actionable"]
    assert chamadas == ["4h"], ("no frame pedido, também fora da run do Storm", chamadas)

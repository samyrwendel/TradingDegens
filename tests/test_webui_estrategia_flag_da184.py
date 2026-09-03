"""Flag de estratégia por setup — visibilidade na TELA (DA-184).

Setup123 fica ligado; Storm123 sai da tela por padrão. O MOTOR nunca lê esta
flag: o scan agendado (``AnalysisRunner.scan_agendado``) continua detectando e
gravando ``setup=storm`` no ledger, e o dry-run MT5 mantém strategy_id/magic
270102 — só o SERVIDOR, ao moldar o que devolve ao front, decide o que some.
Owner-gated: leitura pública (``GET /api/config``), edição só do dono
(``POST /api/estrategias``).

Este arquivo cobre a MECÂNICA (store, filtro do relatório, filtro do scan, a
rota HTTP); a prova VISUAL (print do launcher/card/gráfico com a flag OFF e ON,
DA-062) é manual, fora da suíte.
"""

import json
import os
import threading
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

import pytest

import tradingagents.webui.runner as rm
import tradingagents.webui.scanner as sc
from tradingagents.webui.auth import OwnerAuth
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.scanner import ScanLog, scan_verdicts
from tradingagents.webui.server import make_server
from tradingagents.webui.store import EstrategiaStore, HistoryStore

pytestmark = pytest.mark.unit


# ------------------------------------------------------------- EstrategiaStore --
def test_padrao_e_123_ligado_storm_desligado(tmp_path):
    assert EstrategiaStore(tmp_path).get() == {"123": True, "storm": False}


def test_set_persiste_entre_instancias(tmp_path):
    EstrategiaStore(tmp_path).set("storm", True)
    assert EstrategiaStore(tmp_path).get() == {"123": True, "storm": True}


def test_set_setup_desconhecido_rejeita(tmp_path):
    with pytest.raises(ValueError):
        EstrategiaStore(tmp_path).set("stormer2", True)


def test_arquivo_corrompido_cai_no_padrao(tmp_path):
    store = EstrategiaStore(tmp_path)
    store.path.write_text("{isto não é json", encoding="utf-8")
    assert store.get() == {"123": True, "storm": False}


# --------------------------------------------------- scan_verdicts (relatório) --
def _log_com(tmp_path, *entradas):
    path = tmp_path / "scans.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for e in entradas:
            fh.write(json.dumps(e) + "\n")
    return ScanLog(path)


def _c(d, h, low):
    return {"d": d, "o": low, "h": h, "l": low, "c": low}


def _serie(monkeypatch, candles, preco=None):
    monkeypatch.setattr(sc, "build_actionable_plan_dict",
                        lambda t, d, timeframe="1d", method="padrao":
                        {"price": preco, "pattern": None, "setup_state": "ativo"})
    monkeypatch.setattr(sc, "build_price_chart",
                        lambda t, d, bars=260, timeframe="1d", method="padrao":
                        {"candles": candles})
    monkeypatch.setattr(sc, "_live_price", lambda ticker: preco)
    monkeypatch.setattr(sc, "earnings_window_status",
                        lambda symbol, curr_date, window_days, asset_type="stock": {})


def _log_123_e_storm(tmp_path):
    return _log_com(
        tmp_path,
        {"ts": "2026-08-20T12:00:00+00:00", "ticker": "C", "frame": "1h",
         "direction": "compra", "trigger": 100.0, "sl": 95.0, "tp": 110.0,
         "rr": 2.0, "setup": "123"},
        {"ts": "2026-08-20T12:00:01+00:00", "ticker": "C", "frame": "1h",
         "direction": "compra", "trigger": 100.0, "sl": 95.0, "tp": 110.0,
         "rr": 2.0, "setup": "storm"},
    )


def test_visiveis_none_mantem_o_comportamento_de_sempre(tmp_path, monkeypatch):
    """``visiveis=None`` (default) é o comportamento de ANTES da DA-184 — nenhum
    chamador existente que não passa o parâmetro muda de resultado."""
    log = _log_123_e_storm(tmp_path)
    _serie(monkeypatch, [_c("2026-08-20", 100.0, 99.0), _c("2026-08-21", 111.0, 100.0)],
          preco=101.0)
    out = scan_verdicts(log, "2026-08-22")
    assert {v["setup"] for v in out["verdicts"]} == {"123", "storm"}
    assert set(out["por_setup"]) == {"123", "storm"}
    assert out["n_fechados"] == 2


def test_visiveis_filtra_relatorio_mas_metodo_frame_fica_inteiro(tmp_path, monkeypatch):
    log = _log_123_e_storm(tmp_path)
    _serie(monkeypatch, [_c("2026-08-20", 100.0, 99.0), _c("2026-08-21", 111.0, 100.0)],
          preco=101.0)
    out = scan_verdicts(log, "2026-08-22", visiveis=frozenset({"123"}))

    # RELATÓRIO — só o setup visível
    assert {v["setup"] for v in out["verdicts"]} == {"123"}
    assert set(out["por_setup"]) == {"123"}
    assert out["n_fechados"] == 1
    assert out["taxa_acerto"] == 1.0
    assert set(out["paper"]["por_setup"]) == {"123"}
    assert out["paper"]["carteira"]["n_fechadas"] == 1

    # MÉTODO × FRAME (DA-183) — decompõe OS DOIS, ignora a flag de propósito:
    # é a medição que decide se um setup escondido volta a ficar visível.
    mf = out["paper"]["metodo_frame"]
    assert set(mf["desde_marco"]) == {"123", "storm"}


def test_flag_desligada_nao_impede_o_fechamento_de_ser_gravado_no_ledger(tmp_path, monkeypatch):
    """A flag esconde da VITRINE, nunca do MOTOR: o gatilho do Storm continua
    sendo comparado à série e tendo o desfecho APENDADO no ledger mesmo com a
    flag desligando o setup do relatório."""
    log = _log_123_e_storm(tmp_path)
    _serie(monkeypatch, [_c("2026-08-20", 100.0, 99.0), _c("2026-08-21", 111.0, 100.0)],
          preco=101.0)
    scan_verdicts(log, "2026-08-22", visiveis=frozenset({"123"}))  # storm escondido
    fechamentos = log.fechamentos()
    assert len(fechamentos) == 2, "os DOIS gatilhos fecharam, visível ou não"


# ------------------------------------------------ runner: scan pra tela (JSON) --
def _row(estado="em_gatilho", com_storm=True):
    row = {"frame": "1h", "estado": estado, "direction": "compra", "trigger": 100.0,
          "sl": 95.0, "tp": 110.0, "rr": 2.0, "price": 100.0, "rr_entry": 100.0}
    if com_storm:
        row["storm"] = {"estado": "em_gatilho", "opera": True, "trigger": 200.0,
                        "sl": 190.0, "tp": 220.0, "rr": 2.0, "direction": "compra",
                        "eden_rotulo": "Éden de Alta", "eden_ok": True}
    return row


def _resultado_scan(ticker="AAA"):
    row = _row()
    return {"date": "2026-09-03", "frames": ["1h"], "resumo": {},
           "gerado_em": "2026-09-03T12:00:00-04:00",
           "ativos": [{"ticker": ticker, "melhor": row, "frames": [row]}]}


@pytest.fixture
def _stub_scan_watchlist(monkeypatch):
    monkeypatch.setattr(rm, "scan_watchlist", lambda tickers, date, *a, **k: _resultado_scan())


def test_scan_para_tela_default_off_esconde_storm_do_json(tmp_path, _stub_scan_watchlist):
    """Criterio (1)/(6) da task: com a flag no padrão (storm OFF), a chave
    ``storm`` não aparece em NENHUMA linha do JSON servido ao scan."""
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path))
    runner.watchlist_store.set(["AAA"])
    tela = runner.scan_portfolio("2026-09-03")
    for ativo in tela["ativos"]:
        assert "storm" not in (ativo.get("melhor") or {})
        for f in ativo["frames"]:
            assert "storm" not in f
    assert "storm" not in json.dumps(tela)


def test_scan_para_tela_ledger_recebe_storm_mesmo_com_flag_off(tmp_path, _stub_scan_watchlist):
    """Criterio (3): scans.jsonl ganha a linha ``setup=storm`` mesmo com a flag
    OFF — o motor (``_registrar_gatilhos``) roda ANTES do filtro de tela."""
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path))
    runner.watchlist_store.set(["AAA"])
    runner.scan_portfolio("2026-09-03")
    setups = {e.get("setup") for e in runner.scan_log.entries()}
    assert setups == {"123", "storm"}


def test_scan_para_tela_flag_on_devolve_storm_de_volta(tmp_path, _stub_scan_watchlist):
    """Criterio (2): o dono religa e o Storm volta ao JSON — sem precisar de
    nova varredura (o dado cheio já está em disco/memo)."""
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path))
    runner.watchlist_store.set(["AAA"])
    runner.scan_portfolio("2026-09-03")     # popula o snapshot em disco
    runner.estrategias_set("storm", True)
    salvo = runner.scan_ultimo()
    assert any("storm" in f for a in salvo["ativos"] for f in a["frames"])


def test_scan_ultimo_default_off_tambem_esconde(tmp_path, _stub_scan_watchlist):
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path))
    runner.watchlist_store.set(["AAA"])
    runner.scan_portfolio("2026-09-03")
    salvo = runner.scan_ultimo()
    assert "storm" not in json.dumps(salvo)


# ------------------------------------------------------- plano_com_storm -------
def test_plano_com_storm_incluir_storm_false_nao_chama_o_detector(monkeypatch):
    chamado = []
    monkeypatch.setattr(rm, "fetch_actionable_plan",
                        lambda *a, **k: {"setup_state": "aguardar_rompimento"})
    monkeypatch.setattr(rm, "fetch_storm_plan",
                        lambda *a, **k: chamado.append(1) or {"opera": True})
    plano = rm.plano_com_storm("AAPL", "2026-09-03", "1d", "padrao", incluir_storm=False)
    assert "storm" not in plano
    assert chamado == [], "com a flag OFF o detector do Storm nem roda"


def test_plano_com_storm_incluir_storm_true_e_o_padrao(monkeypatch):
    monkeypatch.setattr(rm, "fetch_actionable_plan",
                        lambda *a, **k: {"setup_state": "aguardar_rompimento"})
    monkeypatch.setattr(rm, "fetch_storm_plan", lambda *a, **k: {"opera": True})
    plano = rm.plano_com_storm("AAPL", "2026-09-03", "1d", "padrao")
    assert plano["storm"] == {"opera": True}


# --------------------------------------------- execucao.card / confiabilidade --
def test_confiabilidade_default_mostra_os_dois_setups():
    """DENTE: sem ``setups``, o comportamento é o de sempre — os DOIS setups,
    mesmo sem amostra (o índice nunca some, é o que o docstring promete)."""
    from tradingagents.webui import execucao

    out = execucao.confiabilidade({})
    assert set(out["setups"]) == {"123", "storm"}


def test_confiabilidade_com_setups_filtra_storm_fora_do_indice():
    """Sem isto o índice "confiabilidade por setup" (dentro do card de execução)
    reaparecia com "Storm123 sem amostra" mesmo com a flag OFF — o card É uma
    das superfícies que a DA-184 manda esconder."""
    from tradingagents.webui import execucao

    out = execucao.confiabilidade({"123": {"n_fechados": 30, "taxa_acerto": 0.6},
                                   "storm": {"n_fechados": 10, "taxa_acerto": 0.4}},
                                  setups=("123",))
    assert set(out["setups"]) == {"123"}


def test_execution_card_esconde_confiabilidade_do_storm_com_flag_off(tmp_path, monkeypatch):
    monkeypatch.setattr(rm, "plano_com_storm",
                        lambda *a, **k: {"setup_state": "aguardar_rompimento"})
    monkeypatch.setattr(rm, "scan_verdicts", lambda *a, **k: {
        "por_setup": {"123": {"n_fechados": 30, "taxa_acerto": 0.6},
                     "storm": {"n_fechados": 10, "taxa_acerto": 0.4}}})
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path))
    out = runner.execution_card("AAPL", "2026-09-03", "1d")
    assert set(out["card"]["confiabilidade"]["setups"]) == {"123"}

    runner.estrategias_set("storm", True)
    out2 = runner.execution_card("AAPL", "2026-09-03", "1d")
    assert set(out2["card"]["confiabilidade"]["setups"]) == {"123", "storm"}


# --------------------------------------------------------------- rota HTTP -----
def _client():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def _post(opener, base, path, payload, headers=None):
    hdr = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode(), headers=hdr)
    try:
        with opener.open(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(opener, base, path):
    try:
        with opener.open(base + path, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.fixture
def _server(tmp_path):
    os.environ["TRADINGDEGENS_OWNER_TOKEN"] = "senha-dono-da184"
    auth = OwnerAuth()
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path))
    httpd = make_server("127.0.0.1", 0, runner=runner, auth=auth)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        os.environ.pop("TRADINGDEGENS_OWNER_TOKEN", None)


def test_get_config_expoe_estrategias_leitura_publica(_server):
    op = _client()
    code, cfg = _get(op, _server, "/api/config")
    assert code == 200
    assert cfg["estrategias"] == {"123": True, "storm": False}


def test_post_estrategias_sem_login_e_403(_server):
    op = _client()
    code, body = _post(op, _server, "/api/estrategias", {"setup": "storm", "ativo": True})
    assert code == 403
    assert body["error_code"] == "owner_only"
    # e o arquivo não foi tocado
    _, cfg = _get(op, _server, "/api/config")
    assert cfg["estrategias"]["storm"] is False


def test_post_estrategias_setup_desconhecido_e_400(_server):
    op = _client()
    _post(op, _server, "/api/login", {"password": "senha-dono-da184"})
    code, body = _post(op, _server, "/api/estrategias", {"setup": "stormer2", "ativo": True})
    assert code == 400
    assert "error" in body


def test_post_estrategias_dono_liga_e_o_config_reflete(_server):
    op = _client()
    code, body = _post(op, _server, "/api/login", {"password": "senha-dono-da184"})
    assert code == 200 and body["owner"] is True

    code, body = _post(op, _server, "/api/estrategias", {"setup": "storm", "ativo": True})
    assert code == 200
    assert body["estrategias"]["storm"] is True

    _, cfg = _get(op, _server, "/api/config")
    assert cfg["estrategias"] == {"123": True, "storm": True}

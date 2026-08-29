"""Portão de AUTORIA do Parar/Pausar/Retomar, e a isenção do 1-2-3 por allowlist.

O que quebrou antes (task 007): ``/api/run/<id>/cancel`` e ``/resume`` usavam o
``_gate_or_403``, que aprova QUALQUER ``X-LLM-Key`` não-vazia sem validar. Como o
run_id é público (``/api/runs`` e ``/api/history`` listam), um anônimo com um header
lixo parava a análise alheia — e no ``resume`` re-enfileirava pelo descritor do DONO,
que carrega ``allow_server_key=True``: a run voltava a rodar NA CHAVE DO SERVIDOR.

E a isenção de gate do atalho 1-2-3 era uma DENYLIST de um item (``compare``): flag
nova que ninguém tivesse lembrado de listar passava isenta. Virou allowlist.

Cada teste aqui tem DENTE: com o gate antigo de volta, ele falha (a prova está em
cada docstring — o que o código antigo respondia).
"""

import json
import threading
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

import pytest

from tests.test_webui_runner import _factory
from tradingagents.webui.auth import OwnerAuth
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.server import make_server
from tradingagents.webui.store import HistoryStore

_SENHA = "senha-do-dono"


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """Sem rede em nenhum enriquecimento do worker."""
    import tradingagents.webui.runner as rm
    monkeypatch.setattr(rm, "fetch_price_chart", lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(rm, "fetch_actionable_plan", lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(rm, "fetch_derivatives_report", lambda t, d: "")


def _loop_factory():
    """Motor que roda pra sempre até o cancelamento cooperativo levantar."""
    import uuid as _uuid

    class _Loop:
        def __init__(self, callbacks):
            self.callbacks = callbacks

        def propagate(self, ticker, date, asset_type="stock", timeframe="1d"):
            while True:
                for cb in self.callbacks:
                    cb.on_chain_start({}, {}, run_id=_uuid.uuid4())
                time.sleep(0.02)

    return lambda config, selected, callbacks: _Loop(callbacks)


@pytest.fixture()
def server(tmp_path, monkeypatch):
    """Servidor com login de dono habilitado e motor que não termina sozinho."""
    monkeypatch.setenv("TRADINGDEGENS_OWNER_TOKEN", _SENHA)
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path)},
        store=HistoryStore(tmp_path),
        graph_factory=_loop_factory(),
    )
    httpd = make_server("127.0.0.1", 0, runner=runner, auth=OwnerAuth())
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture()
def free_server(tmp_path, monkeypatch):
    """Servidor com o motor falso rápido — pra exercitar a isenção do 1-2-3."""
    monkeypatch.setenv("TRADINGDEGENS_OWNER_TOKEN", _SENHA)
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path)},
        store=HistoryStore(tmp_path),
        graph_factory=_factory(),
    )
    httpd = make_server("127.0.0.1", 0, runner=runner, auth=OwnerAuth())
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _post(base, path, payload=None, headers=None, opener=None):
    req = urllib.request.Request(
        base + path, data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json", **(headers or {})})
    op = opener or urllib.request.build_opener()
    try:
        with op.open(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(base, path, opener=None):
    op = opener or urllib.request.build_opener()
    try:
        with op.open(base + path, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _dono(base):
    """Opener com o cookie de sessão do dono."""
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    code, _ = _post(base, "/api/login", {"password": _SENHA}, opener=op)
    assert code == 200
    return op


def _wait(pred, timeout=5.0, step=0.03):
    fim = time.time() + timeout
    while time.time() < fim:
        if pred():
            return True
        time.sleep(step)
    return False


# ------------------------------------------------- Parar/Pausar: só de quem é ----
@pytest.mark.parametrize("rota", ["cancel", "resume"])
def test_anonimo_com_header_lixo_nao_passa_do_portao(server, rota):
    """DENTE: com o ``_gate_or_403`` de volta, a chave lixo passava e a resposta era
    404 ('execução desconhecida') — prova de que o portão tinha sido vencido. Agora
    o portão é de AUTORIA: 403 antes de sequer procurar a run."""
    code, body = _post(server, f"/api/run/nao-existe-999/{rota}", {},
                       headers={"X-LLM-Key": "lixo-nao-validado"})
    assert code == 403, f"{rota} deixou passar um header não validado: {body}"
    assert body.get("error_code") in {"not_run_owner", "owner_only"}


@pytest.mark.parametrize("rota", ["cancel", "resume"])
def test_anonimo_sem_header_nenhum_tambem_e_403(server, rota):
    code, _ = _post(server, f"/api/run/nao-existe-999/{rota}", {})
    assert code == 403


def test_terceiro_nao_para_a_run_alheia_mesmo_com_o_run_id_publico(server):
    """O cenário real: o run_id vaza em /api/runs, o atacante tem BYOK próprio e
    mesmo assim NÃO derruba a análise dos outros. DENTE: antes, 200 + run morta."""
    code, started = _post(server, "/api/analyze", {"ticker": "AAPL", "date": "2020-01-02"},
                          headers={"X-LLM-Key": "sk-do-dono-da-run"})
    assert code == 200
    rid = started["run_id"]
    assert _wait(lambda: rid in (_get(server, "/api/health")[1].get("runs") or []))

    # o run_id é público de propósito — é daí que o atacante o tira
    assert rid in json.dumps(_get(server, "/api/runs")[1]), "run_id não é secreto"

    code, body = _post(server, f"/api/run/{rid}/cancel", {},
                       headers={"X-LLM-Key": "sk-de-um-terceiro"})
    assert code == 403 and body.get("error_code") == "not_run_owner"
    # token errado também não abre
    code, _ = _post(server, f"/api/run/{rid}/cancel", {}, headers={"X-Run-Token": "a" * 32})
    assert code == 403
    # e a run continua VIVA (não foi DoS)
    assert rid in (_get(server, "/api/health")[1].get("runs") or [])

    # quem iniciou tem o token e para a SUA run
    code, res = _post(server, f"/api/run/{rid}/cancel", {},
                      headers={"X-Run-Token": started["run_token"]})
    assert code == 200 and res["cancelled"] is True
    assert _wait(lambda: _get(server, f"/api/run/{rid}")[1].get("status") == "cancelled")


def test_analyze_devolve_o_token_de_controle(server):
    """Sem token na resposta o front não teria como parar a própria run."""
    code, started = _post(server, "/api/analyze", {"ticker": "MSFT", "date": "2020-01-02"},
                          headers={"X-LLM-Key": "sk-x"})
    assert code == 200
    assert len(started.get("run_token") or "") == 32
    _post(server, f"/api/run/{started['run_id']}/cancel", {},
          headers={"X-Run-Token": started["run_token"]})


def test_dono_logado_passa_sem_token(server):
    """O dono não precisa de token: a sessão já o identifica. 404 (run inexistente)
    prova que ele PASSOU do portão — 403 provaria o contrário."""
    op = _dono(server)
    for rota in ("cancel", "resume"):
        code, _ = _post(server, f"/api/run/nao-existe-999/{rota}", {}, opener=op)
        assert code == 404, rota


def test_resume_de_terceiro_nao_reenfileira_na_chave_do_servidor(server):
    """O pior caso do resume: ele ignora os overrides da requisição e re-enfileira
    pelo descritor do dono (allow_server_key=True). Um anônimo não chega lá."""
    op = _dono(server)
    code, started = _post(server, "/api/analyze", {"ticker": "AAPL", "date": "2020-01-02"},
                          opener=op)   # run de dono → resumível (chave do servidor)
    rid = started["run_id"]
    assert _wait(lambda: rid in (_get(server, "/api/health")[1].get("runs") or []))
    code, res = _post(server, f"/api/run/{rid}/cancel", {"pause": True}, opener=op)
    assert code == 200 and res["paused"] is True
    assert _wait(lambda: _get(server, f"/api/run/{rid}")[1].get("status") == "cancelled")

    # anônimo com chave própria (ou com o token da run) NÃO retoma nada
    code, _ = _post(server, f"/api/run/{rid}/resume", {}, headers={"X-LLM-Key": "sk-terceiro"})
    assert code == 403
    code, _ = _post(server, f"/api/run/{rid}/resume", {},
                    headers={"X-Run-Token": started["run_token"]})
    assert code == 403, "resume roda na chave do servidor: token de run não basta"
    # o dono retoma
    code, res = _post(server, f"/api/run/{rid}/resume", {}, opener=op)
    assert code == 200 and res["resuming"] is True
    _post(server, f"/api/run/{rid}/cancel", {}, opener=op)


# ------------------------------------------- isenção do 1-2-3: allowlist de corpo ----
def _stub_estrutural(monkeypatch):
    import tradingagents.webui.runner as rm
    monkeypatch.setattr(rm, "fetch_price_chart",
                        lambda t, d, tf="1d", method="padrao": {"candles": [{"c": 1.0}]})
    monkeypatch.setattr(rm, "fetch_actionable_plan",
                        lambda t, d, tf="1d", method="padrao":
                        {"price": 100.0, "pattern": None, "setup_state": "sem_setup"})


def test_flag_nova_no_corpo_tira_a_isencao_do_setup123(free_server, monkeypatch):
    """DENTE do fail-open: com a denylist antiga (só ``compare``), uma flag INVENTADA
    passava isenta — 200 e run criada anonimamente. Com a allowlist, é 403."""
    _stub_estrutural(monkeypatch)
    code, body = _post(free_server, "/api/analyze",
                       {"ticker": "MSFT", "date": "2026-08-28", "method": "setup123",
                        "flag_que_ninguem_listou": True})
    assert code == 403, body
    assert body.get("error_code") == "need_key"


@pytest.mark.parametrize("extra", [
    {"compare": True},
    {"confront_with": "erick"},
    {"deep_dive": True},
    {"meta": True},
])
def test_corpos_que_escalam_a_rota_seguem_barrados(free_server, monkeypatch, extra):
    _stub_estrutural(monkeypatch)
    code, body = _post(free_server, "/api/analyze",
                       {"ticker": "MSFT", "date": "2026-08-28", "method": "setup123", **extra})
    assert code == 403, f"{extra} passou: {body}"


def test_corpo_real_do_front_segue_isento(free_server, monkeypatch):
    """Contra-prova: o 1-2-3 legítimo do navegador manda ``compare: false`` e a config
    BYOK junto (quem usa Ollama nem tem chave). Fechar o buraco não pode fechar isso."""
    _stub_estrutural(monkeypatch)
    code, body = _post(free_server, "/api/analyze",
                       {"ticker": "MSFT", "date": "2026-08-28", "method": "setup123",
                        "compare": False, "timeframe": "1d", "force_fresh": True,
                        "llm_provider": "ollama", "backend_url": "http://127.0.0.1:11434"})
    assert code == 200, body
    assert body.get("run_id")

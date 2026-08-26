"""E2E (Playwright) do REENGATE do progresso: o front não pode "esquecer" uma
análise viva.

Prova o pedido do Samyr (task 006):
  1. um run EM ANDAMENTO persiste no localStorage; ao recarregar a página, o
     progresso REENGATA a partir do estado persistido — mesmo sem a lista de
     histórico ajudar (isola a via nova, não o openLatestRun);
  2. voltar o app pro primeiro plano (visibilitychange→visível) dispara um poll
     IMEDIATO (contorna o throttle de aba de fundo do mobile);
  3. quando o run termina, o estado persistido é LIMPO (nada a reengatar).

Não roda análise nenhuma: intercepta /api/history|run|status com page.route e
serve snapshots controlados (o servidor real ainda entrega os estáticos e o
/api/config). Pulado com skip se o Playwright/Chromium não estiver disponível.
"""

import json
import re
import threading

import pytest

from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.server import make_server
from tradingagents.webui.store import HistoryStore

pytestmark = pytest.mark.integration

sync_playwright = None
try:  # o browser pode não existir num ambiente mínimo → skip limpo
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None


RUN_ID = "R-TEST-REENGATE"


@pytest.fixture
def live_server(tmp_path):
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path),
                                         "llm_provider": "openai",
                                         "deep_think_llm": "gpt-5.5",
                                         "quick_think_llm": "gpt-5.4-mini"},
                            store=HistoryStore(tmp_path))
    httpd = make_server("127.0.0.1", 0, runner=runner)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


def _running_snap(percent=40):
    return {
        "run_id": RUN_ID, "ticker": "AAPL", "date": "2026-08-26",
        "asset_type": "equity", "status": "running", "error": None,
        "verdict_timeframe": "1d",
        "progress": {"percent": percent, "phase": "Analistas",
                     "label": "Coletando dados de mercado…",
                     "plan": [{"label": "Mercado — técnica"},
                              {"label": "Notícias — fluxo"}],
                     "reached": []},
        "cost": {"usd": 0.01}, "elapsed": 12, "result": None,
    }


def _done_snap():
    s = _running_snap(percent=100)
    s["status"] = "done"
    s["progress"]["phase"] = "Concluído"
    s["progress"]["percent"] = 100
    s["finished_at"] = "2026-08-26T11:10:00-04:00"
    s["result"] = {"verdict": "hold",
                   "final_trade_decision": "Manter posição.",
                   "timeframe": "1d"}
    return s


def _install_routes(page, state):
    """Intercepta os endpoints de run; o resto (estáticos, /api/config) segue pro
    servidor real. `state` controla running↔done e conta os polls de /api/status."""
    def handler(route):
        url = route.request.url
        if "/api/status/" in url or "/api/run/" in url:
            if "/api/status/" in url:
                state["polls"] += 1
            snap = _done_snap() if state["status"] == "done" else _running_snap()
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(snap))
        elif url.endswith("/api/history"):
            runs = [] if state.get("history_empty") else [
                _done_snap() if state["status"] == "done" else _running_snap()
            ]
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"runs": runs}))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_persist_and_reengage_on_reload(live_server):
    """Run vivo persiste; reload reengata o progresso mesmo com histórico VAZIO
    (prova a via de persistência, não o openLatestRun)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 800})  # mobile 390
        state = {"status": "running", "polls": 0, "history_empty": False}
        _install_routes(page, state)
        try:
            page.goto(live_server)
            # 1ª carga: openLatestRun abre o run em andamento → watchRun persiste.
            page.wait_for_selector("#progressPanel", state="visible")
            active = page.evaluate("() => localStorage.getItem('td_active_run')")
            assert active and RUN_ID in active, active

            # Agora o histórico "esvazia": se o reengate dependesse do openLatestRun,
            # o progresso NÃO voltaria. Só a persistência pode reengatar.
            state["history_empty"] = True
            page.reload()
            page.wait_for_selector("#progressPanel", state="visible")
            watched = page.evaluate("() => _watchedRunId")
            assert watched == RUN_ID, watched
            # a lista lateral está vazia (prova que o histórico não ajudou)
            still = page.evaluate("() => localStorage.getItem('td_active_run')")
            assert still and RUN_ID in still, still
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_visibilitychange_forces_immediate_poll(live_server):
    """Voltar o app pro primeiro plano dispara um poll IMEDIATO (contorna o throttle
    da aba de fundo — o timer de 2s não é a única fonte de atualização)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 800})
        state = {"status": "running", "polls": 0, "history_empty": False}
        _install_routes(page, state)
        try:
            page.goto(live_server)
            page.wait_for_selector("#progressPanel", state="visible")
            before = state["polls"]
            # dispara o handler de foreground (headless = aba visível → passa o guard);
            # o poll imediato do watchRun tem que bater /api/status na hora, sem esperar
            # o tick de 2s.
            page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
            page.wait_for_timeout(300)
            assert state["polls"] > before, (before, state["polls"])
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_finished_run_clears_persisted_state(live_server):
    """Run que termina limpa o localStorage (nada a reengatar) e troca o progresso
    pelo resultado."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1000, "height": 820})
        state = {"status": "running", "polls": 0, "history_empty": False}
        _install_routes(page, state)
        try:
            page.goto(live_server)
            page.wait_for_selector("#progressPanel", state="visible")
            assert page.evaluate("() => localStorage.getItem('td_active_run')")

            # o run termina no servidor → o próximo poll (2s) traz "done"
            state["status"] = "done"
            page.wait_for_function(
                "() => !localStorage.getItem('td_active_run')", timeout=8000
            )
            page.wait_for_selector("#resultPanel", state="visible")
            page.wait_for_selector("#progressPanel", state="hidden")
        finally:
            browser.close()

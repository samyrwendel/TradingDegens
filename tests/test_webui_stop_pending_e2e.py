"""E2E (Playwright) do botão PARAR — trava 'parando…' com UM clique (task 013).

Bug do Samyr: "tive que clicar 2x em parar e aí sim consegui interromper". O cancel é
COOPERATIVO (encerra no próximo limite de nó/LLM), então a run segue 'running' por ~2s.
O poll seguinte (updateRunControls) REABRIA o botão (if alive: disabled=false) e o
p.label SOBRESCREVIA o 'parando…', então o usuário achava que o 1º clique não pegou.

Aqui provamos que UM clique basta: o botão trava em 'parando…' (disabled + .is-stopping)
e o poll seguinte com a run AINDA running NÃO o reabre nem sobrescreve o rótulo; quando
a run vira 'cancelled' a UI libera. E se o /cancel falha (não-200), reabre e avisa.

Pulado com skip se o Playwright/Chromium não estiver disponível no ambiente.
"""

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


# Monta na tela uma run VIVA (sem backend real) e stuba o /cancel com o status dado.
# Não empurra a run pra 'cancelled' — simula a janela cooperativa em que ela segue running.
_SETUP = """([cancelOk, cancelStatus]) => {
  window.__cancelCalls = 0;
  const realFetch = window.fetch;
  window.fetch = async (url, opts) => {
    if (String(url).includes('/cancel')) {
      window.__cancelCalls++;
      return { ok: cancelOk, status: cancelStatus,
               json: async () => ({ ok: cancelOk, cancelled: cancelOk, paused: false }) };
    }
    return realFetch(url, opts);
  };
  _watchedRunId = 'RID1';
  renderProgress({ status: 'running', run_id: 'RID1', ticker: 'TEST', elapsed: 5, cost: 0.01,
    progress: { phase: 'Debate', label: 'analista de mercado rodando…', percent: 40,
                plan: [], reached: [] } });
}"""

_POLL_STILL_RUNNING = """() => renderProgress({ status: 'running', run_id: 'RID1',
  ticker: 'TEST', elapsed: 7, cost: 0.02,
  progress: { phase: 'Debate', label: 'trader decidindo…', percent: 55,
              plan: [], reached: [] } })"""


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_one_click_locks_stopping_and_poll_does_not_reopen(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(live_server)
            page.evaluate(_SETUP, [True, 200])

            # botão visível e habilitado antes do clique
            page.wait_for_selector("#stopRunBtn:not([disabled])", timeout=4000)
            assert page.is_visible("#stopRunBtn")

            # UM clique → trava em 'parando…'
            page.click("#stopRunBtn")
            page.wait_for_selector("#stopRunBtn.is-stopping[disabled]", timeout=3000)
            assert "parando" in page.inner_text("#stopRunBtn")
            assert "interrompendo" in page.inner_text("#progressLabel")
            assert page.evaluate("() => window.__cancelCalls") == 1

            # POLL seguinte com a run AINDA running: o bug reabria aqui — agora NÃO.
            page.evaluate(_POLL_STILL_RUNNING)
            assert page.is_disabled("#stopRunBtn")
            assert page.query_selector("#stopRunBtn.is-stopping") is not None
            # rótulo do 'parando' preservado — p.label não sobrescreveu
            assert "interrompendo" in page.inner_text("#progressLabel")
            assert "trader decidindo" not in page.inner_text("#progressLabel")
            # e não disparou um 2º /cancel (o clique só registra uma vez)
            assert page.evaluate("() => window.__cancelCalls") == 1

            # run vira 'cancelled' → UI libera e a trava some
            page.evaluate("() => renderResult({ status: 'cancelled', run_id: 'RID1' })")
            page.wait_for_selector("#progressCtl.hidden", state="attached", timeout=3000)
            assert page.evaluate("() => _cancelPending") == ""
            assert page.evaluate(
                "() => document.getElementById('stopRunBtn').classList.contains('is-stopping')"
            ) is False
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_failed_cancel_reopens_button_and_warns(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(live_server)
            page.evaluate(_SETUP, [False, 500])   # /cancel devolve 500

            page.wait_for_selector("#stopRunBtn:not([disabled])", timeout=4000)
            page.click("#stopRunBtn")

            # cancel falhou → destrava e avisa; o usuário PODE tentar de novo
            page.wait_for_selector("#stopRunBtn:not([disabled]):not(.is-stopping)", timeout=3000)
            assert "não consegui parar" in page.inner_text("#formError")
            assert "Parar análise" in page.inner_text("#stopRunBtn")
        finally:
            browser.close()

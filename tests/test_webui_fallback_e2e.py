"""E2E (Playwright) do FALLBACK transparente visível (task 027-fallback).

Uma run de DONO cujo grafo troca de provedor sozinho numa etapa (topo 429 → openai)
CONCLUI — a análise não para — e a UI mostra o desvio: banner de resumo no topo do
resultado + selo na etapa dentro do rodapé de auditoria. Screenshots em
/tmp/devbot-td-fallback. Pulado sem Playwright/Chromium.
"""
import threading

import pytest

import tradingagents.webui.runner as runner_module
from tests.test_webui_fallback_runner import _factory
from tradingagents.webui.auth import OwnerAuth
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.server import make_server
from tradingagents.webui.store import HistoryStore

pytestmark = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None

_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
         "--disable-software-rasterizer", "--disable-features=VizDisplayCompositor"]
_SHOTS = "/tmp/devbot-td-fallback"


def _shot(page, name):
    import os
    try:
        os.makedirs(_SHOTS, exist_ok=True)
        page.screenshot(path=os.path.join(_SHOTS, name))
    except Exception:  # noqa: BLE001
        pass


def _serve(runner, tmp_path, monkeypatch):
    monkeypatch.setattr(runner_module, "fetch_price_chart", lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_actionable_plan", lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_derivatives_report", lambda t, d: "")
    monkeypatch.setenv("TRADINGDEGENS_OWNER_TOKEN", "senha-dono")
    httpd = make_server("127.0.0.1", 0, runner=runner, auth=OwnerAuth())
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{port}"


@pytest.fixture
def live_fallback(tmp_path, monkeypatch):
    """Runner cujo grafo EXERCITA o fallback (topo 429 → openai) e conclui."""
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "claude-cli",
                     "deep_think_llm": "claude-sonnet-5", "quick_think_llm": "claude-haiku-4-5"},
        store=HistoryStore(tmp_path), graph_factory=_factory())
    httpd, base = _serve(runner, tmp_path, monkeypatch)
    try:
        yield base, runner
    finally:
        httpd.shutdown()


def _login_owner(page, base):
    page.goto(base, wait_until="networkidle")
    page.evaluate("""async () => {
      await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'},
        credentials:'same-origin', body: JSON.stringify({password:'senha-dono'})});
      await applyConfig();
    }""")


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_fallback_marker_is_visible(live_fallback):
    base, _runner = live_fallback
    with sync_playwright() as p:
        browser = p.chromium.launch(args=_ARGS)
        page = browser.new_page()
        _login_owner(page, base)

        # Dispara a run de dono; ela vai TROCAR de provedor numa etapa e concluir.
        page.evaluate("""async () => {
          const r = await apiPost('/api/analyze', {ticker:'AAPL', date:'2020-01-02'});
          const j = await r.json();
          watchRun(j.run_id);
          return j.run_id;
        }""")

        # Banner de resumo do fallback aparece no resultado (a análise não parou).
        page.wait_for_selector(".fallback-banner", timeout=15000)
        banner = page.text_content(".fallback-banner .fb-head")
        assert "não parou" in banner
        assert "claude-cli" in page.text_content(".fallback-banner .fb-list")
        assert "openai" in page.text_content(".fallback-banner .fb-list")
        _shot(page, "fallback-banner.png")

        # Selo por-etapa dentro do rodapé de auditoria: abre o detalhe e confere.
        page.click("details.audit-steps > summary")
        page.wait_for_selector(".as-fallback", timeout=5000)
        badge = page.text_content(".as-fallback")
        assert "fallback" in badge and "openai" in badge
        _shot(page, "fallback-step-badge.png")
        browser.close()

"""E2E (Playwright) do cross-provider RÁPIDO/PESADO + escalonamento (task 027).

Parte A (seletor): o dono liga o modo Avançado, escolhe provedor+modelo de CADA
nível (Rápido=openai, Pesado=claude-cli), salva, e a config avançada persiste e
sai no corpo das requisições (llmRequestParts). Parte B (escalonamento): uma run
de dono que FALHA numa etapa mostra o controle "Escalar etapa com outro LLM"; o
clique dispara POST /api/run/<id>/escalate com o nível/provedor/modelo escolhidos.

Screenshots em /tmp/devbot-td-xprov. Pulado sem Playwright/Chromium.
"""
import threading

import pytest

import tradingagents.webui.runner as runner_module
from tests.test_webui_runner import FINAL_STATE, _FakeGraph
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
_SHOTS = "/tmp/devbot-td-xprov"


def _shot(page, name):
    import os
    try:
        os.makedirs(_SHOTS, exist_ok=True)
        page.screenshot(path=os.path.join(_SHOTS, name))
    except Exception:  # noqa: BLE001
        pass


def _base_config(tmp_path):
    return {"results_dir": str(tmp_path), "llm_provider": "openai",
            "deep_think_llm": "gpt-5.5", "quick_think_llm": "gpt-5.4-mini"}


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
def live(tmp_path, monkeypatch):
    runner = AnalysisRunner(base_config=_base_config(tmp_path), store=HistoryStore(tmp_path))
    httpd, base = _serve(runner, tmp_path, monkeypatch)
    try:
        yield base, runner
    finally:
        httpd.shutdown()


@pytest.fixture
def live_boom(tmp_path, monkeypatch):
    """Runner cujo grafo SEMPRE erra numa etapa — pro card de erro + escalonamento."""
    def boom_factory():
        def make(config, selected, callbacks):
            return _FakeGraph(callbacks, FINAL_STATE, "Buy",
                              raise_exc=RuntimeError("etapa falhou de propósito"))
        return make

    runner = AnalysisRunner(base_config=_base_config(tmp_path),
                            store=HistoryStore(tmp_path), graph_factory=boom_factory())
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


# ============================ PARTE A — seletor cross-provider ================
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_advanced_selector_persists_and_emits(live):
    base, _runner = live
    with sync_playwright() as p:
        browser = p.chromium.launch(args=_ARGS)
        page = browser.new_page()
        _login_owner(page, base)
        page.click("#configBtn")
        page.wait_for_selector("#configPanel:not(.hidden)")
        page.evaluate("() => renderConfigPanel()")

        # Liga o modo Avançado → a grade por nível aparece.
        page.check("#cfgAdvanced")
        page.dispatch_event("#cfgAdvanced", "change")
        page.wait_for_selector("#cfgAdvancedGrid:not(.hidden)")

        # Provedores por nível populados; claude-cli (owner-only) visível pro dono.
        deep_opts = page.eval_on_selector_all(
            "#cfgDeepProvider option", "els => els.map(e => e.value)")
        assert "claude-cli" in deep_opts
        assert "openai" in page.eval_on_selector_all(
            "#cfgQuickProvider option", "els => els.map(e => e.value)")

        # Rápido=openai, Pesado=claude-cli + modelos por nível.
        page.select_option("#cfgQuickProvider", "openai")
        page.select_option("#cfgDeepProvider", "claude-cli")
        page.fill("#cfgQuick", "gpt-5.4-mini")
        page.fill("#cfgDeep", "claude-opus-4-8")
        _shot(page, "advanced-selector.png")
        page.click("#cfgSave")

        cfg = page.evaluate("() => _llmCfg")
        assert cfg["advanced"] is True
        assert cfg["quickProvider"] == "openai"
        assert cfg["deepProvider"] == "claude-cli"

        # O corpo das requisições carrega o cross-provider por nível.
        body = page.evaluate("() => llmRequestParts().body")
        assert body["advanced"] is True
        assert body["quick_provider"] == "openai"
        assert body["deep_provider"] == "claude-cli"
        assert body["deep_think_llm"] == "claude-opus-4-8"
        assert body["quick_think_llm"] == "gpt-5.4-mini"
        browser.close()


# ============================ PARTE B — escalonamento de etapa ================
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_failed_run_shows_escalate_and_posts(live_boom):
    base, runner = live_boom
    with sync_playwright() as p:
        browser = p.chromium.launch(args=_ARGS)
        page = browser.new_page()
        _login_owner(page, base)

        # Dispara uma run de dono que vai ERRAR, e acompanha até o card de erro.
        run_id = page.evaluate("""async () => {
          const r = await apiPost('/api/analyze', {ticker:'AAPL', date:'2020-01-02'});
          const j = await r.json();
          watchRun(j.run_id);
          return j.run_id;
        }""")
        page.wait_for_selector(".err-escalate", timeout=15000)

        # O controle de escalonamento aparece (só o dono vê), com os dois níveis.
        levels = page.eval_on_selector_all(
            '.err-escalate [data-esc="level"] option', "els => els.map(e => e.value)")
        assert set(levels) == {"quick", "deep"}
        _shot(page, "escalate-box.png")

        # Escolhe Pesado=claude-cli e escala; captura o POST /escalate.
        page.select_option('.err-escalate [data-esc="level"]', "deep")
        page.select_option('.err-escalate [data-esc="provider"]', "claude-cli")
        page.fill('.err-escalate [data-esc="model"]', "claude-opus-4-8")
        with page.expect_request(f"**/api/run/{run_id}/escalate") as req_info:
            page.click('.err-escalate [data-act="escalate"]')
        req = req_info.value
        assert req.method == "POST"
        import json as _json
        payload = _json.loads(req.post_data)
        assert payload["level"] == "deep"
        assert payload["provider"] == "claude-cli"
        assert payload["model"] == "claude-opus-4-8"
        browser.close()

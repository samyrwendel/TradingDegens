"""E2E (Playwright) do login da assinatura só-dono (task 017).

Prova: público NÃO vê a seção "Conectar assinatura"; o dono logado vê, conecta pela
UI (o token vai por header) e a assinatura fica conectada — sem o token nunca voltar
ao cliente. Pulado sem Playwright/Chromium.
"""

import os
import threading

import pytest

import tradingagents.webui.runner as runner_module
from tradingagents.webui.auth import OwnerAuth
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.server import make_server
from tradingagents.webui.store import HistoryStore
from tradingagents.webui.subscription import SubscriptionStore

pytestmark = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None

_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
         "--disable-software-rasterizer", "--disable-gpu-compositing",
         "--disable-features=VizDisplayCompositor"]


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_module, "fetch_price_chart", lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_actionable_plan", lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_derivatives_report", lambda t, d: "")
    monkeypatch.setenv("TRADINGDEGENS_OWNER_TOKEN", "senha-dono")
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "openai",
                     "deep_think_llm": "gpt-5.5", "quick_think_llm": "gpt-5.4-mini"},
        store=HistoryStore(tmp_path))
    sub = SubscriptionStore(tmp_path / "sub.json")
    httpd = make_server("127.0.0.1", 0, runner=runner, auth=OwnerAuth(), subscription=sub)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}", sub
    finally:
        httpd.shutdown()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_owner_only_subscription_login_ui(live):
    base, sub = live
    with sync_playwright() as p:
        browser = p.chromium.launch(args=_ARGS)
        page = browser.new_page(viewport={"width": 1100, "height": 800})
        page.goto(base, wait_until="networkidle")
        page.click("#configBtn")                       # abre o painel de config

        # PÚBLICO: a seção da assinatura não aparece
        assert page.query_selector("#subscriptionBox").is_visible() is False

        # login como DONO (mesmo fluxo do owner-login), depois re-render do painel
        page.evaluate("""async () => {
          await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'},
            credentials:'same-origin', body: JSON.stringify({password:'senha-dono'})});
          await applyConfig(); renderConfigPanel();
        }""")
        page.wait_for_selector("#subscriptionBox:not(.hidden)")
        assert page.query_selector("#subscriptionBox").is_visible() is True

        # conecta pela UI: digita o token, clica Conectar → header, nunca fica no DOM
        page.fill("#subToken", "sk-UI-SECRET-777")
        page.click("#subConnectBtn")
        page.wait_for_function(
            "() => document.querySelector('#subStatus').textContent.includes('conectada')")
        # a assinatura conectou server-side com o token (mas ele nunca voltou ao cliente)
        assert sub.token() == "sk-UI-SECRET-777"
        assert page.eval_on_selector("#subToken", "el => el.value") == ""   # input limpo
        # o token não sobra em lugar nenhum do DOM
        assert "sk-UI-SECRET-777" not in page.content()
        # estado reflete conectada + botão desconectar aparece
        assert "conectada" in page.eval_on_selector("#subState", "el => el.textContent")
        browser.close()

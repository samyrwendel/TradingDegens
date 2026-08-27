"""E2E (Playwright) do cadastro POR MODELO — dois pares [provedor + modelo] (task 017).

Pedido do Samyr: "Kd a opção de escolher o provedor de CADA modelo? Cadastro de
Provedor não deve estar amarrado aos dois modelos ao mesmo tempo". Antes havia UM
"Provedor" que valia pros dois níveis e o cross-provider ficava escondido atrás do
toggle "Avançado".

Aqui provamos, na tela:
  1. não existe mais provedor único — o painel abre com RÁPIDO e PESADO, cada um com
     o SEU provedor e o SEU modelo, sem toggle nenhum pra destravar;
  2. dá pra montar cross-provider direto (Rápido=assinatura Claude $0, Pesado=OpenAI)
     e isso sai no corpo da requisição por nível;
  3. "= igual ao Rápido" é o atalho pra rodar tudo no mesmo provedor;
  4. o endpoint (Base URL) é POR NÍVEL — só aparece no nível cujo provedor precisa, e
     o endereço de um nível não vaza pro outro;
  5. a nota da chave diz de QUEM ela é quando os dois níveis usam provedores
     diferentes (senão vira "colei minha chave e mesmo assim deu erro de credencial").

Screenshots em /tmp/devbot-td-pairs. Pulado sem Playwright/Chromium.
"""
import threading

import pytest

import tradingagents.webui.runner as runner_module
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
_SHOTS = "/tmp/devbot-td-pairs"


def _shot(page, name):
    import os
    try:
        os.makedirs(_SHOTS, exist_ok=True)
        page.screenshot(path=os.path.join(_SHOTS, name))
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_module, "fetch_price_chart",
                        lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_actionable_plan",
                        lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_derivatives_report", lambda t, d: "")
    monkeypatch.setenv("TRADINGDEGENS_OWNER_TOKEN", "senha-dono")
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "openai",
                     "deep_think_llm": "gpt-5.5", "quick_think_llm": "gpt-5.4-mini"},
        store=HistoryStore(tmp_path))
    httpd = make_server("127.0.0.1", 0, runner=runner, auth=OwnerAuth())
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


def _login_owner(page, base):
    page.goto(base, wait_until="networkidle")
    page.evaluate("""async () => {
      await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'},
        credentials:'same-origin', body: JSON.stringify({password:'senha-dono'})});
      await applyConfig();
    }""")


def _open_config(page):
    page.click("#configBtn")
    page.wait_for_selector("#configPanel:not(.hidden)")


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_nao_existe_mais_provedor_unico_amarrado_aos_dois(live):
    with sync_playwright() as p:
        browser = p.chromium.launch(args=_ARGS)
        page = browser.new_page(viewport={"width": 1180, "height": 940})
        try:
            _login_owner(page, live)
            _open_config(page)
            # o select único morreu; cada nível tem o seu, visível de cara
            assert page.query_selector("#cfgProvider") is None
            assert page.query_selector("#cfgAdvanced") is None
            assert page.is_visible("#cfgQuickProvider")
            assert page.is_visible("#cfgDeepProvider")
            assert page.is_visible("#cfgQuick")
            assert page.is_visible("#cfgDeep")
            # e cada nível já mostra um modelo CONCRETO (não só placeholder)
            assert page.input_value("#cfgQuick")
            assert page.input_value("#cfgDeep")
            _shot(page, "1-dois-pares.png")
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_cross_provider_direto_sai_por_nivel_na_requisicao(live):
    with sync_playwright() as p:
        browser = p.chromium.launch(args=_ARGS)
        page = browser.new_page(viewport={"width": 1180, "height": 940})
        try:
            _login_owner(page, live)
            _open_config(page)
            # Rápido = assinatura Claude ($0/token) · Pesado = OpenAI — escolhido DIRETO
            page.select_option("#cfgQuickProvider", "claude-cli")
            page.select_option("#cfgDeepProvider", "openai")
            assert page.input_value("#cfgQuick").startswith("claude")
            assert page.input_value("#cfgDeep").startswith("gpt")
            _shot(page, "2-cross-provider.png")

            page.click("#cfgSave")
            body = page.evaluate("() => llmRequestParts().body")
            assert body["advanced"] is True
            assert body["quick_provider"] == "claude-cli"
            assert body["deep_provider"] == "openai"
            assert body["quick_think_llm"].startswith("claude")
            assert body["deep_think_llm"].startswith("gpt")
            # provedor-base da requisição = o do PESADO (dono da chave BYOK)
            assert body["llm_provider"] == "openai"
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_botao_igual_ao_rapido_iguala_os_dois_niveis(live):
    with sync_playwright() as p:
        browser = p.chromium.launch(args=_ARGS)
        page = browser.new_page(viewport={"width": 1180, "height": 940})
        try:
            _login_owner(page, live)
            _open_config(page)
            page.select_option("#cfgQuickProvider", "claude-cli")
            page.select_option("#cfgDeepProvider", "openai")
            page.click("#cfgSameAsQuick")
            assert page.input_value("#cfgDeepProvider") == "claude-cli"
            assert page.input_value("#cfgDeep").startswith("claude")
            # o modelo do Pesado é do provedor certo (nunca fica no formato de outro)
            assert "/" not in page.input_value("#cfgDeep")
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_base_url_e_por_nivel(live):
    with sync_playwright() as p:
        browser = p.chromium.launch(args=_ARGS)
        page = browser.new_page(viewport={"width": 1180, "height": 940})
        try:
            _login_owner(page, live)
            _open_config(page)
            # Ollama só no RÁPIDO → só o campo de endpoint DELE aparece
            page.select_option("#cfgQuickProvider", "ollama")
            page.select_option("#cfgDeepProvider", "openai")
            assert page.is_visible("#cfgQuickBaseUrlField")
            assert page.is_hidden("#cfgDeepBaseUrlField")

            page.fill("#cfgQuickBaseUrl", "http://localhost:11434/v1")
            page.click("#cfgSave")
            body = page.evaluate("() => llmRequestParts().body")
            # o endereço do self-host vai só no nível dele — não vaza pro outro client
            assert body["quick_backend_url"] == "http://localhost:11434/v1"
            assert "deep_backend_url" not in body
            _shot(page, "3-endpoint-por-nivel.png")
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_nota_da_chave_diz_de_quem_ela_e(live):
    with sync_playwright() as p:
        browser = p.chromium.launch(args=_ARGS)
        page = browser.new_page(viewport={"width": 1180, "height": 940})
        try:
            _login_owner(page, live)
            _open_config(page)
            # dois provedores PAGOS diferentes: a chave é do Pesado — a nota avisa
            page.select_option("#cfgQuickProvider", "anthropic")
            page.select_option("#cfgDeepProvider", "openai")
            assert page.is_visible("#cfgKeyNote")
            nota = page.text_content("#cfgKeyNote")
            assert "Pesado" in nota and "openai" in nota and "anthropic" in nota
            assert "openai" in page.text_content("#cfgKeyLabel")

            # mesmo provedor nos dois → some a ressalva, a chave vale pros dois
            page.select_option("#cfgQuickProvider", "openai")
            assert page.is_hidden("#cfgKeyNote")
            assert "vale pros dois" in page.text_content("#cfgKeyLabel")
        finally:
            browser.close()

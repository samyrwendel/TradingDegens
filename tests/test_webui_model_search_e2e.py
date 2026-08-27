"""E2E (Playwright) do combobox pesquisável de modelos (BYOK).

Prova o pedido do Samyr: com ~400 modelos do OpenRouter, digitar "glm 5.2" filtra
até `z-ai/glm-5.2` (o prefixo `z-ai/` e a falta de busca o tinham escondido) e
"deepseek flash" até `deepseek/deepseek-v4-flash`. Cobre teclado (↑/↓/Enter) e o
fallback de texto livre (id não listado é aceito). O provider é OpenRouter (catálogo
público → lista sem chave); o fetch é injetado, nada bate na rede.

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


# ~400 modelos sintéticos, com os dois alvos do brief no meio do palheiro.
def _fake_catalog():
    infos = [
        {"id": "z-ai/glm-5.2", "name": "Z.AI: GLM 5.2", "price_in": 0.6, "price_out": 2.2},
        {"id": "deepseek/deepseek-v4-flash", "name": "DeepSeek V4 Flash",
         "price_in": 0.14, "price_out": 0.28},
        {"id": "deepseek/deepseek-v4-flash:free", "name": "DeepSeek V4 Flash (free)",
         "price_in": None, "price_out": None},
    ]
    for i in range(400):
        infos.append({"id": f"acme/filler-model-{i:03d}", "name": f"Filler {i}",
                      "price_in": None, "price_out": None})
    return infos


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    monkeypatch.setattr("tradingagents.webui.server.fetch_provider_model_infos",
                        lambda *a, **k: _fake_catalog())
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


def _open_openrouter_config(page, base):
    """Abre a config, escolhe OpenRouter e espera a lista carregar."""
    page.goto(base)
    page.click("#configBtn")
    page.wait_for_selector("#configPanel:not(.hidden)")
    # Provedor por NÍVEL (task 017): não há mais um "provedor" único — o PESADO é o
    # provedor-base (dono da chave), e é o status dele que a barra mostra.
    page.select_option("#cfgDeepProvider", "openrouter")
    page.select_option("#cfgQuickProvider", "openrouter")
    # o change dispara POST /api/models → status "✅ … modelos carregados"
    page.wait_for_selector("#cfgStatus.ok", timeout=8000)


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_typing_glm_filters_to_zai_glm(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 780})  # mobile 390
        try:
            _open_openrouter_config(page, live_server)

            # "glm 5.2" tem que achar z-ai/glm-5.2 mesmo com prefixo z-ai/ (token match)
            page.fill("#cfgQuick", "glm 5.2")
            opt = page.wait_for_selector('#cfgQuickOpts li[data-val="z-ai/glm-5.2"]',
                                         timeout=4000)
            assert opt.is_visible()
            # o filtro NARROU: filler não casa "glm 5.2"
            assert page.query_selector('#cfgQuickOpts li[data-val^="acme/"]') is None
            # preço aparece (veio do catálogo)
            assert "$" in page.inner_text('#cfgQuickOpts li[data-val="z-ai/glm-5.2"]')

            # clicar seleciona → id completo (com prefixo) vai pro input
            opt.click()
            assert page.input_value("#cfgQuick") == "z-ai/glm-5.2"
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_typing_deepseek_flash_and_keyboard_select(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 780})
        try:
            _open_openrouter_config(page, live_server)

            # campo pesado: "deepseek flash" filtra até os deepseek-v4-flash
            page.fill("#cfgDeep", "deepseek flash")
            page.wait_for_selector('#cfgDeepOpts li[data-val="deepseek/deepseek-v4-flash"]',
                                   timeout=4000)
            # teclado: ↓ destaca a 1ª opção, Enter escolhe
            page.focus("#cfgDeep")
            page.keyboard.press("ArrowDown")
            page.keyboard.press("Enter")
            assert page.input_value("#cfgDeep").startswith("deepseek/deepseek-v4-flash")
            # lista fecha após escolher
            assert page.query_selector("#cfgDeepOpts.hidden") is not None
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_free_text_id_is_accepted(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 780})
        try:
            _open_openrouter_config(page, live_server)

            # id livre não listado ainda funciona (fallback): digita e Esc/Enter mantém
            page.fill("#cfgQuick", "my/custom-model-xyz")
            page.wait_for_selector("#cfgQuickOpts .combo-empty", timeout=4000)
            page.focus("#cfgQuick")
            page.keyboard.press("Enter")
            assert page.input_value("#cfgQuick") == "my/custom-model-xyz"
        finally:
            browser.close()

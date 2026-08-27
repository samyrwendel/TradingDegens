"""E2E (Playwright) da SINCRONIZAÇÃO modelo↔provedor no config (task 014).

Bug do Samyr: no Avançado, pôr Provedor do Rápido/Pesado = Anthropic mas os MODELOS
ficarem gpt-5.4-mini/gpt-5.5 (OpenAI) → a run/Testar caíam no OpenAI sem crédito.

Aqui provamos que escolher um provedor (simples OU por-nível) JÁ reflete os modelos
DAQUELE provedor — o campo vira o default do provedor e o dropdown lista os modelos
dele, nunca de outro (o mismatch morre). Usa provedores públicos (openai/anthropic);
claude-cli (assinatura, owner-only) é coberto nos testes de backend.

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


def _open_config(page, base):
    page.goto(base)
    page.click("#configBtn")
    page.wait_for_selector("#configPanel:not(.hidden)")


# ids dos modelos que o combo de um nível está oferecendo agora.
_COMBO_IDS = "(id) => (_modelCombos[id] && _modelCombos[id].items.map(m => m.id)) || []"


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_simple_provider_switch_syncs_models(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            _open_config(page, live_server)
            # começa em OpenAI: o dropdown lista gpt (o campo vazio mostra o default no
            # placeholder — sem chave/owner não há preseleção, mas o catálogo já é gpt)
            quick_ids = page.evaluate(_COMBO_IDS, "cfgQuick")
            assert quick_ids and all(i.startswith("gpt") for i in quick_ids)

            # troca pra Anthropic → modelos viram Claude, SEM sobrar gpt (mismatch morto)
            page.select_option("#cfgProvider", "anthropic")
            assert page.input_value("#cfgQuick").startswith("claude")
            assert page.input_value("#cfgDeep").startswith("claude")
            quick_ids = page.evaluate(_COMBO_IDS, "cfgQuick")
            deep_ids = page.evaluate(_COMBO_IDS, "cfgDeep")
            assert quick_ids and all(i.startswith("claude") for i in quick_ids)
            assert deep_ids and all(i.startswith("claude") for i in deep_ids)
            assert not any(i.startswith("gpt") for i in quick_ids + deep_ids)
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_advanced_cross_provider_syncs_each_level(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            _open_config(page, live_server)
            # liga o Avançado (cross-provider por nível)
            page.check("#cfgAdvanced")
            page.wait_for_selector("#cfgAdvancedGrid:not(.hidden)")

            # Provedor do RÁPIDO = Anthropic → só o Rápido vira Claude
            page.select_option("#cfgQuickProvider", "anthropic")
            assert page.input_value("#cfgQuick").startswith("claude")
            quick_ids = page.evaluate(_COMBO_IDS, "cfgQuick")
            assert quick_ids and all(i.startswith("claude") for i in quick_ids)

            # o PESADO continua OpenAI (default): modelo e dropdown seguem gpt
            assert page.input_value("#cfgDeep").startswith("gpt")
            deep_ids = page.evaluate(_COMBO_IDS, "cfgDeep")
            assert deep_ids and all(i.startswith("gpt") for i in deep_ids)

            # agora Provedor do PESADO = Anthropic → o Pesado também vira Claude
            page.select_option("#cfgDeepProvider", "anthropic")
            assert page.input_value("#cfgDeep").startswith("claude")
            deep_ids = page.evaluate(_COMBO_IDS, "cfgDeep")
            assert deep_ids and all(i.startswith("claude") for i in deep_ids)
        finally:
            browser.close()

"""E2E (Playwright) do CAP da lista de modelos — prova que nenhuma família some.

Regressão do bug confirmado: OpenRouter tem 418 modelos; ``_rank`` empurrava a
família ``z-ai/`` (fora dos _CHAT_HINTS) pro fim e o corte antigo em 400 a sumia
inteira — ``z-ai/glm-5.2`` nunca chegava ao combobox. Diferente do e2e de busca
(que injeta o catálogo já parseado), aqui o mock é na REDE (urlopen): o parser +
cap REAIS rodam server-side sobre 418 modelos crus, e a UI ainda tem que carregar
os 418 e filtrar até z-ai/glm-5.2 ao digitar "glm 5.2".

Pulado com skip se o Playwright/Chromium não estiver disponível no ambiente.
"""

import io
import json
import threading
import urllib.request

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


# --- 418 modelos crus do OpenRouter, com z-ai/glm-5.2 fadado ao fim do ranking ---
def _fake_openrouter_payload():
    data = [
        {"id": "openai/gpt-5.5", "name": "OpenAI: GPT-5.5"},              # rank 0 (chat)
        {"id": "anthropic/claude-sonnet-5", "name": "Claude Sonnet 5"},  # rank 0 (chat)
        {"id": "z-ai/glm-5.2", "name": "Z.AI: GLM 5.2",                  # rank 1, sorta por ÚLTIMO
         "pricing": {"prompt": "0.0000006", "completion": "0.0000022"}},
    ]
    for i in range(415):  # fillers sem chat-hint (sortam antes de z-ai) → 418 total
        data.append({"id": f"acme/filler-{i:03d}", "name": f"Filler {i}"})
    return {"data": data}


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    # Mock só a REDE: o parser + cap reais de models_list rodam sobre os 418 crus.
    def _fake_urlopen(req, timeout=None):
        return _FakeResp(json.dumps(_fake_openrouter_payload()).encode())

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path),
                                         "llm_provider": "openai",
                                         "deep_think_llm": "gpt-5.5",
                                         "quick_think_llm": "gpt-5.4-mini"},
                            store=HistoryStore(tmp_path))
    httpd = make_server("127.0.0.1", 0, runner=runner)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_zai_glm_survives_the_cap_and_is_filterable(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 780})  # mobile 390
        try:
            page.goto(live_server)
            page.click("#configBtn")
            page.wait_for_selector("#configPanel:not(.hidden)")
            # OpenRouter = catálogo público (lista sem chave) → dispara /api/models,
            # que roda o parser+cap REAIS sobre os 418 crus.
            page.select_option("#cfgProvider", "openrouter")
            page.wait_for_selector("#cfgStatus.ok", timeout=8000)
            # os 418 carregaram (nada foi cortado em 400)
            assert "418" in page.inner_text("#cfgStatus")

            # digitar "glm 5.2" filtra a lista COMPLETA até z-ai/glm-5.2 (o id que o
            # corte antigo escondia) — casa por token em id+nome, apesar do prefixo z-ai/
            page.fill("#cfgQuick", "glm 5.2")
            opt = page.wait_for_selector('#cfgQuickOpts li[data-val="z-ai/glm-5.2"]',
                                         timeout=4000)
            assert opt.is_visible()
            assert "$" in page.inner_text('#cfgQuickOpts li[data-val="z-ai/glm-5.2"]')
            # selecionar leva o id completo pro campo
            opt.click()
            assert page.input_value("#cfgQuick") == "z-ai/glm-5.2"
        finally:
            browser.close()

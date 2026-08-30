"""E2E (Playwright) do botão "Testar modelo" (BYOK).

Prova o pedido do Samyr ("tem um teste de modelo?"): clicar em "Testar modelo"
pinga o modelo RÁPIDO e o PESADO escolhidos com um prompt trivial e mostra a
latência de CADA um (ou a mensagem humana de erro), sem rodar a análise de 12min. O
``create_llm_client`` é injetado (nada bate na rede): um modelo responde rápido,
o outro devagar (latências diferentes), e um modelo ruim vira erro humano — a
chave nunca aparece na tela.

Pulado com skip se o Playwright/Chromium não estiver disponível no ambiente.
"""

import re
import threading
import time

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


# --- motor de LLM falso: despacha pelo NOME do modelo (rápido/lento/ruim) --------
class _Reply:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    def __init__(self, sample="ok", sleep=0.0, raise_exc=None):
        self._sample, self._sleep, self._raise = sample, sleep, raise_exc

    def invoke(self, _msg):
        if self._raise:
            raise self._raise
        if self._sleep:
            time.sleep(self._sleep)
        return _Reply(self._sample)


class _FakeClient:
    def __init__(self, llm):
        self._llm = llm

    def get_llm(self):
        return self._llm


def _fake_create(provider, model, base_url=None, **kwargs):
    if model == "slow-deep":
        return _FakeClient(_FakeLLM(sample="ok", sleep=0.08))   # ~80ms, latência real
    if model == "bad-deep":
        return _FakeClient(_FakeLLM(raise_exc=RuntimeError("401 invalid api key")))
    return _FakeClient(_FakeLLM(sample="ok"))                   # rápido, quase 0ms


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    # sem rede: o client é o falso e a listagem de modelos (disparada ao digitar a
    # chave) devolve vazio em vez de bater no provider.
    monkeypatch.setattr("tradingagents.llm_clients.create_llm_client", _fake_create)
    monkeypatch.setattr("tradingagents.webui.server.fetch_provider_model_infos",
                        lambda *a, **k: [])
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


def _open_config_with_key(page, base, quick, deep):
    """Abre a config, põe OpenAI nos DOIS níveis (task 017), cola a chave e os modelos."""
    page.goto(base)
    page.click("#configBtn")
    page.wait_for_selector("#configPanel:not(.hidden)")
    page.select_option("#cfgQuickProvider", "openai")
    page.select_option("#cfgDeepProvider", "openai")
    page.fill("#cfgKey", "sk-e2e-secret")
    page.fill("#cfgQuick", quick)
    page.fill("#cfgDeep", deep)
    # fecha o combobox aberto pelo fill antes de clicar no botão
    page.keyboard.press("Escape")


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_test_model_shows_latency_for_both(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 780})  # mobile 390
        try:
            _open_config_with_key(page, live_server, "fast-quick", "slow-deep")
            page.click("#cfgTestModel")
            # espera as DUAS linhas de sucesso (rápido e pesado)
            page.wait_for_selector("#cfgModelTest .mt-row.ok", timeout=8000)
            rows = page.query_selector_all("#cfgModelTest .mt-row")
            assert len(rows) == 2
            box = page.inner_text("#cfgModelTest")
            # os dois modelos escolhidos foram pingados e nomeados
            assert "fast-quick" in box and "slow-deep" in box
            # cada um mostra uma latência real (ms/s), e o PASSOU/FALHOU é a CLASSE
            # da linha (.mt-row.ok / .mt-row.err) desde que o pictograma saiu (DA-076)
            assert page.locator(".model-test .mt-row.ok").count() == 2, box
            assert page.locator(".model-test .mt-row.err").count() == 0, box
            assert re.search(r"\d+\s*(ms|s)\b", box)
            # a chave NUNCA aparece na tela
            assert "sk-e2e-secret" not in page.content()
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_test_model_bad_model_shows_human_error(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 780})
        try:
            _open_config_with_key(page, live_server, "good-quick", "bad-deep")
            page.click("#cfgTestModel")
            # o pesado (bad-deep) falha → linha de erro humano
            page.wait_for_selector("#cfgModelTest .mt-row.err", timeout=8000)
            box = page.inner_text("#cfgModelTest")
            assert "good-quick" in box and "bad-deep" in box
            # 401 vira a frase acionável (mapa da 041), sem stack, sem "401" cru
            assert "Configurações" in box
            assert "401" not in box
            # o rápido ainda respondeu (linha .ok); a chave não vaza
            assert page.locator(".model-test .mt-row.ok").count() == 1, box
            assert page.locator(".model-test .mt-row.err").count() == 1, box
            assert "sk-e2e-secret" not in page.content()
        finally:
            browser.close()

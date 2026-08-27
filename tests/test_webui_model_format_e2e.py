"""E2E (Playwright) do FORMATO do modelo por provedor + modelo POR NÍVEL (task 016).

Bug do Samyr: com OpenRouter salvo, ele trocou o provedor pra "Claude — assinatura
(claude-cli)" e os campos de MODELO ficaram no formato OpenRouter
("anthropic/claude-opus-5"). A API Anthropic não entende o prefixo ``anthropic/`` →
404 ``AnthropicModelNotFoundError`` no meio da run do CRWD.

Aqui provamos, na tela:
  1. config salva com id do provedor ANTERIOR é normalizada ao abrir (e persistida);
  2. o corpo das requisições (analyze/Testar) sai com o id PURO — sem 404;
  3. no Avançado, cada nível tem o SEU modelo AO LADO do SEU provedor, listando os
     modelos daquele provedor (não um campo compartilhado no formato de outro).

Screenshots em /tmp/devbot-td-mfmt. Pulado sem Playwright/Chromium.
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
_SHOTS = "/tmp/devbot-td-mfmt"


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


# Config do navegador ANTES do boot: é exatamente o estado que causou o 404 —
# provedor já é a assinatura, modelos ainda no formato OpenRouter.
_BROKEN_CFG = ('{"provider":"claude-cli",'
               '"deepModel":"anthropic/claude-opus-5",'
               '"quickModel":"anthropic/claude-haiku-4.5"}')


def _login_owner(page, base):
    page.goto(base, wait_until="networkidle")
    page.evaluate("""async () => {
      await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'},
        credentials:'same-origin', body: JSON.stringify({password:'senha-dono'})});
      await applyConfig();
    }""")


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_config_salva_no_formato_antigo_e_normalizada_ao_abrir(live):
    with sync_playwright() as p:
        browser = p.chromium.launch(args=_ARGS)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(live, wait_until="networkidle")
            page.evaluate(f"() => localStorage.setItem('td_llm_cfg', '{_BROKEN_CFG}')")
            _login_owner(page, live)
            page.reload(wait_until="networkidle")   # dono abre a página com a config velha
            page.click("#configBtn")
            page.wait_for_selector("#configPanel:not(.hidden)")

            # o prefixo do OpenRouter morreu nos DOIS campos
            assert page.input_value("#cfgDeep") == "claude-opus-5"
            assert page.input_value("#cfgQuick") == "claude-haiku-4-5"
            _shot(page, "normalizado.png")

            # e a config PERSISTIDA também (senão o chip e a próxima run voltavam ao id velho)
            cfg = page.evaluate("() => _llmCfg")
            assert cfg["deepModel"] == "claude-opus-5"
            assert cfg["quickModel"] == "claude-haiku-4-5"

            # o corpo da requisição de análise sai com o id PURO — sem 404
            body = page.evaluate("() => llmRequestParts().body")
            assert body["deep_think_llm"] == "claude-opus-5"
            assert body["quick_think_llm"] == "claude-haiku-4-5"
            assert "/" not in body["deep_think_llm"]
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_avancado_tem_modelo_por_nivel_ao_lado_do_provedor(live):
    with sync_playwright() as p:
        browser = p.chromium.launch(args=_ARGS)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            _login_owner(page, live)
            page.click("#configBtn")
            page.wait_for_selector("#configPanel:not(.hidden)")
            page.check("#cfgAdvanced")
            page.dispatch_event("#cfgAdvanced", "change")
            page.wait_for_selector("#cfgAdvancedGrid:not(.hidden)")

            # os campos de modelo MIGRARAM pra grade do avançado, cada um depois do
            # provedor do seu nível (Provedor Rápido · Modelo Rápido · Provedor Pesado · Modelo Pesado)
            ordem = page.eval_on_selector_all(
                "#cfgAdvancedGrid > .field", "els => els.map(e => e.id)")
            assert ordem == ["cfgQuickProviderField", "cfgQuickField",
                             "cfgDeepProviderField", "cfgDeepField"]

            # Pesado = assinatura Claude → o modelo do PESADO vira Claude e o dropdown
            # daquele nível lista modelos Claude; o Rápido segue no OpenAI (gpt).
            page.select_option("#cfgDeepProvider", "claude-cli")
            assert page.input_value("#cfgDeep").startswith("claude")
            assert page.input_value("#cfgQuick").startswith("gpt")
            deep_ids = page.evaluate(
                "() => _modelCombos.cfgDeep.items.map(m => m.id)")
            quick_ids = page.evaluate(
                "() => _modelCombos.cfgQuick.items.map(m => m.id)")
            assert deep_ids and all(i.startswith("claude") for i in deep_ids)
            assert quick_ids and all(i.startswith("gpt") for i in quick_ids)
            # nenhum id do catálogo da assinatura vem no formato OpenRouter
            assert not any("/" in i for i in deep_ids)

            # o rótulo diz de quem é o modelo e em que provedor ele roda
            assert "Pesado" in page.text_content("#cfgDeepLabel")
            assert "claude-cli" in page.text_content("#cfgDeepLabel")
            _shot(page, "avancado-modelo-por-nivel.png")

            # um id colado no formato OpenRouter no campo do nível é corrigido ao salvar
            page.fill("#cfgDeep", "anthropic/claude-opus-5")
            page.click("#cfgSave")
            cfg = page.evaluate("() => _llmCfg")
            assert cfg["deepProvider"] == "claude-cli"
            assert cfg["deepModel"] == "claude-opus-5"
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_deslogado_nao_apaga_o_modelo_de_provedor_owner_only(live):
    """Provedor salvo é owner-only e a sessão NÃO é dona: o select cai no provedor
    visível (openai), mas a tela não representa a escolha do usuário — normalizar ali
    trocaria os modelos Claude dele por gpt e PERSISTIRIA a perda. Não pode tocar."""
    with sync_playwright() as p:
        browser = p.chromium.launch(args=_ARGS)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(live, wait_until="networkidle")
            page.evaluate(f"() => localStorage.setItem('td_llm_cfg', '{_BROKEN_CFG}')")
            page.reload(wait_until="networkidle")          # sem login: público
            assert page.evaluate("() => _isOwner") is False
            cfg = page.evaluate("() => _llmCfg")
            assert cfg["deepModel"] == "anthropic/claude-opus-5"   # intacto
            assert cfg["provider"] == "claude-cli"

            # ao logar, aí sim a config vira o formato certo da assinatura
            page.evaluate("""async () => {
              await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'},
                credentials:'same-origin', body: JSON.stringify({password:'senha-dono'})});
              await applyConfig();
            }""")
            cfg = page.evaluate("() => _llmCfg")
            assert cfg["deepModel"] == "claude-opus-5"
        finally:
            browser.close()

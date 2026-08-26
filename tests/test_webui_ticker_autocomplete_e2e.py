"""E2E (Playwright) do autocomplete do campo ATIVO (task 020-021).

BUG: ao ESCOLHER uma sugestão do <datalist> (clique OU Enter), o navegador seta o
valor e dispara 'input' — que reagendava a busca e REABRIA o popup, forçando o Samyr
a selecionar de novo. FIX: a seleção fecha o dropdown e NÃO reabre; digitar de novo
reabre normalmente.

O popup do <datalist> é UI nativa do navegador (não é DOM) e não dá pra clicar por
seletor; então a SELEÇÃO é exercitada pelo evento que o clique E o Enter emitem de
fato — 'input' com inputType 'insertReplacementText' — além do fallback por
igualdade-exata (navegadores sem inputType). /api/search é interceptado (sem rede).
"""

import json
import threading

import pytest

from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.server import make_server
from tradingagents.webui.store import HistoryStore

pytestmark = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None

_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]

# resultados canônicos do /api/search pro termo "AVGO" (o mesmo do repro do Samyr)
_SEARCH = {
    "query": "AVGO",
    "results": [
        {"symbol": "AVGO", "name": "Broadcom Inc."},
        {"symbol": "AVS", "name": "Direxion Daily AVGO Bear 1X ETF"},
        {"symbol": "AVGO.TO", "name": "BROADCOM CDR (CAD HEDGED)"},
        {"symbol": "AVGL.TA", "name": "AVGOL INDUSTRIES"},
    ],
}


@pytest.fixture
def base(tmp_path):
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "openai",
                     "deep_think_llm": "x", "quick_think_llm": "y"},
        store=HistoryStore(tmp_path))
    httpd = make_server("127.0.0.1", 0, runner=runner)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


def _page(browser, base):
    page = browser.new_page(viewport={"width": 1200, "height": 800})
    # intercepta a busca → suggestions canônicas, sem rede
    page.route("**/api/search**", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(_SEARCH)))
    page.goto(base, wait_until="networkidle")
    return page


def _opts(page):
    return page.eval_on_selector("#tickerSuggest", "el => el.options.length")


def _type_until_suggestions(page, term):
    """Digita ``term`` e espera o datalist popular (a busca reabre normalmente)."""
    page.fill("#ticker", "")
    page.fill("#ticker", term)
    page.wait_for_function("() => document.querySelector('#tickerSuggest').options.length > 0")


def _dispatch_pick(page, value):
    """Reproduz a SELEÇÃO do datalist: o navegador seta o valor e dispara 'input' com
    inputType 'insertReplacementText' (idêntico no clique e no Enter)."""
    page.eval_on_selector("#ticker", """(el, v) => {
      el.value = v;
      el.dispatchEvent(new InputEvent('input', {inputType: 'insertReplacementText', bubbles: true}));
    }""", value)


def _stays_closed(page):
    """Passa do debounce (220ms) e confirma que o popup NÃO reabriu (datalist vazio)."""
    page.wait_for_timeout(400)
    assert _opts(page) == 0


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_pick_via_selection_event_closes_and_stays_closed(base):
    """Caminho do CLIQUE/ENTER (o evento que ambos emitem): fecha e não reabre."""
    with sync_playwright() as p:
        browser = p.chromium.launch(args=_ARGS)
        page = _page(browser, base)
        _type_until_suggestions(page, "AVGO")
        assert _opts(page) > 0                       # popup aberto (sugestões)
        _dispatch_pick(page, "AVGO")                 # escolhe "AVGO — Broadcom Inc."
        _stays_closed(page)                          # FECHA e fica fechado
        assert page.input_value("#ticker") == "AVGO"  # campo pronto pra Analisar
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_pick_via_exact_match_fallback_closes(base):
    """Fallback (navegador sem inputType): valor == símbolo sugerido fecha o popup."""
    with sync_playwright() as p:
        browser = p.chromium.launch(args=_ARGS)
        page = _page(browser, base)
        _type_until_suggestions(page, "AVGO")
        # 'input' comum (sem inputType), valor batendo EXATO num símbolo sugerido
        page.eval_on_selector("#ticker", """(el) => {
          el.value = 'AVGO';
          el.dispatchEvent(new Event('input', {bubbles: true}));
        }""")
        _stays_closed(page)
        assert page.input_value("#ticker") == "AVGO"
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_typing_again_reopens_suggestions(base):
    """Não regride a busca: depois de escolher, digitar de novo reabre o popup."""
    with sync_playwright() as p:
        browser = p.chromium.launch(args=_ARGS)
        page = _page(browser, base)
        _type_until_suggestions(page, "AVGO")
        _dispatch_pick(page, "AVGO")
        _stays_closed(page)                          # fechado após escolher
        _type_until_suggestions(page, "AVG")         # digitar reabre normalmente
        assert _opts(page) > 0
        browser.close()

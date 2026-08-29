"""E2E da lista do scan: busca, dois modos de apresentação e o timeframe destacado
(task de UI 010, pedidos 4 e 5 do Samyr).

Pedido 4: campo de BUSCA filtrando os resultados; modos CARDS e LISTA alternáveis;
o TIMEFRAME destacado no INÍCIO da linha — antes saía colado no preço
(``1d$513,530.15%``), sem respiro e sem hierarquia.

Pedido 5 (consequência do DA-070): ``.scan-frame-row`` e ``.scan-row`` trocam o
degradê de estado por borda-esquerda sólida; nada de gradiente em card, linha ou
chip. Isto é testável de verdade — ``getComputedStyle`` denuncia o gradiente.
"""

import json
import re
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


def _frame(frame, estado, **kw):
    base = {"frame": frame, "estado": estado, "direction": "compra", "price": 513.53,
            "dist_pct": 0.0015, "dist_txt": "0.15%", "trigger": 512.76, "sl": 471.35,
            "tp": 515.06, "rr": 0.06, "rr_note": None, "pattern_state": "formando"}
    base.update(kw)
    return base


_SCAN = {
    "date": "2026-08-29", "frames": ["1d", "4h", "1h"],
    "resumo": {"em_gatilho": 2, "formando": 1},
    "ativos": [
        {"ticker": "MSFT", "melhor": _frame("1d", "em_gatilho"),
         "frames": [_frame("1d", "em_gatilho"), _frame("4h", "formando"),
                    _frame("1h", "em_movimento")]},
        {"ticker": "NVDA", "melhor": _frame("1d", "em_gatilho"),
         "frames": [_frame("1d", "em_gatilho", price=217.55)]},
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


def _abre_scan(page, base):
    def handler(route):
        if "/api/scan" in route.request.url and "verdicts" not in route.request.url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_SCAN))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)
    page.goto(base, wait_until="networkidle")
    page.click("#scanOpenBtn")
    page.click("#scanRunBtn")
    page.wait_for_selector("#scanList li")


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_busca_filtra_a_lista(base):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_scan(page, base)
        assert page.locator("#scanList > li").count() == 2
        page.fill("#scanSearch", "nv")            # minúscula: a busca não é sensível
        page.wait_for_timeout(120)
        assert page.locator("#scanList > li").count() == 1
        assert "NVDA" in page.inner_text("#scanList")
        page.fill("#scanSearch", "XPTO")
        page.wait_for_timeout(120)
        assert page.locator(".scan-vazio").count() == 1   # ausência DECLARADA
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_dois_modos_de_apresentacao_e_a_escolha_persiste(base):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_scan(page, base)
        # CARDS: agrupa por ativo (2 li) e cada frame é uma sub-linha
        assert page.locator("#scanList > li.scan-row").count() == 2
        assert page.locator(".scan-frame-row").count() == 4

        page.click(".scan-view[data-view='lista']")
        page.wait_for_timeout(150)
        # LISTA: uma linha por ativo+frame, sem agrupar (4 linhas)
        assert page.locator("#scanList > li.scan-line-row").count() == 4
        assert page.locator("#scanList > li.scan-row").count() == 0
        assert page.evaluate("() => localStorage.getItem('td_scan_view')") == "lista"

        page.reload(wait_until="networkidle")
        page.click("#scanOpenBtn")
        page.click("#scanRunBtn")
        page.wait_for_selector("#scanList li")
        assert page.locator("#scanList > li.scan-line-row").count() == 4, "o modo não persistiu"
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_timeframe_destacado_no_inicio_da_linha(base):
    """Antes: ``1d$513,530.15%`` — o frame colado no preço. Agora o badge vem
    primeiro, com caixa própria e respiro."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_scan(page, base)
        m = page.evaluate("""() => {
          const row = document.querySelector('.scan-frame-row');
          const tf = row.querySelector('.scan-tf');
          const pr = row.querySelector('.scan-price');
          const cs = getComputedStyle(tf);
          return {tfLeft: tf.getBoundingClientRect().left, prLeft: pr.getBoundingClientRect().left,
                  gap: pr.getBoundingClientRect().left - tf.getBoundingClientRect().right,
                  minW: parseFloat(cs.minWidth), borda: cs.borderLeftWidth,
                  txt: row.innerText.slice(0, 12)};
        }""")
        assert m["tfLeft"] < m["prLeft"], m          # o frame vem ANTES do preço
        assert m["gap"] >= 6, ("sem respiro entre frame e preço", m)
        assert m["minW"] >= 30, m                    # caixa própria, largura fixa
        assert m["borda"] != "0px", m                # é um badge, não texto solto
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_DA070_sem_degrade_e_sem_canto_arredondado_nas_linhas(base):
    """DA-070 conferida no que o navegador REALMENTE aplica, nos dois modos."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_scan(page, base)
        for seletor in (".scan-row", ".scan-frame-row"):
            m = page.evaluate(f"""() => {{
              const els = [...document.querySelectorAll('{seletor}')];
              return els.map(e => {{ const c = getComputedStyle(e);
                return {{bg: c.backgroundImage, raio: c.borderTopLeftRadius,
                         bordaEsq: c.borderLeftWidth}}; }});
            }}""")
            assert m, seletor
            assert all(x["bg"] == "none" for x in m), (seletor, m)      # zero degradê
            assert all(x["raio"] == "0px" for x in m), (seletor, m)     # card quadrado
        # o estado vira BORDA-ESQUERDA sólida (a cor continua informando)
        borda = page.evaluate("""() => getComputedStyle(
            document.querySelector('.scan-frame-row.em_gatilho')).borderLeftWidth""")
        assert borda != "0px", borda

        page.click(".scan-view[data-view='lista']")
        page.wait_for_timeout(150)
        m = page.evaluate("""() => [...document.querySelectorAll('.scan-line-row')]
            .map(e => { const c = getComputedStyle(e);
              return {bg: c.backgroundImage, raio: c.borderTopLeftRadius}; })""")
        assert m and all(x["bg"] == "none" and x["raio"] == "0px" for x in m), m
        browser.close()

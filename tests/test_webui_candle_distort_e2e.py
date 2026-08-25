"""E2E (Playwright) da DISTORÇÃO das velas com o zoom (task 015).

Prova o pedido do Samyr: "as velas devem se distorcer conforme o zoom, largura e
altura."
  1. ZOOM HORIZONTAL (régua de baixo): menos velas na janela → cada vela mais LARGA
     (cw = plotWx/vis * 0.7, sem teto travando). Observável em canvas.dataset.cw.
  2. ZOOM VERTICAL (régua direita): comprimir o eixo Y estica corpo/pavio na ALTURA
     (mesma variação de preço ocupa mais pixels). Observável em canvas.dataset.ppp
     (pixels por unidade de preço = plotH/(hi-lo)).

Injeta candles sintéticos no <canvas>, dirige o mouse pelas réguas (mesmo caminho do
usuário) e lê a geometria em canvas.dataset. Skip limpo sem Playwright/Chromium.
"""

import threading

import pytest

from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.server import make_server
from tradingagents.webui.store import HistoryStore

pytestmark = pytest.mark.integration

sync_playwright = None
try:
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


_SEED_JS = r"""
() => {
  const cv = document.getElementById('priceChart');
  document.getElementById('resultPanel').classList.remove('hidden');
  document.getElementById('chartCard').classList.remove('hidden');
  const candles = [];
  let p = 100;
  for (let i = 0; i < 200; i++) {
    const o = p;
    const c = p + Math.sin(i / 7) * 3 + ((i % 5) - 2) * 0.5;
    const h = Math.max(o, c) + 1.5;
    const l = Math.min(o, c) - 1.5;
    const dd = String((i % 28) + 1).padStart(2, '0');
    const mm = String((Math.floor(i / 28) % 12) + 1).padStart(2, '0');
    candles.push({ o, h, l, c, d: `2026-${mm}-${dd}` });
    p = c;
  }
  cv._chart = { candles, ma: {}, ema: {}, markers: {} };
  cv._actionable = null;
  cv._view = null; cv._vview = null;
  drawPriceChart(cv, cv._chart, cv._actionable);
  bindChartZoom(cv);
  cv.scrollIntoView({ block: 'center', inline: 'center' });
  const r = cv.getBoundingClientRect();
  return { left: r.left, top: r.top, width: r.width, height: r.height };
}
"""

_GEOM = r"""
() => {
  const cv = document.getElementById('priceChart');
  return { v0: +cv.dataset.v0, v1: +cv.dataset.v1,
           cw: +cv.dataset.cw, ppp: +cv.dataset.ppp };
}
"""


def _seed(page):
    return page.evaluate(_SEED_JS)


def _geom(page):
    return page.evaluate(_GEOM)


def _drag(page, x0, y0, x1, y1):
    page.mouse.move(x0, y0)
    page.mouse.down()
    page.mouse.move(x1, y1, steps=6)
    page.mouse.up()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_horizontal_zoom_widens_candles(live_server):
    """Zoom h (régua de baixo): aproximar alarga a vela; afastar afina — proporcional."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1000, "height": 860})
        try:
            page.goto(live_server)
            page.wait_for_selector("#priceChart", state="attached")
            r = _seed(page)
            full = _geom(page)
            assert full["cw"] >= 1                      # nunca some

            # arrasta a régua de baixo pra ESQUERDA → menos velas → vela mais LARGA
            cx = r["left"] + r["width"] / 2
            yb = r["top"] + r["height"] - 8
            _drag(page, cx, yb, cx - 260, yb)
            zin = _geom(page)
            assert (zin["v1"] - zin["v0"]) < (full["v1"] - full["v0"])   # zoom in
            assert zin["cw"] > full["cw"] * 1.5, (full, zin)            # visivelmente mais larga

            # arrasta pra DIREITA → mais velas → vela mais FINA
            _drag(page, cx, yb, cx + 260, yb)
            zout = _geom(page)
            assert zout["cw"] < zin["cw"], (zin, zout)
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_vertical_zoom_stretches_candle_height(live_server):
    """Zoom v (régua direita): comprimir o eixo Y estica corpo/pavio na altura."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 860})  # mobile 390
        try:
            page.goto(live_server)
            page.wait_for_selector("#priceChart", state="attached")
            r = _seed(page)
            full = _geom(page)

            # arrasta a régua DIREITA pra CIMA → comprime a janela de preço →
            # mais pixels por unidade de preço (velas mais altas)
            xr = r["left"] + r["width"] - 8
            y0 = r["top"] + r["height"] * 0.5
            _drag(page, xr, y0, xr, y0 - 130)
            zin = _geom(page)
            assert zin["ppp"] > full["ppp"] * 1.3, (full, zin)   # esticou na altura
            # a largura (janela h) NÃO mudou com zoom vertical
            assert zin["cw"] == pytest.approx(full["cw"], rel=0.01)
        finally:
            browser.close()

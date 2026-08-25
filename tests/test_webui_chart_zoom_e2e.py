"""E2E (Playwright) do zoom na régua HORIZONTAL + pan livre 2D do gráfico.

Prova o pedido do Samyr (task 014):
  1. arrastar a RÉGUA DE TEMPO (eixo X, embaixo) comprime/expande o zoom horizontal
     em torno do centro — espelho do zoom-na-régua vertical (task 038);
  2. agarrar o CORPO do gráfico e mover desloca nos DOIS eixos (h E v) mantendo o zoom;
  3. régua direita (zoom vertical) + dblclick-reset seguem sem regressão.

Não roda análise nenhuma: injeta candles sintéticos direto no <canvas>, chama
drawPriceChart + bindChartZoom (funções globais do app.js) e dirige o mouse. As
janelas ficam observáveis em canvas.dataset (v0/v1 = janela h; plo/phi = janela de preço).

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


# Injeta 200 candles sintéticos no #priceChart, desenha e liga o zoom/pan.
# Retorna o retângulo do canvas (CSS px) pra o teste calcular as coordenadas do mouse.
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
  cv.scrollIntoView({ block: 'center', inline: 'center' });  // cabe todo no viewport (mobile 390)
  const r = cv.getBoundingClientRect();
  return { left: r.left, top: r.top, width: r.width, height: r.height };
}
"""

_READ_WIN = r"""
() => {
  const cv = document.getElementById('priceChart');
  return { v0: +cv.dataset.v0, v1: +cv.dataset.v1,
           plo: +cv.dataset.plo, phi: +cv.dataset.phi };
}
"""


def _seed(page):
    return page.evaluate(_SEED_JS)


def _win(page):
    return page.evaluate(_READ_WIN)


def _drag(page, x0, y0, x1, y1):
    page.mouse.move(x0, y0)
    page.mouse.down()
    page.mouse.move(x1, y1, steps=6)
    page.mouse.up()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_bottom_axis_drag_zooms_horizontal(live_server):
    """Arrastar a régua de tempo (embaixo) pra ESQUERDA comprime o zoom h (âncora no centro)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1000, "height": 820})
        try:
            page.goto(live_server)
            page.wait_for_selector("#priceChart", state="attached")
            r = _seed(page)
            before = _win(page)
            assert before["v0"] == 0 and before["v1"] == 200   # visão cheia

            # arrasta na FAIXA de baixo (y perto da base = banda dos rótulos de data),
            # do centro pra ESQUERDA → zoom IN horizontal
            cx = r["left"] + r["width"] / 2
            yb = r["top"] + r["height"] - 8
            _drag(page, cx, yb, cx - 220, yb)
            after = _win(page)
            vis_before = before["v1"] - before["v0"]
            vis_after = after["v1"] - after["v0"]
            assert vis_after < vis_before, (before, after)     # comprimiu (menos candles)
            # âncora no centro: a janela ficou centrada em ~100
            center = (after["v0"] + after["v1"]) / 2
            assert abs(center - 100) <= 12, after

            # agora arrasta pra DIREITA → expande de volta (mais candles)
            _drag(page, cx, yb, cx + 220, yb)
            back = _win(page)
            assert (back["v1"] - back["v0"]) > vis_after, (after, back)
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_body_drag_pans_both_axes(live_server):
    """Com zoom h+v ativos, agarrar o corpo e mover desloca em X E Y mantendo o zoom."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 800})  # mobile 390
        try:
            page.goto(live_server)
            page.wait_for_selector("#priceChart", state="attached")
            r = _seed(page)
            # zoom horizontal (janela 50..150) + zoom vertical (faixa central de preço)
            page.evaluate(r"""() => {
              const cv = document.getElementById('priceChart');
              cv._view = { v0: 50, v1: 150 };
              const lo = +cv.dataset.plo, hi = +cv.dataset.phi;
              const mid = (lo + hi) / 2, rg = (hi - lo) * 0.4;
              cv._vview = { lo: mid - rg / 2, hi: mid + rg / 2 };
              drawPriceChart(cv, cv._chart, cv._actionable);
            }""")
            before = _win(page)
            vis_before = before["v1"] - before["v0"]
            range_before = before["phi"] - before["plo"]

            # agarra o CORPO (centro) e move pra esquerda-baixo
            cx = r["left"] + r["width"] / 2
            cy = r["top"] + r["height"] / 2
            _drag(page, cx, cy, cx - 70, cy + 60)
            after = _win(page)

            # HORIZONTAL: arrastar pra esquerda avança a janela → v0 sobe; zoom preservado
            assert after["v0"] > before["v0"], (before, after)
            assert (after["v1"] - after["v0"]) == vis_before, (before, after)
            # VERTICAL: arrastar pra baixo sobe a janela de preço → plo sobe; zoom preservado
            assert after["plo"] > before["plo"], (before, after)
            assert abs((after["phi"] - after["plo"]) - range_before) < range_before * 0.02
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_right_axis_vertical_zoom_no_regression(live_server):
    """Régua direita (zoom vertical, task 038) e dblclick-reset seguem funcionando."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1000, "height": 820})
        try:
            page.goto(live_server)
            page.wait_for_selector("#priceChart", state="attached")
            r = _seed(page)
            before = _win(page)
            range_before = before["phi"] - before["plo"]

            # arrasta na régua DIREITA pra CIMA → comprime o preço (zoom v in)
            xr = r["left"] + r["width"] - 8
            y0 = r["top"] + r["height"] * 0.5
            _drag(page, xr, y0, xr, y0 - 120)
            zoomed = _win(page)
            assert (zoomed["phi"] - zoomed["plo"]) < range_before, (before, zoomed)
            # a janela horizontal NÃO mudou (zoom v não mexe em x)
            assert zoomed["v0"] == before["v0"] and zoomed["v1"] == before["v1"]

            # dblclick reseta os dois eixos → volta ao auto
            page.mouse.dblclick(r["left"] + r["width"] / 2, r["top"] + r["height"] / 2)
            reset = _win(page)
            assert abs((reset["phi"] - reset["plo"]) - range_before) < range_before * 0.02
            assert reset["v0"] == 0 and reset["v1"] == 200
        finally:
            browser.close()

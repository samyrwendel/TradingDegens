"""E2E (Playwright) da passada de layout (task 018): watchlist sem réguas + rolagem
interna, divisória vertical arrastável (persiste), e gráfico SEM borda verde.

Não regride 009/010/013/015: a lista segue densa/legível, só sem as linhas.
"""

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


def _rec(rid, tk):
    return {"run_id": rid, "ticker": tk, "date": "2026-08-26", "asset_type": "stock",
            "status": "done", "verdict": "COMPRAR", "cost_usd": 0.01, "elapsed": 40,
            "finished_at": "2026-08-26T10:00:00-04:00", "result": {"verdict": "COMPRAR"}}


@pytest.fixture
def base(tmp_path):
    store = HistoryStore(tmp_path)
    for i in range(5):
        store.save(_rec(f"r{i}", f"TICK{i}"))
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "openai",
                     "deep_think_llm": "x", "quick_think_llm": "y"},
        store=store)
    httpd = make_server("127.0.0.1", 0, runner=runner)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_history_has_no_dividers_and_scrolls(base):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        # `networkidle` NÃO serve nesta tela: os pollers de lista (5s) e de preço (40s)
        # batem sozinhos, e sob a carga da suíte cheia a rede não fica 500ms ociosa —
        # o `goto` estoura em 30s e o teste reprova por espera, não por layout. Espera-se
        # o que ele mede. (Mesma lição da DA-139 sobre deadlines de parede.)
        page.goto(base, wait_until="domcontentloaded")
        page.wait_for_selector(".history li")
        # (1) sem régua de 1px entre os itens
        borders = page.evaluate(
            "()=>[...document.querySelectorAll('.history li')]"
            ".map(li=>getComputedStyle(li).borderBottomWidth)")
        assert set(borders) == {"0px"}, borders
        # (2) rolagem interna do painel
        assert page.eval_on_selector(
            ".sidebar .history", "el=>getComputedStyle(el).overflowY") == "auto"
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_vertical_resizer_drags_and_persists(base):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1500, "height": 950})
        page = ctx.new_page()
        page.goto(base, wait_until="domcontentloaded")
        page.wait_for_selector("#colResizer", state="attached")
        rz = page.query_selector("#colResizer")
        assert rz is not None
        assert page.eval_on_selector("#colResizer", "el=>getComputedStyle(el).cursor") == "col-resize"
        box = rz.bounding_box()
        page.mouse.move(box["x"] + 3, box["y"] + 200)
        page.mouse.down()
        page.mouse.move(400, box["y"] + 200, steps=6)
        page.mouse.up()
        w = page.evaluate("()=>document.querySelector('main.layout').style.getPropertyValue('--sidebar-w')")
        assert w.endswith("px") and 360 <= int(w[:-2]) <= 440, w
        assert page.evaluate("()=>localStorage.getItem('td_sidebar_w')") == w[:-2]
        # persiste no reload
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("#colResizer", state="attached")
        w2 = page.evaluate("()=>getComputedStyle(document.querySelector('main.layout')).getPropertyValue('--sidebar-w')")
        assert int(w2.replace("px", "")) == int(w[:-2]), (w, w2)
        # sem overflow horizontal
        ov = page.evaluate("()=>({sw:document.documentElement.scrollWidth, cw:document.documentElement.clientWidth})")
        assert ov["sw"] <= ov["cw"], ov
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_chart_card_setup_active_has_no_green_border(base):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.goto(base, wait_until="domcontentloaded")
        style = page.evaluate("""()=>{
          const d=document.createElement('div'); d.className='chart-card setup-active';
          document.body.appendChild(d); const cs=getComputedStyle(d);
          const r={border: cs.borderColor, shadow: cs.boxShadow}; d.remove(); return r;
        }""")
        assert "46, 125, 85" not in style["border"]        # #2e7d55 (verde) saiu
        assert "46, 204, 113" not in style["shadow"]       # inset verde saiu
        assert style["shadow"] == "none", style
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_resizer_hidden_on_mobile(base):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(base, wait_until="domcontentloaded")
        page.wait_for_selector("#colResizer", state="attached")
        assert page.eval_on_selector("#colResizer", "el=>getComputedStyle(el).display") == "none"
        ov = page.evaluate("()=>({sw:document.documentElement.scrollWidth, cw:document.documentElement.clientWidth})")
        assert ov["sw"] <= ov["cw"], ov
        browser.close()

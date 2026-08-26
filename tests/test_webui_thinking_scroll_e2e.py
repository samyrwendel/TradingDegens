"""E2E (Playwright) — os cards de raciocínio ao vivo são LEGÍVEIS (bug 013).

O bug: ``.thinking-live`` é um flex column de altura fixa, então o flex-shrink
padrão espremia cada ``.tk-card`` pra caber e o ``overflow:hidden`` CORTAVA o texto —
sem rolagem pra ler o parecer completo. FIX: ``.tk-card { flex: 0 0 auto }`` — os
cards mantêm a altura natural (conteúdo completo) e quem rola é o container.

Aqui semeamos cards com texto LONGO (renderThinking real) e provamos, em desktop e
mobile, que (a) o card não está clipado (body inteiro visível), (b) o container rola
pra navegar todos os agentes, (c) sem overflow horizontal no 390.
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

_LONG = ("LEITURA DO MÓDULO: o preço rompeu a EMA de 21 com volume acima da média e "
         "o RSI diário está em 58, sugerindo continuidade sem sobrecompra. Zonas de "
         "recuo em 312,40 e 308,90 válidas; alvo 1 em 330; gatilho 1-2-3 acima de 316. ") * 5

_ITEMS = [
    {"id": "Market Analyst", "label": "📊 Mercado", "phase": "Analistas",
     "debate": False, "order": 1, "len": len(_LONG), "text": _LONG},
    {"id": "News Analyst", "label": "📰 Notícias", "phase": "Analistas",
     "debate": False, "order": 2, "len": len(_LONG), "text": _LONG},
    {"id": "Bull Researcher", "label": "🟢 Tese de Alta", "phase": "Debate",
     "debate": True, "order": 5, "len": len(_LONG), "text": _LONG},
    {"id": "Trader", "label": "🧑 Plano do Trader", "phase": "Decisão",
     "debate": False, "order": 8, "len": len(_LONG), "text": _LONG},
]


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


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("w,h", [(1500, 950), (390, 844)])
def test_thinking_cards_readable_and_container_scrolls(base, w, h):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": w, "height": h})
        page.goto(base, wait_until="networkidle")
        page.evaluate("""(items)=>{
          document.getElementById('progressPanel').classList.remove('hidden');
          renderThinking(items);
        }""", _ITEMS)
        page.wait_for_selector(".tk-card .tk-body")

        m = page.evaluate("""()=>{
          const box = document.getElementById('thinkingLive');
          const cards = [...box.querySelectorAll('.tk-card')];
          const clipped = cards.filter(c => c.scrollHeight > c.clientHeight + 2).length;
          const bodies = [...box.querySelectorAll('.tk-body')];
          const bodyClipped = bodies.filter(b => b.scrollHeight > b.clientHeight + 2).length;
          return {
            cards: cards.length,
            cardClipped: clipped,           // nenhum card pode estar cortado
            bodyClipped: bodyClipped,       // nenhum body pode estar cortado
            containerScrollable: box.scrollHeight > box.clientHeight + 2,
            firstBodyChars: bodies[0].textContent.length,
          };
        }""")
        assert m["cards"] == 4, m
        assert m["cardClipped"] == 0, m           # cards NÃO clipados (o bug)
        assert m["bodyClipped"] == 0, m           # texto completo em cada card
        assert m["containerScrollable"] is True, m  # o container rola pra navegar
        assert m["firstBodyChars"] > 500, m       # conteúdo longo, inteiro presente

        # sem overflow horizontal (mobile 390 incluso)
        ov = page.evaluate(
            "()=>({sw:document.documentElement.scrollWidth, cw:document.documentElement.clientWidth})")
        assert ov["sw"] <= ov["cw"], ov
        browser.close()

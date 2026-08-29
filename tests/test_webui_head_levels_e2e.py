"""E2E do rodapé do cabeçalho: gatilhos no canto inferior direito + preço que DIZ
qual preço é (task de UI 010, pedidos 2 e 3 do Samyr).

Pedido 2: o veredito FICA em cima; os gatilhos descem pro canto inferior direito do
card, ao lado do preço atual, alinhados à direita.

Pedido 3: a análise 1-2-3 tem que buscar a cotação ATUAL, e a tela tem que dizer QUE
preço está mostrando. O plano é date-guarded — o número que ele carrega é o último
FECHAMENTO da série (MSFT em 29/08: 505,06 de 27/08 com o papel valendo 513,53) — e
a tela o exibia como se fosse "agora". Fechamento, pré-market e after-market são
preços diferentes; o rótulo é o que impede a tela de chamar qualquer um de "agora".
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


_SNAP = {
    "run_id": "R-HEAD", "ticker": "MSFT", "date": "2026-08-29", "asset_type": "stock",
    "status": "done", "elapsed": 1, "cost": {"usd": 0.0},
    "verdict": None, "verdict_timeframe": "1d",
    "result": {
        "setup123": True, "verdict": None, "final_decision": "",
        "timeframe": "1d", "as_of_price": 505.06,
        "actionable": {
            "price": 505.06, "as_of": "2026-08-27", "setup_state": "aguardar_rompimento",
            "pattern": {"trigger": 512.76, "state": "formando", "direction": "compra"},
            "stop": {"price": 471.35, "basis": "invalidação + folga"},
            "target": {"price": 515.06, "label": "topo anterior 2025-09-19"},
            "risk_reward": {"rr": 0.06, "entry": 512.76, "risk": 41.41, "reward": 2.3,
                            "note": None, "entry_basis": "gatilho"},
            "invalidation": {"price": 476.25, "meaning": "perde o ponto 3"},
        },
        # cotação ATUAL com a sessão declarada (o que o runner passou a anexar)
        "live_price": {"price": 513.53, "change_pct": 1.68, "currency": "USD",
                       "sessao": "fechado", "rotulo": "último fechamento",
                       "as_of": "28/08 16:00", "regular_price": 513.53,
                       "fuso": "America/New_York", "em": "2026-08-29"},
        "price_chart": {}, "degraded": [],
        "bull": "", "bear": "", "research_manager": "", "investment_plan": "",
        "trader_plan": "", "risk_decision": "", "market_report": "",
        "sentiment_report": "", "news_report": "", "fundamentals_report": "",
        "erick_report": "", "drop_nature": {}, "derivatives_report": "",
    },
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


def _abre_resultado(page, snap):
    """Serve a run pronta e manda o front abri-la (sem rodar nada)."""
    def handler(route):
        url = route.request.url
        if "/api/status/" in url or re.search(r"/api/run/[^/]+$", url):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(snap))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)
    page.goto(page.url if page.url.startswith("http") else "about:blank")


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_gatilhos_no_canto_inferior_direito_com_o_veredito_em_cima(base):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_resultado(page, _SNAP)
        page.goto(base, wait_until="networkidle")
        page.evaluate("() => watchRun('R-HEAD')")
        page.wait_for_selector("#headLevels:not(.hidden)")

        m = page.evaluate("""() => {
          const box = (s) => { const e = document.querySelector(s); if (!e) return null;
            const r = e.getBoundingClientRect(); return {top: r.top, right: r.right, left: r.left}; };
          return {veredito: box('#verdictBadge'), tira: box('#headLevels'),
                  gatilhos: box('#headTriggers'), preco: box('#headPrice'),
                  card: box('#resultPanel'),
                  txtGatilhos: document.querySelector('#headTriggers').innerText,
                  txtPreco: document.querySelector('#headPrice').innerText};
        }""")
        # veredito EM CIMA da tira (pedido: ele permanece onde está)
        assert m["veredito"]["top"] < m["tira"]["top"], m
        # tira ALINHADA À DIREITA do card (a menos da borda/padding)
        assert m["card"]["right"] - m["tira"]["right"] < 40, m
        # gatilhos ao lado (à esquerda) do preço, na mesma tira
        assert m["gatilhos"]["left"] < m["preco"]["left"], m
        # e os níveis estão lá, com os números da análise
        assert "gatilho" in m["txtGatilhos"] and "512,76" in m["txtGatilhos"], m
        assert "SL" in m["txtGatilhos"] and "471,35" in m["txtGatilhos"], m
        assert "TP" in m["txtGatilhos"] and "515,06" in m["txtGatilhos"], m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_preco_diz_QUAL_preco_e(base):
    """DENTE do pedido 3: sem o rótulo, a tela mostra 505,06 (fechamento de 27/08)
    como se fosse a cotação de agora. Com ele, aparecem os DOIS, cada um nomeado."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_resultado(page, _SNAP)
        page.goto(base, wait_until="networkidle")
        page.evaluate("() => watchRun('R-HEAD')")
        page.wait_for_selector("#headPrice:not(.hidden)")
        txt = page.inner_text("#headPrice")
        assert "513,53" in txt, txt                     # a cotação atual
        assert "ÚLTIMO FECHAMENTO" in txt.upper(), txt  # DIZENDO que é fechamento
        assert "28/08 16:00" in txt, txt                # e de quando, no fuso da bolsa
        assert "505,06" in txt and "27/08" in txt, txt  # o preço da ANÁLISE ao lado
        assert "ANÁLISE" in txt.upper(), txt
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_cotacao_de_ontem_nao_se_disfarca_de_agora(base):
    """A run é persistida inteira: reaberta amanhã, a cotação carimbada com o dia
    anterior não pode aparecer como atual. Só o preço da análise sobra."""
    snap = json.loads(json.dumps(_SNAP))
    snap["result"]["live_price"]["em"] = "2020-01-01"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_resultado(page, snap)
        page.goto(base, wait_until="networkidle")
        page.evaluate("() => watchRun('R-HEAD')")
        page.wait_for_selector("#headPrice:not(.hidden)")
        txt = page.inner_text("#headPrice")
        assert "513,53" not in txt, txt
        assert "505,06" in txt, txt
        browser.close()

"""E2E (Playwright) do HISTÓRICO como watchlist densa + bordas uniformes (task 009).

Prova os dois pedidos do Samyr:
  1. BORDAS uniformes/menos arredondadas: todos os botões dividem o MESMO raio
     pequeno (token --radius = 5px). Aqui checamos botões sempre presentes
     (#runBtn "Analisar" e #configBtn "Chaves") + a linha do histórico, que agora
     NÃO é um cartão arredondado (raio 0, divisória de 1px embaixo).
  2. LISTA estilo watchlist (ref lista-ref.jpg): cada linha é ticker (negrito) +
     nome da empresa (cinza) à esquerda; veredito + data à direita; × discreto pra
     remover. Abas Todos/Ações/Cripto com sublinhado verde no ativo (sem caixa).
     O × remove o ATIVO (todas as análises do ticker) — real, via DELETE.

Semeia o histórico no store real (mesmo caminho do usuário) e checa DOM + estilo
computado. Skip limpo sem Playwright/Chromium.
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

# Mesmos flags CI-safe dos outros e2e do webui (força render por software).
_CHROMIUM_ARGS = [
    "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
    "--disable-software-rasterizer", "--disable-gpu-compositing",
    "--disable-features=VizDisplayCompositor",
]


def _rec(run_id, ticker, verdict, asset_type="stock"):
    return {
        "run_id": run_id, "ticker": ticker, "date": "2026-08-22",
        "asset_type": asset_type, "status": "done", "verdict": verdict,
        "cost_usd": 0.0123, "elapsed": 42.0, "finished_at": "2026-08-22T12:00:00-04:00",
        "result": {"verdict": verdict},
    }


@pytest.fixture
def live(tmp_path):
    store = HistoryStore(tmp_path)
    store.save(_rec("m1", "MCD", "COMPRAR"))
    store.save(_rec("m2", "MCD", "MANTER"))          # 2º run do MESMO ativo
    store.save(_rec("b1", "BTC", "COMPRAR", "crypto"))
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "openai",
                     "deep_think_llm": "gpt-5.5", "quick_think_llm": "gpt-5.4-mini"},
        store=store)
    httpd = make_server("127.0.0.1", 0, runner=runner)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}", store
    finally:
        httpd.shutdown()


_SEED_NAMES = ("(names)=>{for(const k in names){_nameCache.set(k,names[k]);} "
               "if(window.paintHistory) paintHistory();}")


@pytest.fixture
def live_long(tmp_path):
    # ticker longo (ZEC-USD) + veredito COM qualificador inglês (Overweight) —
    # o caso que quebrava a linha e espremia o nome (bug 015).
    store = HistoryStore(tmp_path)
    store.save(_rec("z1", "ZEC-USD", "Overweight", "crypto"))
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "openai",
                     "deep_think_llm": "gpt-5.5", "quick_think_llm": "gpt-5.4-mini"},
        store=store)
    httpd = make_server("127.0.0.1", 0, runner=runner)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_long_ticker_one_line_name_legible_qualifier_hidden(live_long):
    with sync_playwright() as p:
        browser = p.chromium.launch(args=_CHROMIUM_ARGS)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.goto(live_long, wait_until="networkidle")
        page.evaluate(_SEED_NAMES, {"ZEC-USD": "Zcash"})
        page.wait_for_selector('.history li[data-ticker="ZEC-USD"] .tk-sym')
        row = page.query_selector('.history li[data-ticker="ZEC-USD"]')

        m = page.evaluate("""()=>{
          const li = document.querySelector('.history li[data-ticker="ZEC-USD"]');
          const sym = li.querySelector('.tk-sym');
          const lh = parseFloat(getComputedStyle(sym).lineHeight) || 20;
          const orig = li.querySelector('.h-verdict .verdict-orig');
          const co = li.querySelector('.tk-co');
          return {
            symText: sym.textContent,
            symH: sym.getBoundingClientRect().height, lh,
            origVisible: orig ? orig.offsetHeight > 0 : false,
            coText: co ? co.textContent : "",
          };
        }""")
        assert m["symText"] == "ZEC-USD"
        assert m["symH"] <= m["lh"] * 1.6, m       # ticker em UMA linha (não quebrou)
        assert m["origVisible"] is False, m        # "Overweight" escondido na lateral
        assert m["coText"] == "Zcash"              # nome legível, não "Zca…"

        ov = page.evaluate(
            "()=>({sw:document.documentElement.scrollWidth, cw:document.documentElement.clientWidth})")
        assert ov["sw"] <= ov["cw"], ov
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_watchlist_layout_and_uniform_radius(live):
    base, _store = live
    with sync_playwright() as p:
        browser = p.chromium.launch(args=_CHROMIUM_ARGS)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.goto(base, wait_until="networkidle")
        page.evaluate(_SEED_NAMES, {"MCD": "McDonald's Corp.", "BTC": "Bitcoin"})
        page.wait_for_selector(".history li .h-remove")

        # (1) BORDAS: raio único em botões sempre presentes + a linha do histórico
        # não é cartão arredondado (raio 0). A régua de 1px entre itens saiu na 018
        # (watchlist contínua estilo Quantfury) — a densidade vem do espaçamento.
        run_r = page.eval_on_selector("#runBtn", "el=>getComputedStyle(el).borderRadius")
        cfg_r = page.eval_on_selector("#configBtn", "el=>getComputedStyle(el).borderRadius")
        assert run_r == "5px", run_r
        assert cfg_r == "5px", cfg_r            # era pílula (999px) — agora igual
        li = page.eval_on_selector(".history li", """el=>{
          const cs = getComputedStyle(el);
          return {radius: cs.borderRadius, bottom: cs.borderBottomWidth,
                  display: cs.display};
        }""")
        assert li["radius"] == "0px", li
        assert li["bottom"] == "0px", li        # sem divisória entre itens (task 018)
        assert li["display"] == "grid", li

        # abas: sublinhado verde no ativo, sem caixa (borda só embaixo)
        tab = page.eval_on_selector(".h-tab.is-active", """el=>{
          const cs = getComputedStyle(el);
          return {bw: cs.borderBottomWidth, bc: cs.borderBottomColor,
                  tw: cs.borderTopWidth};
        }""")
        assert tab["tw"] == "0px", tab          # sem caixa em volta
        assert tab["bw"] == "2px", tab
        assert "46, 204, 113" in tab["bc"], tab["bc"]   # var(--green) #2ecc71

        # (2) LAYOUT watchlist: uma linha por ATIVO (MCD agregado) com as peças
        rows = page.query_selector_all(".history li")
        assert len(rows) == 2, len(rows)        # MCD + BTC (m1/m2 agregam em 1)
        first = page.query_selector('.history li[data-ticker="MCD"]')
        assert first.query_selector(".h-ticker .tk-sym").inner_text() == "MCD"
        assert "McDonald" in first.query_selector(".h-ticker .tk-co").inner_text()
        assert first.query_selector(".h-right .h-verdict") is not None
        assert first.query_selector(".h-right .h-meta") is not None
        assert first.query_selector("button.h-remove") is not None

        # ticker acima do nome (2 linhas): o nome fica MAIS EMBAIXO que o símbolo
        sym_box = first.query_selector(".tk-sym").bounding_box()
        co_box = first.query_selector(".tk-co").bounding_box()
        assert co_box["y"] > sym_box["y"], (sym_box, co_box)

        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_price_is_third_line(live, monkeypatch):
    # 3ª linha = PREÇO LIVE (task 010). Troca a fonte por um fake (sem rede) e checa
    # que a linha aparece abaixo do nome, com valor + variação colorida.
    import tradingagents.dataflows.live_price as lp
    monkeypatch.setattr(lp, "fetch_live_price", lambda s: (
        {"price": 267.12, "change_pct": -0.37, "currency": "USD"} if s.upper() == "MCD"
        else {"price": 64230.5, "change_pct": 2.11, "currency": "USD"} if s.upper() == "BTC"
        else None))
    base, _store = live
    with sync_playwright() as p:
        browser = p.chromium.launch(args=_CHROMIUM_ARGS)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.goto(base, wait_until="networkidle")
        page.evaluate(_SEED_NAMES, {"MCD": "McDonald's Corp.", "BTC": "Bitcoin"})
        # a 3ª linha nasce "—" e o refreshNewPrices preenche com o preço do fake
        page.wait_for_selector('.history li[data-ticker="MCD"] .h-price .pval')
        mcd = page.query_selector('.history li[data-ticker="MCD"]')
        price = mcd.query_selector(".h-price .pval").inner_text()
        assert "267.12" in price and price.startswith("$"), price
        chg = mcd.query_selector(".h-price .pchg")
        assert "0.37%" in chg.inner_text()
        assert "down" in (chg.get_attribute("class") or "")   # variação negativa = vermelho

        # o preço fica ABAIXO do nome da empresa (é a 3ª linha)
        co_y = mcd.query_selector(".tk-co").bounding_box()["y"]
        pr_y = mcd.query_selector(".h-price").bounding_box()["y"]
        assert pr_y > co_y, (co_y, pr_y)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_remove_button_deletes_ticker(live):
    base, store = live
    with sync_playwright() as p:
        browser = p.chromium.launch(args=_CHROMIUM_ARGS)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.on("dialog", lambda d: d.accept())     # confirma o "remover?"
        page.goto(base, wait_until="networkidle")
        page.evaluate(_SEED_NAMES, {"MCD": "McDonald's Corp.", "BTC": "Bitcoin"})
        page.wait_for_selector('.history li[data-ticker="MCD"] .h-remove')

        assert store.recent()                        # tem MCD no disco
        page.click('.history li[data-ticker="MCD"] .h-remove')
        # a lista re-carrega e o MCD some; o BTC permanece
        page.wait_for_selector('.history li[data-ticker="MCD"]', state="detached")
        assert page.query_selector('.history li[data-ticker="BTC"]') is not None

        # e sumiu do STORE de verdade (as duas análises do ticker)
        assert [r["ticker"] for r in store.recent()] == ["BTC"]
        browser.close()

"""E2E (Playwright) do botão "Exportar PDF" (task 016).

Prova o pedido do Samyr: "cria um botão de exportar o PDF da análise escolhida ou
da comparação." A abordagem é o print-to-PDF do navegador (sem lib):

  1. ANÁLISE ÚNICA: com um resultado na tela, disparar `beforeprint` (o mesmo
     evento que `window.print()` e o `page.pdf()` do Chromium emitem) deve:
       - ABRIR todos os <details> colapsados (relatórios) pro PDF conter tudo;
       - carimbar o <title> com o nome sugerido (TradingDegens_MSFT_4h_2026-08-25);
       - sob @media print, ESCONDER o chrome (topo/launcher/histórico/rodapé/nav/Q&A)
         e MANTER o cabeçalho (veredito), o GRÁFICO (canvas) e os relatórios.
     `afterprint` restaura o estado (título + <details>).
  2. COMPARAÇÃO: idem, com as duas colunas + meta-juiz visíveis.
  3. `page.pdf()` gera um PDF real não-vazio (smoke do pipeline inteiro).

Semeia o DOM chamando os renderizadores reais (renderResult/renderCompare) com um
snapshot sintético — mesmo caminho do usuário. Skip limpo sem Playwright/Chromium.
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


# --- helpers de semeadura (renderizadores reais com snapshot sintético) --------
_CANDLES_JS = r"""
  const candles = [];
  let p = 100;
  for (let i = 0; i < 60; i++) {
    const o = p;
    const c = p + Math.sin(i / 6) * 3 + ((i % 5) - 2) * 0.5;
    const h = Math.max(o, c) + 1.5;
    const l = Math.min(o, c) - 1.5;
    const dd = String((i % 28) + 1).padStart(2, '0');
    candles.push({ o, h, l, c, d: `2026-06-${dd}` });
    p = c;
  }
"""

_SEED_SINGLE = r"""
() => {
""" + _CANDLES_JS + r"""
  const snap = {
    run_id: "test-single-1", ticker: "MSFT", date: "2026-08-25",
    asset_type: "stock", status: "done", cost: 0.1234, elapsed: 42,
    verdict_timeframe: "4h", finished_at: "2026-08-25T16:00:00-04:00",
    result: {
      verdict: "BUY", timeframe: "4h", verdict_timeframe: "4h",
      bull: "## Tese de alta\nMomentum forte acima da EMA.",
      bear: "## Tese de baixa\nResistencia proxima limita.",
      market_report: "### Mercado\nPreco acima da EMA 8/21.",
      news_report: "### Noticias\nMacro neutro.",
      sentiment_report: "### Sentimento\nOtimista.",
      fundamentals_report: "### Fundamentos\nMultiplos ok.",
      trader_plan: "### Plano do Trader\nComprar no recuo a media.",
      risk_decision: "### Decisao de Risco\nVeredito final: comprar.",
      research_manager: "### Juiz do Debate\nAlta vence com folga.",
      actionable: null,
      price_chart: { candles, ma: {}, ema: {}, markers: {} },
      degraded: []
    }
  };
  renderResult(snap);
  const cv = document.getElementById('priceChart');
  if (cv) cv.scrollIntoView({ block: 'center' });
  return document.querySelectorAll('#resultPanel details').length;
}
"""

_SEED_COMPARE = r"""
() => {
""" + _CANDLES_JS + r"""
  const col = (method, verdict) => ({
    method, label: method === 'erick' ? 'Metodo Erick' : 'Padrao',
    verdict, status: 'done', date: '2026-08-25', run_id: 'run-' + method,
    timeframe: '4h', verdict_timeframe: '4h',
    trader_plan: '### Plano ' + method + '\nEntrada no recuo, saida antes da reversao.',
    erick_report: method === 'erick' ? '### Erick\nRecuo a media EMA 8/21.' : '',
    price_chart: { candles, ma: {}, ema: {}, markers: {} },
    cost: 0.05, elapsed: 20, degraded: []
  });
  const snap = {
    run_id: "test-compare-1", ticker: "MSFT", date: "2026-08-25",
    asset_type: "stock", status: "done", cost: 0.2, elapsed: 55,
    finished_at: "2026-08-25T16:00:00-04:00",
    result: { compare: {
      meta: {
        agreement: "divergem",
        headline: "Padrao compra, Erick espera recuo.",
        concordancia: "Ambos veem tendencia de alta.",
        divergencia: "O timing diverge: entrar agora x esperar.",
        significado: "A divergencia e o sinal — reduzir tamanho."
      },
      a: col('padrao', 'BUY'),
      b: col('erick', 'HOLD')
    } }
  };
  renderCompare(snap);
  return document.querySelectorAll('#comparePanel .cmp-col').length;
}
"""

# lê estado de impressão: display computado do chrome + visibilidade do conteúdo
_PRINT_STATE = r"""
(sel) => {
  const disp = (s) => {
    const el = document.querySelector(s);
    if (!el) return 'missing';
    return getComputedStyle(el).display;
  };
  const panelSel = sel.panel;
  return {
    title: document.title,
    // chrome que NÃO deve sair no PDF
    topbar: disp('.topbar'),
    launcher: disp('.launcher'),
    sidebar: disp('.sidebar'),
    footer: disp('footer'),
    headNav: disp(sel.nav),
    ask: disp(sel.ask),
    exportBtn: disp(sel.btn),
    // conteúdo que DEVE sair
    panel: disp(panelSel),
    verdict: (document.querySelector(sel.verdict) || {}).textContent || '',
    verdictDisp: disp(sel.verdict),
    canvas: disp(sel.canvas),
    canvasH: (document.querySelector(sel.canvas) || {}).offsetHeight || 0,
    detailsTotal: document.querySelectorAll(panelSel + ' details').length,
    detailsOpen: document.querySelectorAll(panelSel + ' details[open]').length,
    firstMdDisp: disp(panelSel + ' details .md'),
  };
}
"""


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_export_pdf_single(live_server):
    """Análise única: beforeprint expande relatórios + esconde chrome; PDF sai."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        try:
            page.goto(live_server)
            page.wait_for_selector("#resultPanel", state="attached")
            n_details = page.evaluate(_SEED_SINGLE)
            assert n_details >= 3, f"esperava vários <details>, veio {n_details}"

            # o botão aparece na análise (some no estado de erro; aqui é sucesso)
            assert page.is_visible("#exportPdfBtn")

            sel = {
                "panel": "#resultPanel", "nav": ".head-nav", "ask": "#askSingle",
                "btn": "#exportPdfBtn", "verdict": "#verdictBadge",
                "canvas": "#priceChart",
            }

            # emula impressão e dispara o MESMO evento que window.print()/page.pdf()
            page.emulate_media(media="print")
            page.evaluate("() => window.dispatchEvent(new Event('beforeprint'))")
            st = page.evaluate(_PRINT_STATE, sel)

            # nome de arquivo sugestivo via <title>
            assert st["title"] == "TradingDegens_MSFT_4h_2026-08-25", st["title"]
            # relatórios TODOS abertos pro PDF conter a análise completa
            assert st["detailsOpen"] == st["detailsTotal"] and st["detailsTotal"] >= 3, st
            assert st["firstMdDisp"] != "none", st  # corpo do relatório visível
            # chrome escondido na impressão
            for k in ("topbar", "launcher", "sidebar", "footer", "headNav", "ask", "exportBtn"):
                assert st[k] == "none", f"{k} deveria sumir no print, veio {st[k]}"
            # conteúdo presente: veredito + gráfico
            assert "COMPRAR" in st["verdict"], st["verdict"]
            assert st["verdictDisp"] != "none"
            assert st["canvas"] != "none" and st["canvasH"] > 0, st

            # afterprint restaura título e estado dos <details>
            page.evaluate("() => window.dispatchEvent(new Event('afterprint'))")
            after = page.evaluate(_PRINT_STATE, sel)
            assert after["title"] == "TradingDegens", "título não restaurado"
            assert after["detailsOpen"] < after["detailsTotal"], "estado dos <details> não restaurado"

            # pipeline real: page.pdf() gera um PDF não-vazio
            page.emulate_media(media="screen")
            pdf = page.pdf(format="A4", print_background=True)
            assert pdf[:5] == b"%PDF-" and len(pdf) > 2000, len(pdf)
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_export_pdf_compare(live_server):
    """Comparação: as duas colunas + meta-juiz saem; chrome escondido; PDF gerado."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        try:
            page.goto(live_server)
            page.wait_for_selector("#comparePanel", state="attached")
            n_cols = page.evaluate(_SEED_COMPARE)
            assert n_cols == 2, f"esperava 2 colunas, veio {n_cols}"

            assert page.is_visible("#exportPdfCmpBtn")

            sel = {
                "panel": "#comparePanel", "nav": ".head-nav", "ask": "#askCompare",
                "btn": "#exportPdfCmpBtn", "verdict": ".cmp-verdict-row .verdict",
                "canvas": ".cmp-canvas",
            }

            page.emulate_media(media="print")
            page.evaluate("() => window.dispatchEvent(new Event('beforeprint'))")
            st = page.evaluate(_PRINT_STATE, sel)

            assert st["title"] == "TradingDegens_MSFT_4h_2026-08-25", st["title"]
            # meta-juiz visível + as duas colunas
            assert page.evaluate("() => getComputedStyle(document.querySelector('.meta-judge')).display") != "none"
            assert page.evaluate("() => document.querySelectorAll('#comparePanel .cmp-col').length") == 2
            for k in ("topbar", "launcher", "sidebar", "footer", "ask", "exportBtn"):
                assert st[k] == "none", f"{k} deveria sumir no print, veio {st[k]}"
            assert "COMPRAR" in st["verdict"], st["verdict"]
            assert st["canvas"] != "none" and st["canvasH"] > 0, st

            page.evaluate("() => window.dispatchEvent(new Event('afterprint'))")
            assert page.evaluate("() => document.title") == "TradingDegens"

            page.emulate_media(media="screen")
            pdf = page.pdf(format="A4", print_background=True)
            assert pdf[:5] == b"%PDF-" and len(pdf) > 2000, len(pdf)
        finally:
            browser.close()

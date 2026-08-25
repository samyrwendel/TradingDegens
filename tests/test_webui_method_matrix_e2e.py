"""E2E (Playwright) — MATRIZ COMPLETA de método × timeframe × entrada (task 018).

Prova a consolidação dos controles pedida pelo Samyr ("confirmar e TESTAR TODAS as
combinações antes de dizer que foi feito"):

  * método vive num lugar SÓ — a barra de reanálise [Padrão][🧭 Erick][⚖️ Comparar];
  * o launcher (ATIVO+DATA+Analisar) NÃO tem mais checkbox e roda sempre Padrão;
  * "Atualizar" foi absorvido pela barra: o método aberto fica destacado (is-open) e
    a reanálise sempre sai na data de HOJE.

Sem rodar LLM: intercepta o POST /api/analyze (Playwright route) e LÊ o corpo, provando
que cada combinação manda o método + timeframe + data corretos. Cobre AÇÃO e CRIPTO,
as duas rotas de entrada, os casos críticos (Erick preserva Erick; Comparar sempre
Padrão × Erick; default Padrão) e a matriz inteira 3×5.

Pulado com skip se Playwright/Chromium não estiver disponível.
"""

import json
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


TODAY = "2026-08-25"          # data de "hoje" forçada no cliente (semântica do Atualizar)
OPEN_DATE = "2026-08-20"      # data da análise ABERTA (≠ hoje) — prova que a reanálise usa hoje
METHODS = ["padrao", "erick", "compare"]
TFS = ["1w", "1d", "4h", "1h", "15m"]
TF_PT = {"1w": "semanal", "1d": "diário", "4h": "4h", "1h": "1h", "15m": "15m"}
ASSETS = [("AAPL", "ação"), ("BTC-USD", "cripto")]


@pytest.fixture
def live_server(tmp_path):
    runner = AnalysisRunner(
        base_config={
            "results_dir": str(tmp_path),
            "llm_provider": "openai",
            "deep_think_llm": "gpt-5.5",
            "quick_think_llm": "gpt-5.4-mini",
        },
        store=HistoryStore(tmp_path),
    )
    httpd = make_server("127.0.0.1", 0, runner=runner)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


# Semeia o estado "ativo ABERTO" e renderiza a barra de reanálise (sem rodar nada).
# openView = método destacado na barra ("padrao"|"erick"|"compare").
_SEED_JS = r"""
(args) => {
  const [ticker, tf, openView, today, openDate] = args;
  document.getElementById('progressPanel').classList.add('hidden');
  document.getElementById('resultPanel').classList.add('hidden');
  document.getElementById('comparePanel').classList.add('hidden');
  _openTicker = ticker;
  _openDate = openDate;
  _todayManaus = today;
  _assetType = /-(USD|USDT)$|^BTC|^ETH/.test(ticker.toUpperCase()) ? 'crypto' : 'stock';
  _timeframes = ['1w','1d','4h','1h','15m'];
  _verdictTf = '1d';
  _reTf = tf;
  _openMethod = (openView === 'erick') ? 'erick' : 'padrao';
  _openView = openView;
  renderReanalyzeBar();
  return true;
}
"""


def _route_stub(page):
    """Intercepta /api/analyze e responde stub (não chega no motor). Também neutraliza
    o polling de /api/status (404 → o cliente ignora sem re-renderizar), pra o watchRun
    disparado por cada clique não esconder a barra no meio da matriz."""
    page.route(
        "**/api/analyze",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"run_id": "stub-run"}),
        ),
    )
    page.route(
        "**/api/status/**",
        lambda route: route.fulfill(
            status=404, content_type="application/json", body="{}"
        ),
    )


def _seed(page, ticker, tf, open_view):
    page.evaluate(_SEED_JS, [ticker, tf, open_view, TODAY, OPEN_DATE])


def _bar_post(page, ticker, tf, method, open_view="padrao"):
    """Semeia a barra, clica TF depois método, devolve o corpo do POST interceptado."""
    _seed(page, ticker, tf, open_view)
    page.click(f'#reanalyzeBar button.re-tf[data-retf="{tf}"]')
    with page.expect_request("**/api/analyze") as ri:
        page.click(f'#reanalyzeBar button.re-method[data-method="{method}"]')
    return json.loads(ri.value.post_data)


def _launcher_post(page, ticker):
    page.fill("#ticker", ticker)
    page.fill("#date", TODAY)
    with page.expect_request("**/api/analyze") as ri:
        page.click("#runBtn")
    return json.loads(ri.value.post_data)


def _expect_bar_body(body, method, tf):
    """Valida o POST de uma combinação da barra (método + TF + data de hoje)."""
    assert body.get("timeframe") == tf, ("timeframe", body)
    assert body.get("date") == TODAY, ("data != hoje", body)
    if method == "compare":
        # Comparar SEMPRE dispara Padrão × Erick no backend (compare=true) — nunca
        # método × ele-mesmo. O method do corpo é irrelevante (o backend roda os dois).
        assert body.get("compare") is True, ("compare", body)
    else:
        assert body.get("compare") in (False, None), ("compare deveria ser falso", body)
        assert body.get("method") == method, ("method", body)


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_full_method_timeframe_matrix(live_server):
    """A matriz inteira: método{padrao,erick,compare} × TF{5} × ativo{ação,cripto}
    pela barra + a rota do launcher (default Padrão). Imprime a tabela de resultados."""
    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _route_stub(page)
        try:
            page.goto(live_server)
            page.wait_for_selector("#analyzeForm", state="attached")

            # rota do LAUNCHER: sempre Padrão, sem compare (default Padrão)
            for ticker, asset_pt in ASSETS:
                body = _launcher_post(page, ticker)
                ok = body.get("method") == "padrao" and body.get("compare") in (False, None)
                rows.append(("launcher", asset_pt, ticker, "padrao", "(default)", ok, body))
                assert ok, ("launcher default padrao", body)

            # rota da BARRA: método × TF × ativo
            for ticker, asset_pt in ASSETS:
                for method in METHODS:
                    for tf in TFS:
                        body = _bar_post(page, ticker, tf, method)
                        _expect_bar_body(body, method, tf)
                        rows.append(("barra", asset_pt, ticker, method, tf, True, body))
        finally:
            browser.close()

    # imprime a tabela (capturada com -s) — o que foi testado e passou
    print("\n\n=== MATRIZ método × timeframe × entrada (POST /api/analyze interceptado) ===")
    print(f"{'entrada':9} {'ativo':6} {'ticker':8} {'método':8} {'TF':8} {'compare':7} {'result'}")
    for entry, asset_pt, ticker, method, tf, ok, body in rows:
        tf_lbl = TF_PT.get(tf, tf)
        print(f"{entry:9} {asset_pt:6} {ticker:8} {method:8} {tf_lbl:8} "
              f"{str(body.get('compare', False)):7} {'✅' if ok else '❌'}")
    total = len(rows)
    green = sum(1 for r in rows if r[5])
    print(f"--- {green}/{total} combinações verdes ---")
    assert green == total


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_reanalyze_from_erick_preserves_erick(live_server):
    """Estando numa análise ERICK, a barra DESTACA Erick (is-open) e reanalisar Erick
    mantém Erick em TODOS os TFs — a classe de bug do 037/039 não volta."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _route_stub(page)
        try:
            page.goto(live_server)
            page.wait_for_selector("#analyzeForm", state="attached")
            for tf in TFS:
                _seed(page, "AAPL", tf, "erick")
                # o botão Erick fica destacado (é o "Atualizar" embutido)
                cls = page.get_attribute('#reanalyzeBar button.re-method[data-method="erick"]', "class")
                assert "is-open" in (cls or ""), ("Erick sem destaque no TF", tf, cls)
                body = _bar_post(page, "AAPL", tf, "erick", open_view="erick")
                assert body.get("method") == "erick", ("caiu pra padrao", tf, body)
                assert body.get("compare") in (False, None)
                assert body.get("timeframe") == tf
                assert body.get("date") == TODAY   # reanalisar-hoje preserva método + usa hoje
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_compare_always_padrao_x_erick(live_server):
    """Comparar SEMPRE sai como Padrão × Erick (compare=true), mesmo estando aberto
    num Erick — nunca método × ele-mesmo — em todos os TFs, ação e cripto."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _route_stub(page)
        try:
            page.goto(live_server)
            page.wait_for_selector("#analyzeForm", state="attached")
            for ticker, _ in ASSETS:
                for open_view in ("padrao", "erick"):
                    for tf in TFS:
                        body = _bar_post(page, ticker, tf, "compare", open_view=open_view)
                        assert body.get("compare") is True, ("compare deveria ser true", body)
                        assert body.get("timeframe") == tf
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_launcher_has_no_method_controls(live_server):
    """O launcher ficou limpo: some o checkbox de método/comparar e o botão Atualizar
    separado — método vive só na barra (zero botão morto)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        try:
            page.goto(live_server)
            page.wait_for_selector("#analyzeForm", state="attached")
            assert page.locator("#erickToggle").count() == 0
            assert page.locator("#compareToggle").count() == 0
            assert page.locator(".method-toggle").count() == 0
            assert page.locator("#refreshBtn").count() == 0
            # o form tem só ATIVO + DATA + Analisar
            assert page.locator("#ticker").count() == 1
            assert page.locator("#date").count() == 1
            assert page.locator("#runBtn").count() == 1
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_bar_renders_and_clicks_on_mobile_390(live_server):
    """Sem regressão no mobile 390: a barra renderiza os 3 métodos + 5 TFs e o clique
    dispara o POST correto (layout empilha, mas tudo continua clicável)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 850})
        _route_stub(page)
        try:
            page.goto(live_server)
            page.wait_for_selector("#analyzeForm", state="attached")
            _seed(page, "BTC-USD", "4h", "padrao")
            assert page.locator("#reanalyzeBar button.re-method").count() == 3
            assert page.locator("#reanalyzeBar button.re-tf").count() == 5
            body = _bar_post(page, "BTC-USD", "15m", "erick")
            assert body.get("method") == "erick" and body.get("timeframe") == "15m"
        finally:
            browser.close()

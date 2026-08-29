"""E2E (Playwright) — MATRIZ COMPLETA de método × timeframe × entrada (task 029).

Consolidação confirmada pelo Samyr (opção A): o launcher virou a BARRA ÚNICA no topo
e a barra de reanálise separada DEIXOU DE EXISTIR. Esta suíte prova a matriz inteira no
NOVO layout (o DOM mudou — os seletores .re-* saíram, entraram .lb-*):

  * método volta a viver no launcher (reverte a 021 de propósito): a barra tem
    ‹Padrão · 🧭 Erick · ⚖️ Comparar› como SELETOR (clicar seleciona; Analisar roda);
  * Analisar roda o ticker do input com o método + timeframe escolhidos na barra;
  * ↻ (#rerunBtn) reanalisa o ativo ABERTO hoje preservando o método aberto — absorve
    o antigo "Atualizar"; fica desabilitado enquanto nenhum ativo está aberto;
  * preservação de método por TF (031/037/039): trocar de TF NÃO reseta o método.

Sem rodar LLM: intercepta o POST /api/analyze (Playwright route) e LÊ o corpo, provando
que cada combinação manda método + timeframe + data corretos. Cobre AÇÃO e CRIPTO, as
duas rotas (launcher e ↻), os casos críticos (Erick preserva Erick por TF; Comparar
sempre Padrão × Erick; default Padrão · Diário) e a matriz inteira 3×5.

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


TODAY = "2026-08-25"          # data de "hoje" forçada no cliente (semântica do ↻)
OPEN_DATE = "2026-08-20"      # data da análise ABERTA (≠ hoje) — prova que o ↻ usa hoje
METHODS = ["padrao", "erick", "compare"]
TFS = ["1w", "1d", "4h", "1h", "15m"]
TF_PT = {"1w": "semanal", "1d": "diário", "4h": "4h", "1h": "1h", "15m": "15m"}
ASSETS = [("AAPL", "ação"), ("BTC-USD", "cripto")]

# CI-safe Chromium flags. In the full pytest process, heavy earlier tests (a
# subprocess spawn — e.g. the debate text-sanity validator shelling out to
# aspell — or pandas/BLAS thread pools) leave headless Chromium's GPU/compositor
# path unable to paint: elements resolve in the DOM but never become "visible",
# so clicks time out. Forcing the software path and dropping the /dev/shm
# compositor keeps this browser suite order-independent (it otherwise passes
# alone but flakes in a full run).
_CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--disable-gpu-compositing",
    "--disable-features=VizDisplayCompositor",
]


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


# Semeia o estado "ativo ABERTO" e aponta a barra única pra ele (sem rodar nada).
# openView = método aberto ("padrao"|"erick"|"compare"); syncLaunchBarToOpen espelha
# isso em _barMethod/_barTf e preenche o ticker do input — igual ao render de resultado.
_SEED_OPEN_JS = r"""
(args) => {
  const [ticker, openView, today, openDate, verdictTf] = args;
  document.getElementById('progressPanel').classList.add('hidden');
  document.getElementById('resultPanel').classList.add('hidden');
  document.getElementById('comparePanel').classList.add('hidden');
  _openTicker = ticker;
  _openDate = openDate;
  _todayManaus = today;
  _assetType = /-(USD|USDT)$|^BTC|^ETH/.test(ticker.toUpperCase()) ? 'crypto' : 'stock';
  _timeframes = ['1w','1d','4h','1h','15m'];
  _verdictTf = verdictTf;
  _openView = openView;
  _openMethod = (openView === 'erick') ? 'erick' : 'padrao';
  syncLaunchBarToOpen();
  return true;
}
"""


def _route_stub(page):
    """Intercepta /api/analyze e responde stub (não chega no motor). Também neutraliza
    o polling de /api/status (404 → o cliente ignora sem re-renderizar)."""
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


def _seed_open(page, ticker, open_view, verdict_tf="1d"):
    page.evaluate(_SEED_OPEN_JS, [ticker, open_view, TODAY, OPEN_DATE, verdict_tf])


def _launch_run(page, ticker, method, tf, date=TODAY):
    """Barra única: preenche ativo + data, seleciona TF e método, clica Analisar e
    devolve o corpo do POST /api/analyze interceptado."""
    page.fill("#ticker", ticker)
    page.click(f'#launchTfs button.lb-tf[data-tf="{tf}"]')
    page.click(f'#launchMethods button.lb-method[data-method="{method}"]')
    # data por ÚLTIMO: vence qualquer default assíncrono do applyConfig (/api/config)
    page.fill("#date", date)
    with page.expect_response("**/api/analyze") as ri:
        page.click("#runBtn")
    return json.loads(ri.value.request.post_data)


def _rerun_click(page):
    """Clica o ↻ (reanalisar o ABERTO) e devolve o corpo do POST interceptado."""
    with page.expect_response("**/api/analyze") as ri:
        page.click("#rerunBtn")
    return json.loads(ri.value.request.post_data)


def _expect_body(body, method, tf):
    """Valida o POST de uma combinação (método + TF + data de hoje)."""
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
    """A matriz inteira: método{padrao,erick,compare} × TF{5} × ativo{ação,cripto} pela
    barra única (Analisar), mais o default (Padrão · Diário sem tocar nos seletores).
    Imprime a tabela de resultados."""
    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _route_stub(page)
        try:
            page.goto(live_server)
            page.wait_for_selector("#launchMethods button.lb-method", state="visible")

            # DEFAULT: sem tocar nos seletores → Padrão · Diário, sem compare
            for ticker, asset_pt in ASSETS:
                page.fill("#ticker", ticker)
                page.fill("#date", TODAY)
                with page.expect_response("**/api/analyze") as ri:
                    page.click("#runBtn")
                body = json.loads(ri.value.request.post_data)
                ok = (
                    body.get("method") == "padrao"
                    and body.get("compare") in (False, None)
                    and body.get("timeframe") == "1d"
                )
                rows.append(("default", asset_pt, ticker, "padrao", "diário", ok, body))
                assert ok, ("default padrao/diário", body)

            # MATRIZ: método × TF × ativo pela barra (Analisar)
            for ticker, asset_pt in ASSETS:
                for method in METHODS:
                    for tf in TFS:
                        body = _launch_run(page, ticker, method, tf)
                        _expect_body(body, method, tf)
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
def test_reanalyze_preserves_method_across_tf(live_server):
    """Preservação de método por TF (031/037/039) no novo layout: com um ERICK aberto,
    a barra DESTACA Erick (is-active); trocar de TF NÃO reseta o método; e o ↻ reanalisa
    Erick em TODOS os TFs, sempre na data de HOJE — a classe de bug do 037/039 não volta."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _route_stub(page)
        try:
            page.goto(live_server)
            page.wait_for_selector("#launchMethods button.lb-method", state="visible")
            for tf in TFS:
                _seed_open(page, "AAPL", "erick", verdict_tf="1d")
                # abrir um Erick já deixa o método Erick selecionado e o ↻ habilitado
                cls0 = page.get_attribute('#launchMethods button.lb-method[data-method="erick"]', "class")
                assert "is-active" in (cls0 or ""), ("Erick sem destaque ao abrir", tf, cls0)
                assert page.is_disabled("#rerunBtn") is False, "↻ deveria estar habilitado com ativo aberto"

                # troca de TF: só muda o timeframe — o método Erick PERMANECE selecionado
                page.click(f'#launchTfs button.lb-tf[data-tf="{tf}"]')
                cls = page.get_attribute('#launchMethods button.lb-method[data-method="erick"]', "class")
                assert "is-active" in (cls or ""), ("trocar de TF resetou o método", tf, cls)

                # ↻ reanalisa o ABERTO preservando Erick, no TF escolhido, na data de hoje
                body = _rerun_click(page)
                assert body.get("method") == "erick", ("↻ caiu pra padrao", tf, body)
                assert body.get("compare") in (False, None), ("↻ não é compare", tf, body)
                assert body.get("timeframe") == tf, ("↻ timeframe", tf, body)
                assert body.get("date") == TODAY, ("↻ deveria usar hoje", tf, body)
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_compare_always_padrao_x_erick(live_server):
    """Comparar SEMPRE sai como Padrão × Erick (compare=true), nunca método × ele-mesmo:
    tanto do zero (launcher) quanto selecionando Comparar sobre um Erick aberto, em todos
    os TFs, ação e cripto."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _route_stub(page)
        try:
            page.goto(live_server)
            page.wait_for_selector("#launchMethods button.lb-method", state="visible")
            for ticker, _ in ASSETS:
                for tf in TFS:
                    # do zero, pelo launcher
                    body = _launch_run(page, ticker, "compare", tf)
                    assert body.get("compare") is True, ("compare do zero", body)
                    assert body.get("timeframe") == tf, ("compare timeframe", body)

                    # com um Erick ABERTO: selecionar Comparar sobrepõe o método aberto
                    _seed_open(page, ticker, "erick", verdict_tf="1d")
                    page.click(f'#launchTfs button.lb-tf[data-tf="{tf}"]')
                    page.click('#launchMethods button.lb-method[data-method="compare"]')
                    with page.expect_response("**/api/analyze") as ri:
                        page.click("#runBtn")
                    body2 = json.loads(ri.value.request.post_data)
                    assert body2.get("compare") is True, ("compare sobre erick", body2)
                    assert body2.get("timeframe") == tf, ("compare/erick timeframe", body2)
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_single_bar_layout_no_separate_reanalyze_bar(live_server):
    """UMA barra só: a barra de reanálise separada não existe mais, e o launcher tem
    ativo · data (chip) · tempo (5 TFs) · método (3) · Analisar · ↻. Sem botão morto."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        try:
            page.goto(live_server)
            page.wait_for_selector("#launchMethods button.lb-method", state="visible")

            # a barra de reanálise separada e os controles antigos sumiram
            assert page.locator("#reanalyzeBar").count() == 0
            assert page.locator(".reanalyze-bar").count() == 0
            assert page.locator("#erickToggle").count() == 0
            assert page.locator("#compareToggle").count() == 0
            assert page.locator(".method-toggle").count() == 0
            assert page.locator("#refreshBtn").count() == 0

            # a barra única tem tudo num lugar só
            assert page.locator("#ticker").count() == 1
            assert page.locator("#date").count() == 1
            assert page.locator("#dateChip").count() == 1
            assert page.locator("#runBtn").count() == 1
            assert page.locator("#rerunBtn").count() == 1
            assert page.locator("#launchTfs button.lb-tf").count() == 5
            assert page.locator("#launchMethods button.lb-method").count() == 4  # Padrão · Erick · 1-2-3 · Comparar

            # sem ativo aberto, o ↻ nasce desabilitado (não há o que reanalisar)
            assert page.is_disabled("#rerunBtn") is True

            # o chip de data começa em "Hoje"
            assert (page.text_content("#dateChipLabel") or "").strip() == "Hoje"
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_bar_renders_and_clicks_on_mobile_390(live_server):
    """Mobile 390: a barra única quebra limpa (flex-wrap, sem overflow horizontal),
    renderiza os 4 métodos + 5 TFs, e o clique dispara o POST correto."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
        page = browser.new_page(viewport={"width": 390, "height": 850})
        _route_stub(page)
        try:
            page.goto(live_server)
            page.wait_for_selector("#launchMethods button.lb-method", state="visible")
            assert page.locator("#launchMethods button.lb-method").count() == 4  # Padrão · Erick · 1-2-3 · Comparar
            assert page.locator("#launchTfs button.lb-tf").count() == 5

            # quebra limpa: nada estoura a largura de 390 (sem scroll horizontal)
            no_overflow = page.evaluate(
                "() => document.body.scrollWidth <= window.innerWidth + 1"
            )
            assert no_overflow, "barra estourou a largura no mobile 390"

            body = _launch_run(page, "BTC-USD", "erick", "15m")
            assert body.get("method") == "erick" and body.get("timeframe") == "15m"
        finally:
            browser.close()


# --- o atalho 1-2-3 no ↻: $0 não pode virar análise completa ------------------
# Seed próprio (o _SEED_OPEN_JS achata _openMethod em padrao|erick de propósito,
# porque nasceu antes do setup123 existir).
_SEED_OPEN_SETUP123_JS = r"""
(args) => {
  const [ticker, today, openDate] = args;
  document.getElementById('progressPanel').classList.add('hidden');
  document.getElementById('resultPanel').classList.add('hidden');
  document.getElementById('comparePanel').classList.add('hidden');
  _openTicker = ticker;
  _openDate = openDate;
  _todayManaus = today;
  _assetType = 'stock';
  _timeframes = ['1w','1d','4h','1h','15m'];
  _verdictTf = '1d';
  _openView = 'setup123';
  _openMethod = 'setup123';
  syncLaunchBarToOpen();
  return true;
}
"""


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_rerun_do_setup123_nao_cai_em_padrao(live_server):
    """↻ com o 1-2-3 ABERTO re-roda o ATALHO ($0), nunca uma Padrão completa.

    Regressão medida: ``runReanalyze()`` achatava o método com
    ``method === "erick" ? "erick" : "padrao"``. O setup123 — atalho estrutural
    sem LLM — não é "erick", então caía em "padrao" e subia o pipeline
    multi-agente inteiro: o botão prometia $0 e cobrava uma análise completa.
    Este teste falha com o ternário de volta.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _route_stub(page)
        try:
            page.goto(live_server)
            page.wait_for_selector("#launchMethods button.lb-method", state="visible")
            page.evaluate(_SEED_OPEN_SETUP123_JS, ["AAPL", TODAY, OPEN_DATE])

            body = _rerun_click(page)
            assert body.get("method") == "setup123", (
                "o ↻ do 1-2-3 trocou de método — achatamento voltou e isso custa "
                "uma análise completa de LLM", body)
            assert body.get("compare") in (False, None), ("compare", body)
            assert body.get("date") == TODAY, ("↻ usa hoje", body)
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_launcher_manda_setup123_inteiro(live_server):
    """Pela barra (Analisar), o 1-2-3 selecionado também viaja inteiro."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _route_stub(page)
        try:
            page.goto(live_server)
            page.wait_for_selector("#launchMethods button.lb-method", state="visible")
            body = _launch_run(page, "AAPL", "setup123", "1d")
            assert body.get("method") == "setup123", body
            assert body.get("compare") in (False, None), body
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_rerun_de_setup123_que_FALHOU_continua_setup123(live_server):
    """O caminho do ERRO também não pode escalar o atalho.

    ``renderResult`` no ramo ``status == "error"`` reconstruía o método aberto por
    uma lista que não tinha ``setup123`` — um atalho $0 que falhou reabria como
    "padrao" e o ↻ subia o pipeline multi-agente inteiro. É o MESMO bug do
    achatamento, na porta dos fundos: aqui a run nem chegou a existir direito.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _route_stub(page)
        try:
            page.goto(live_server)
            page.wait_for_selector("#launchMethods button.lb-method", state="visible")
            # Run 1-2-3 que FALHOU, renderizada pelo caminho real (renderResult).
            page.evaluate(
                """(args) => {
                  const [ticker, today, openDate] = args;
                  _todayManaus = today;
                  _timeframes = ['1w','1d','4h','1h','15m'];
                  renderResult({
                    run_id: 'err-1', ticker, date: openDate, status: 'error',
                    method: 'setup123', asset_type: 'stock',
                    result: { error: 'fonte fora do ar' },
                  });
                  return _openView;
                }""",
                ["AAPL", TODAY, OPEN_DATE],
            )
            body = _rerun_click(page)
            assert body.get("method") == "setup123", (
                "o ↻ de um 1-2-3 que falhou virou análise completa — o atalho $0 "
                "cobrando LLM pela porta do erro", body)
            assert body.get("compare") in (False, None), ("compare", body)
        finally:
            browser.close()

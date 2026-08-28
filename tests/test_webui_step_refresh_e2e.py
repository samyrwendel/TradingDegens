"""E2E (Playwright) da RETOMADA VISÍVEL + botão de atualizar etapa (task 002 / DA-062).

Pedido do Samyr, olhando uma análise que continuou de onde parou: *"se está
continuando de onde parou as fases devem aparecer verdes e ter um botão de atualizar
se eu achar que quero dados atualizados em uma delas"*. Prova pela tela:

  1. as etapas que voltaram do checkpoint saem VERDES (``.done``) e marcadas ``♻``,
     não cinza — o trabalho preservado fica óbvio;
  2. cada etapa concluída tem o 🔄, e o clique chama ``/api/run/<id>/refresh-step``
     com o NÓ daquela etapa (re-roda só ela, não a análise inteira);
  3. visitante (não-dono) não vê o 🔄 — a atualização roda pela credencial do dono;
  4. enquanto o servidor pausa→rebobina→re-entra, a tela DIZ "atualizando", em vez de
     piscar "pausada" e sumir com o progresso.

Não roda análise nenhuma: ``page.route`` serve snapshots controlados. Pulado com skip
se o Playwright/Chromium não estiver disponível.
"""

import json
import re
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


RUN_ID = "R-TEST-RETOMADA"
MARKET = "Market Analyst"


@pytest.fixture
def live_server(tmp_path):
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path),
                                         "llm_provider": "openai",
                                         "deep_think_llm": "gpt-5.5",
                                         "quick_think_llm": "gpt-5.4-mini"},
                            store=HistoryStore(tmp_path))
    httpd = make_server("127.0.0.1", 0, runner=runner)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


def _steps(states):
    plan = [
        (MARKET, "Analista de Mercado — preço, múltiplos tempos gráficos, derivativos", "Analistas"),
        ("Sentiment Analyst", "Analista de Sentimento", "Analistas"),
        ("News Analyst", "Analista de Notícias — macro e mercados de previsão", "Analistas"),
        ("Bull Researcher", "Pesquisador do bull case", "Debate"),
        ("Portfolio Manager", "Gestor de Portfólio — veredito final", "Risco"),
    ]
    return [{"node": n, "label": lb, "phase": ph, "state": states.get(n, "pending")}
            for n, lb, ph in plan]


def _snap(states, *, status="running", refreshing=None):
    steps = _steps(states)
    running = [s["label"] for s in steps if s["state"] == "running"]
    return {
        "run_id": RUN_ID, "ticker": "AAPL", "date": "2026-08-27",
        "asset_type": "equity", "status": status, "error": None,
        "verdict_timeframe": "1d", "resumable": True, "cancellable": True,
        "refreshing": refreshing,
        "progress": {
            "percent": 45, "phase": "Retomando",
            "label": running[0] if running else "Retomando de onde parou — 2 etapas preservadas",
            "plan": [{"label": s["label"], "phase": s["phase"]} for s in steps],
            "reached": [{"label": s["label"], "phase": s["phase"]}
                        for s in steps if s["state"] in ("done", "reused", "running")],
            "steps": steps,
        },
        "thinking": [{
            "id": MARKET, "label": "📊 Mercado — preço e tempos gráficos",
            "phase": "Analistas", "debate": False, "order": 0, "len": 26,
            "text": "Leitura preservada do checkpoint.", "provider": None,
            "model": None, "timeframe": "semanal · diário", "reused": True,
        }],
        "cost": {"usd": 0.02}, "elapsed": 31, "result": None,
    }


_RESUMED = {MARKET: "reused", "Sentiment Analyst": "reused", "News Analyst": "running"}


def _install(page, state):
    """Serve /api/config (com o dono), /api/status e captura o POST do refresh."""
    def handler(route):
        url = route.request.url
        def _json(payload, status=200):
            route.fulfill(status=status, content_type="application/json",
                          body=json.dumps(payload))

        if url.endswith("/api/config"):
            # config REAL do servidor, só com o bit de DONO trocado: o app precisa do
            # resto (provedores, modelos) pra subir — um config sintético o quebraria.
            cfg = route.fetch().json()
            cfg["owner"] = state["owner"]
            _json(cfg)
        elif "/api/subscription/status" in url:
            # só-dono no servidor real; sem isto o 403 derruba a sessão de dono no
            # front (handleOwnerSessionLost) e o 🔄 sumiria por um motivo alheio.
            _json({"owner": state["owner"], "connected": False, "providers": {}})
        elif "/refresh-step" in url:
            state["posted"].append(json.loads(route.request.post_data or "{}"))
            state["refreshing"] = {"node": MARKET, "label": "Analista de Mercado"}
            _json({"ok": True, "run_id": RUN_ID, "refreshing": True, "node": MARKET})
        elif "/api/status/" in url or "/api/run/" in url:
            _json(_snap(_RESUMED, refreshing=state["refreshing"]))
        elif url.endswith("/api/history"):
            _json({"runs": [_snap(_RESUMED)]})
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)


def _open(page, live_server, owner=True):
    state = {"owner": owner, "posted": [], "refreshing": None}
    _install(page, state)
    page.goto(live_server)
    page.wait_for_selector("#progressPanel", state="visible")
    page.wait_for_selector("#steps li", state="attached")
    if owner:   # o 🔄 só entra depois que o front sabe que a sessão é do dono
        page.wait_for_selector("#steps .step-refresh", state="attached", timeout=15000)
    return state


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_resumed_stages_are_green_and_offer_refresh(live_server, tmp_path):
    """As etapas preservadas saem verdes + ♻, a corrente fica ativa, e só as
    concluídas ganham o 🔄. Gera o print da DA-062."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1000, "height": 900})
        try:
            _open(page, live_server)
            market = page.locator('#steps li[data-label^="Analista de Mercado"]')
            assert "done" in (market.get_attribute("class") or "")
            assert "reused" in (market.get_attribute("class") or "")
            assert market.locator(".step-reused").count() == 1     # ♻ do reaproveitado
            news = page.locator('#steps li[data-label^="Analista de Notícias"]')
            assert "active" in (news.get_attribute("class") or "")

            # 🔄 só nas CONCLUÍDAS (a que está rodando e as pendentes não têm)
            assert page.locator("#steps li.done .step-refresh").count() == 2
            assert news.locator(".step-refresh").count() == 0
            assert page.locator('#steps li[data-label^="Gestor de Portfólio"]'
                                ' .step-refresh').count() == 0
            # o card de raciocínio preservado se declara reaproveitado
            assert "reaproveitado" in page.locator(".tk-reused").first.inner_text()

            page.screenshot(path=str(tmp_path / "da062-retomada-verde.png"))
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_refresh_click_posts_that_stage_and_shows_progress(live_server, tmp_path):
    """O clique manda o NÓ daquela etapa (não a análise toda) e a tela passa a dizer
    'atualizando' enquanto o servidor pausa→rebobina→re-entra."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1000, "height": 900})
        try:
            state = _open(page, live_server)
            page.locator('#steps li[data-label^="Analista de Mercado"] .step-refresh').click()
            page.wait_for_function("() => document.querySelectorAll('.step-refresh').length >= 0")
            page.wait_for_timeout(300)
            assert state["posted"] == [{"node": MARKET}]

            # o snapshot passa a trazer `refreshing` → o painel segue VIVO e explica
            page.wait_for_function(
                "() => document.getElementById('progressPhase').textContent === 'Atualizando'",
                timeout=8000)
            assert "atualizando" in page.locator("#progressLabel").inner_text()
            assert page.locator("#progressPanel").is_visible()
            page.screenshot(path=str(tmp_path / "da062-atualizando-etapa.png"))
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_visitor_sees_green_but_no_refresh_button(live_server):
    """Visitante vê o que foi preservado (verde/♻) mas NÃO o 🔄 — a atualização roda
    pela credencial do dono."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1000, "height": 900})
        try:
            _open(page, live_server, owner=False)
            assert page.locator("#steps li.reused").count() == 2
            assert page.locator("#steps .step-refresh").count() == 0
        finally:
            browser.close()

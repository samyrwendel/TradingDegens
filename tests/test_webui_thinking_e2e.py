"""E2E (Playwright) do RACIOCÍNIO ao vivo (task 008): durante um run, o painel de
progresso revela o texto dos agentes conforme eles terminam — não só o nome da
fase. Prova o pedido do Samyr ("mostra o pensamento da análise").

Não roda análise real: intercepta /api/history|run|status e serve snapshots com um
campo `thinking` que CRESCE a cada poll (mercado → sentimento → debate). Verifica
que os cards aparecem progressivamente, o debate ganha destaque e o mobile 390 não
tem overflow horizontal. Pulado se Playwright/Chromium não estiver disponível.
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
try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None

RUN_ID = "R-THINK-1"

# Ordem do pipeline (subset): mercado, sentimento, debate bull. len é preenchido.
# provider/model = atribuição por etapa (task 024): run MISTA (analistas openai, juiz
# do debate anthropic) pra provar que cada card mostra o LLM que REALMENTE o rodou.
_THINK = [
    {"id": "Market Analyst", "label": "📊 Mercado — preço e tempos gráficos",
     "phase": "Analistas", "debate": False, "order": 0,
     "provider": "openai", "model": "gpt-5.4-mini",
     "text": "Leitura técnica: o preço testa a média de 21 e o volume confirma o movimento."},
    {"id": "Sentiment Analyst", "label": "💬 Sentimento",
     "phase": "Analistas", "debate": False, "order": 1,
     "provider": "openai", "model": "gpt-5.4-mini",
     "text": "Sentimento levemente positivo nas redes, sem euforia — fluxo comprador moderado."},
    {"id": "Bull Researcher", "label": "🟢 Tese de Alta (bull)",
     "phase": "Debate", "debate": True, "order": 5,
     "provider": "anthropic", "model": "claude-sonnet-5",
     "text": "Bull: momentum e earnings sustentam continuação; alvo na resistência anterior."},
]


def _think(n):
    out = []
    for it in _THINK[:n]:
        c = dict(it)
        c["len"] = len(c["text"])
        out.append(c)
    return out


def _snap(n, status="running"):
    return {
        "run_id": RUN_ID, "ticker": "NVDA", "date": "2026-08-26",
        "asset_type": "equity", "status": status, "error": None,
        "verdict_timeframe": "1d",
        "progress": {"percent": 20 + n * 10, "phase": "Analistas",
                     "label": "Coletando dados…", "plan": [], "reached": []},
        "thinking": _think(n),
        "cost": {"usd": 0.01}, "elapsed": 30, "result": None,
    }


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


def _routes(page, state):
    def handler(route):
        url = route.request.url
        if "/api/status/" in url:
            state["n"] = min(state["n"] + 1, len(_THINK))   # cada poll revela mais
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(_snap(state["n"])))
        elif "/api/run/" in url:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(_snap(max(1, state["n"]))))
        elif url.endswith("/api/history"):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"runs": [_snap(max(1, state["n"]))]}))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_thinking_reports_appear_progressively(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 800})  # mobile 390
        state = {"n": 0}
        _routes(page, state)
        try:
            page.goto(live_server)
            page.wait_for_selector("#thinkingLive .tk-card", state="visible")
            # força mais polls: os cards crescem até o pipeline completo do fixture
            page.wait_for_function(
                "() => document.querySelectorAll('#thinkingLive .tk-card').length >= 3",
                timeout=8000,
            )
            cards = page.query_selector_all("#thinkingLive .tk-card")
            assert len(cards) == 3, len(cards)

            # ordem do pipeline preservada (mercado antes do debate)
            labels = page.evaluate(
                "() => [...document.querySelectorAll('#thinkingLive .tk-sum')].map(s => s.textContent)"
            )
            assert "Mercado" in labels[0] and "bull" in labels[2].lower(), labels

            # o texto do parecer aparece (não só o nome da fase)
            body0 = page.inner_text("#thinkingLive .tk-card:first-child .tk-body")
            assert "média de 21" in body0, body0

            # debate em destaque (classe própria)
            assert page.query_selector("#thinkingLive .tk-card.tk-debate") is not None

            # Atribuição POR ETAPA (task 024): cada card mostra o LLM que o rodou —
            # run MISTA, analistas em openai, juiz do debate em anthropic (o REAL).
            badges = page.evaluate(
                "() => [...document.querySelectorAll('#thinkingLive .tk-card .tk-model')]"
                ".map(s => s.textContent)"
            )
            assert "openai · gpt-5.4-mini" in badges[0], badges
            assert "anthropic · claude-sonnet-5" in badges[2], badges

            # mobile 390: sem overflow horizontal (rola por dentro, não estoura a tela)
            over = page.evaluate("""() => {
              const b = document.getElementById('thinkingLive');
              const body = document.body;
              return { boxOver: b.scrollWidth - b.clientWidth,
                       pageOver: body.scrollWidth - window.innerWidth };
            }""")
            assert over["boxOver"] <= 1, over
            assert over["pageOver"] <= 1, over
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_audit_footer_lists_models_by_step(live_server):
    """Rodapé de auditoria (task 024): a lista "qual LLM fez cada etapa" renderiza o
    provedor/modelo REAL por etapa; run mista mostra openai e anthropic em papéis
    diferentes. Exercita a função de render direto (sem precisar de result completo)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1000, "height": 820})
        page.route(re.compile(r"/api/"), lambda r: r.fulfill(
            status=200, content_type="application/json", body="{}")
            if r.request.url.endswith("/api/history") else r.continue_())
        try:
            page.goto(live_server)
            page.wait_for_selector("#analyzeForm", state="attached")
            html = page.evaluate("""() => auditFooterHtml({
              run_id: "R-AUD-1", collected_at: "2026-08-26T10:00:00-04:00",
              pipeline_version: "9.9",
              models: {provider: "openai", deep_think: "gpt-5.5", quick_think: "gpt-5.4-mini"},
              models_by_step: [
                {node:"Market Analyst", label:"📊 Mercado", phase:"Analistas", order:0,
                 provider:"openai", model:"gpt-5.4-mini"},
                {node:"Portfolio Manager", label:"🛡️ Decisão de Risco", phase:"Risco", order:100,
                 provider:"anthropic", model:"claude-sonnet-5"},
              ],
            }, 123.45);""")
            assert "qual LLM fez cada etapa (2)" in html, html
            assert "openai · gpt-5.4-mini" in html and "anthropic · claude-sonnet-5" in html, html
            assert "Mercado" in html and "Decisão de Risco" in html, html

            # a lista de fato aparece no DOM quando injetada
            container = page.evaluate("""() => {
              const d = document.createElement('div');
              d.innerHTML = auditFooterHtml({run_id:"x", models_by_step:[
                {node:"Trader", label:"🎯 Trader", phase:"Execução", order:80,
                 provider:"google", model:"gemini-2.5-pro"}]}, null);
              document.body.appendChild(d);
              const li = d.querySelectorAll('.audit-steps-list li');
              return {count: li.length, txt: li[0] ? li[0].textContent : ""};
            }""")
            assert container["count"] == 1, container
            assert "google · gemini-2.5-pro" in container["txt"], container
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_thinking_hidden_when_absent(live_server):
    """Sem `thinking` no snapshot (runs antigas/compare), o painel some — sem erro."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1000, "height": 820})

        def handler(route):
            url = route.request.url
            snap = {
                "run_id": RUN_ID, "ticker": "NVDA", "date": "2026-08-26",
                "asset_type": "equity", "status": "running", "error": None,
                "verdict_timeframe": "1d",
                "progress": {"percent": 20, "phase": "Analistas", "label": "…",
                             "plan": [], "reached": []},
                "cost": {"usd": 0.01}, "elapsed": 10, "result": None,
            }  # sem chave "thinking"
            if "/api/status/" in url or "/api/run/" in url:
                route.fulfill(status=200, content_type="application/json", body=json.dumps(snap))
            elif url.endswith("/api/history"):
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"runs": [snap]}))
            else:
                route.continue_()
        page.route(re.compile(r"/api/"), handler)
        try:
            page.goto(live_server)
            page.wait_for_selector("#progressPanel", state="visible")
            page.wait_for_timeout(300)
            assert page.is_hidden("#thinkingLive")
        finally:
            browser.close()

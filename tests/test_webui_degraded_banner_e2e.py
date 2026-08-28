"""E2E (Playwright) do banner de fonte degradada (task 20260828-003).

Bug do Samyr (print de 27/08 21:37, run do AAOI): o banner amarelo dizia literalmente
"Análise feita SEM a fonte: **fonte**" — sem nome de fonte e com a lista de motivos
vazia. Causa raiz: o guard de sanidade do debate empurrava uma STRING no canal
``degraded_sources``, cujo contrato é um dict ``{label, report_key, reason, kind}``.

Aqui a prova é pela TELA, não pelo dict: o registro do AAOI é regravado com a string
legada exata, a UI reabre pelo ``/api/run/<id>`` real e o banner tem que NOMEAR a
fonte e MOSTRAR o motivo. Cobre também a separação das duas semânticas (fonte ausente
vs. texto sinalizado) e a manutenção de "fonte" como último recurso.

Pulado com skip se o Playwright/Chromium não estiver disponível no ambiente.
"""

import os
import threading

import pytest

import tradingagents.webui.runner as runner_module
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

# a string EXATA gravada no run do print (results/webui/runs/20260827-213215-6ebd31.json)
LEGACY_NOTE = (
    "Bear Researcher (texto suspeito: severity=suspect invented=20 (1.73%) "
    "[DILUÇÃO, dilução, DILUÇÃO, Dilução])"
)

_EMPTY_REPORTS = {
    "market_report": "", "sentiment_report": "", "news_report": "",
    "fundamentals_report": "", "erick_report": "", "bull": "", "bear": "",
    "research_manager": "", "investment_plan": "", "trader_plan": "",
    "risk_decision": "", "axes": {}, "audit": {}, "fallbacks": [],
}


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setattr(runner_module, "fetch_price_chart",
                        lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_actionable_plan",
                        lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_derivatives_report", lambda t, d: "")
    yield
    os.environ.pop("TRADINGDEGENS_OWNER_TOKEN", None)


def _serve(tmp_path, store):
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path),
                                         "llm_provider": "openai"},
                            store=store)
    httpd = make_server("127.0.0.1", 0, runner=runner)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


@pytest.fixture
def live_server(tmp_path):
    """Histórico VAZIO — a UI não auto-abre nada e o teste controla o render."""
    yield from _serve(tmp_path, HistoryStore(tmp_path))


@pytest.fixture
def legacy_server(tmp_path):
    """Histórico com o registro do print: a nota legada em texto solto."""
    store = HistoryStore(tmp_path)
    store.save({
        "run_id": "AAOI-legacy", "ticker": "AAOI", "date": "2026-08-27",
        "asset_type": "stock", "status": "done", "error": None, "error_code": None,
        "verdict": "Sell", "verdict_timeframe": "1d", "method": "padrao",
        "cost": {"usd": 0.0}, "elapsed": 0,
        "result": dict(_EMPTY_REPORTS, verdict="Sell", degraded=[LEGACY_NOTE]),
    })
    yield from _serve(tmp_path, store)


_RESULT_FIELDS = """
      market_report: '', sentiment_report: '', news_report: '',
      fundamentals_report: '', erick_report: '', bull: '', bear: '',
      research_manager: '', investment_plan: '', trader_plan: '',
      risk_decision: '', axes: {}, audit: {}, fallbacks: []
"""


def _snap(degraded):
    """renderResult() com um resultado mínimo e a lista `degraded` sob teste."""
    return ("() => renderResult({"
            "  status: 'done', run_id: 'RID-dg', ticker: 'AAOI', date: '2026-08-27',"
            "  asset_type: 'stock', elapsed: 0, cost: { usd: 0 },"
            "  result: { verdict: 'Sell', " + _RESULT_FIELDS +
            ", degraded: " + degraded + " } })")


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_stored_legacy_note_reopens_with_the_source_named(legacy_server, tmp_path):
    """O run do print reabre pela borda HTTP real — e nomeia a fonte."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(legacy_server)
            page.evaluate("() => openRun('AAOI-legacy')")
            page.wait_for_selector("#degradedBanner:not(.hidden)")
            banner = page.inner_text("#degradedBanner")

            # o defeito exato do print: nome vira o placeholder e o motivo some
            assert "SEM a fonte: fonte" not in banner
            assert "Bear Researcher" in banner
            # o motivo aparece na lista (o <ul> estava vazio antes)
            assert page.locator("#degradedBanner .dg-list li").count() == 1
            assert "invented=20" in banner

            page.screenshot(path=str(tmp_path / "degraded-legacy.png"))
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_missing_and_suspect_are_told_apart(live_server):
    """Fonte AUSENTE e turno com texto sinalizado dizem coisas opostas.

    Antes, tudo caía na frase "análise feita SEM" — falso pro turno sinalizado, que
    ESTÁ na leitura (o guard entrega a geração mais limpa das duas).
    """
    degraded = (
        "[{label: 'News Analyst', report_key: 'news_report', "
        "reason: 'RuntimeError: timeout', kind: 'missing'},"
        " {label: 'Bear Researcher', report_key: 'investment_debate_state', "
        "reason: 'texto suspeito — 20 termo(s) fora do léxico', kind: 'suspect'}]"
    )
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(live_server)
            page.evaluate(_snap(degraded))
            page.wait_for_selector("#degradedBanner:not(.hidden)")
            banner = page.inner_text("#degradedBanner")

            heads = page.locator("#degradedBanner .dg-head")
            assert heads.count() == 2
            assert "SEM" in heads.nth(0).inner_text()
            assert "News Analyst" in heads.nth(0).inner_text()
            assert "News Analyst" not in heads.nth(1).inner_text()
            assert "Texto sinalizado" in heads.nth(1).inner_text()
            assert "Bear Researcher" in heads.nth(1).inner_text()
            # a fonte sinalizada NÃO é anunciada como ausente
            assert "SEM a fonte: Bear Researcher" not in banner
            # os dois motivos entram na lista
            assert page.locator("#degradedBanner .dg-list li").count() == 2
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_placeholder_survives_only_as_last_resort(live_server):
    """Entrada sem nenhum nome recuperável ainda cai em 'fonte' — mas com o motivo."""
    degraded = "[{label: '', report_key: '', reason: 'origem não registrada', kind: 'missing'}]"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(live_server)
            page.evaluate(_snap(degraded))
            page.wait_for_selector("#degradedBanner:not(.hidden)")
            banner = page.inner_text("#degradedBanner")
            assert "fonte: fonte" in banner
            assert "origem não registrada" in banner
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_clean_run_has_no_banner(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(live_server)
            page.evaluate(_snap("[]"))
            page.wait_for_selector("#resultPanel:not(.hidden)")
            assert page.locator("#degradedBanner.hidden").count() == 1
        finally:
            browser.close()

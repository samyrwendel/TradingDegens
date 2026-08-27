"""E2E (Playwright) do erro PARCIAL — preserva as etapas concluídas (task 015).

Bug do Samyr: 'se der erro, tem que parar no que deu erro e não zerar toda a análise'.
Antes, um erro no meio mostrava tela vazia de ERRO e descartava o trabalho. Agora a UI
mostra as etapas já concluídas (analistas + debate) + um banner 'parou nesta etapa' com
o caminho pra continuar (escalar/retomar). E quando NADA concluiu, segue o erro honesto.

Pulado com skip se o Playwright/Chromium não estiver disponível no ambiente.
"""

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


@pytest.fixture
def live_server(tmp_path):
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path),
                                         "llm_provider": "openai"},
                            store=HistoryStore(tmp_path))
    httpd = make_server("127.0.0.1", 0, runner=runner)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


_PARTIAL_SNAP = """() => renderResult({
  status: 'error', run_id: 'RID-partial', ticker: 'AAPL', date: '2026-08-22',
  asset_type: 'stock', error: 'Limite de requisições (429) no juiz — tente de novo.',
  error_code: 'rate_limit', elapsed: 132, cost: { usd: 0.0123 }, resumable: false,
  result: {
    partial: true, failed_step: { label: 'Juiz do Debate' },
    market_report: 'Leitura técnica: tendência de alta no diário, acima da MMS200.',
    bull: 'Tese de alta: momentum forte e volume crescente.',
    bear: 'Tese de baixa: valuation esticado após a corrida.',
    research_manager: '', investment_plan: '', trader_plan: '', sentiment_report: '',
    news_report: '', fundamentals_report: '', erick_report: '', risk_decision: '',
    axes: {}, audit: {}, fallbacks: [], degraded: []
  }
})"""

_EMPTY_ERROR_SNAP = """() => renderResult({
  status: 'error', run_id: 'RID-empty', ticker: 'AAPL', date: '2026-08-22',
  asset_type: 'stock', error: 'Chave inválida.', error_code: 'invalid_key',
  elapsed: 3, cost: { usd: 0 }, result: null
})"""


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_partial_error_preserves_completed_steps(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(live_server)
            page.evaluate(_PARTIAL_SNAP)
            page.wait_for_selector("#resultPanel:not(.hidden)")
            # não é tela vazia: veredito vira PARCIAL, não "ERRO"
            assert "PARCIAL" in page.inner_text("#verdictBadge")
            # banner (visível) nomeia a etapa que falhou e diz que o resto foi preservado
            banner = page.inner_text("#sections")
            assert "Parou em" in banner and "Juiz do Debate" in banner
            assert "preservad" in banner.lower()
            # as etapas concluídas estão no DOM (seções <details> colapsadas → text_content):
            # título + corpo preservados, não descartados
            sections_all = page.text_content("#sections")
            assert "Mercado" in sections_all and "tendência de alta" in sections_all
            # as teses de alta/baixa concluídas ficam no DOM (thesis colapsada)
            assert "momentum forte" in page.text_content("#bull")
            assert "valuation esticado" in page.text_content("#bear")
            # há o caminho de continuar (escalar) — o box de escalonamento renderizou
            # (só aparece pro dono; aqui _isOwner=false, então checamos ao menos o banner)
            assert page.query_selector("#sections .error-card.partial") is not None
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_empty_error_still_shows_honest_error_card(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(live_server)
            page.evaluate(_EMPTY_ERROR_SNAP)
            page.wait_for_selector("#resultPanel:not(.hidden)")
            # nada concluído → erro honesto (não finge parcial)
            assert "ERRO" in page.inner_text("#verdictBadge")
            assert page.query_selector("#sections .error-card") is not None
            assert page.query_selector("#sections .error-card.partial") is None
        finally:
            browser.close()

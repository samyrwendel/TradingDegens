"""A LINHA CLICADA É A LEITURA MOSTRADA, mesmo com a preferência da sessão contra
ela (DA-151 — corrige a DA-143).

O Samyr clicou no "D" verde da linha **123** (Setup123) no card do AAOI e a tela
abriu com o **Storm123** desenhado: o cabeçalho dizia "Setup123" (o pedido chegou
certo — ver ``test_clicar_no_marcador_abre_o_ativo_NAQUELE_frame_e_NAQUELE_metodo``
em ``test_webui_faixa_de_frames_e2e.py``, que já prova que o POST leva o método da
linha), mas em LEITURAS o Storm123 estava aceso e o gráfico traçava os níveis dele.

**A causa não é a navegação, é o que acontece DEPOIS que o resultado chega.**
``plano_com_storm`` (runner.py) sempre anexa a leitura do Storm ao lado da do
plano — de propósito, desde a DA-088, pra ela poder ser LIGADA sem re-rodar nada.
Isso significa que ``actionable.storm`` existe e tem o que desenhar mesmo numa run
aberta como ``setup123``. Quem decide qual das duas aparece é ``iniciaCamadas``, e
ela dá prioridade à preferência PEGAJOSA da sessão (``_camadasTocado`` /
``sessionStorage``, DA-143: "depois do primeiro toque, a escolha dele vale nas
próximas análises") por cima do método que acabou de ser pedido. Se o Samyr tinha
tocado no seletor "Storm123" numa análise anterior — de QUALQUER ativo —, a
preferência gravada sobrevive à navegação da faixa e desenha Storm de novo, não
importa que linha ele tenha clicado agora.

**O dente**: grava a preferência pegajosa pro lado ERRADO antes de navegar, clica
na linha do método OPOSTO via a faixa, e prova que a leitura desenhada é a da
LINHA CLICADA — não a da preferência antiga, mesmo quando a leitura "vencedora"
teria sido a mais forte das duas.
"""

import json
import re

import pytest

from tests.test_webui_frame_e_cor_e2e import DESKTOP, sobe_servidor
from tests.test_webui_um_grafico_um_metodo_e2e import _CHART, _PLANO, _STORM

pytestmark = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None


@pytest.fixture
def base(tmp_path):
    yield from sobe_servidor(tmp_path)


_TICKER = "AAOI"


def _linha(frame):
    return {"frame": frame, "estado": "em_gatilho", "direction": "venda",
            "price": 100.0, "trigger": 101.0, "dist_pct": 0.01,
            "pattern_state": "formando",
            "storm": {"frame": frame, "estado": "em_gatilho", "direction": "venda",
                      "price": 100.0, "trigger": 101.0, "dist_pct": 0.01,
                      "pattern_state": "formando"}}


_SCAN = {"date": "2026-08-31", "frames": ["1d", "4h", "1h"],
         "gerado_em": "2026-08-31T22:00:00-04:00",
         "ativos": [{"ticker": _TICKER, "melhor": _linha("1d"),
                     "frames": [_linha(f) for f in ("1d", "4h", "1h")]}],
         "oportunidades": [],
         "resumo": {"em_gatilho": 1}}

_HIST = [{"run_id": "R-hist", "ticker": _TICKER, "date": "2026-08-31",
          "asset_type": "stock", "status": "done", "verdict": None,
          "elapsed": 1, "cost": {"usd": 0.0}, "finished_at": "2026-08-31 20:00"}]


def _snap(run_id, metodo):
    # `plano_com_storm` (runner.py) SEMPRE anexa a leitura do Storm, não importa o
    # método pedido (DA-088) — é essa a condição real que faz o bug possível: as
    # DUAS leituras existem e têm o que desenhar, e só a preferência de tela decide
    # qual aparece.
    r = {"verdict": None, "final_decision": "", "timeframe": "1d",
         "as_of_price": 465.58, "actionable": {**_PLANO, "storm": _STORM},
         "price_chart": _CHART, "degraded": [],
         "bull": "", "bear": "", "research_manager": "", "investment_plan": "",
         "trader_plan": "", "risk_decision": "", "market_report": "",
         "sentiment_report": "", "news_report": "", "fundamentals_report": "",
         "erick_report": "", "drop_nature": {}, "derivatives_report": "",
         "setup123": metodo == "setup123", "storm123": metodo == "storm123"}
    return {"run_id": run_id, "ticker": _TICKER, "date": "2026-08-31",
            "asset_type": "stock", "status": "done", "elapsed": 1,
            "cost": {"usd": 0.0}, "verdict": None, "verdict_timeframe": "1d",
            "result": r}


def _abre(page, base_url, *, sticky):
    """Abre a lista com o scan pronto e a preferência de leitura PEGAJOSA já
    gravada em `sticky` — o estado que uma análise anterior, de QUALQUER ativo,
    deixou na sessão."""
    runs = {}

    def handler(route):
        u = route.request.url
        if route.request.method == "POST" and "/api/analyze" in u:
            body = json.loads(route.request.post_data or "{}")
            metodo = body.get("method")
            rid = f"R-{metodo}"
            runs[rid] = _snap(rid, metodo)
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"run_id": rid, "run_token": "tok"}))
        elif "/api/status/" in u or re.search(r"/api/run/[^/]+$", u):
            rid = u.rsplit("/", 1)[-1]
            snap = runs.get(rid)
            if snap is None:
                route.fulfill(status=404, content_type="application/json", body="{}")
            else:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps(snap))
        elif "/api/scan/salvo" in u or "/api/scan" in u:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(_SCAN))
        elif "/api/history" in u:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"runs": _HIST}))
        elif "/api/watchlist" in u:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"tickers": [{"ticker": _TICKER}]}))
        else:
            route.continue_()

    page.route(re.compile(r"/api/"), handler)
    # A preferência PEGAJOSA de uma análise anterior — de qualquer ativo — já
    # gravada antes de a página carregar, exatamente como `salvaCamadas` a deixa.
    page.add_init_script(
        "sessionStorage.setItem('td.camadas.v1', JSON.stringify("
        f"{{tocado: true, camadas: {json.dumps(sticky)}}}))")
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_selector(".history li", state="attached", timeout=15000)
    page.wait_for_selector(".h-faixa-linha", state="attached", timeout=15000)


def _clica_na_linha(page, rotulo, frame_curto):
    """Clica no marcador `frame_curto` (ex.: "D") da linha cujo `.fx-met` é
    `rotulo` ("123" ou "storm") — a mesma navegação que a faixa oferece de
    verdade, sem atalho por `abreDaFaixa` direto: é o CLIQUE que se testa."""
    page.evaluate("""([rotulo, curto]) => {
      const li = document.querySelector('.history li');
      const linha = [...li.querySelectorAll('.h-faixa-linha')]
        .find((l) => l.querySelector('.fx-met').textContent === rotulo);
      const marca = linha && [...linha.querySelectorAll('[data-faixa-go]')]
        .find((m) => m.textContent === curto);
      if (!marca) throw new Error('marcador não achado: ' + rotulo + '/' + curto);
      marca.click();
    }""", [rotulo, frame_curto])


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("frame_curto,frame_interno", [
    ("D", "1d"), ("4h", "4h"), ("1h", "1h"),
])
def test_clicar_no_123_abre_123_MESMO_com_a_sessao_presa_no_storm(base, frame_curto, frame_interno):
    """O DENTE central: a sessão está com Storm123 aceso de uma análise anterior
    (a leitura "mais forte"/última tocada); clicar na linha do 123 tem de trocar
    pra 123 mesmo assim."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, sticky=["storm"])
        _clica_na_linha(page, "123", frame_curto)
        page.wait_for_function("() => typeof _openMethod !== 'undefined' && _openMethod === 'setup123'",
                                timeout=15000)
        estado = page.evaluate(
            "() => ({metodo: _openMethod, plano: camadaVisivel('plano'), storm: camadaVisivel('storm')})")
        assert estado["metodo"] == "setup123", estado
        assert estado["plano"] is True, ("123 clicado mas a leitura do plano não acendeu", estado)
        assert estado["storm"] is False, ("123 clicado mas o Storm continuou na tela", estado)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("frame_curto,frame_interno", [
    ("D", "1d"), ("4h", "4h"), ("1h", "1h"),
])
def test_clicar_no_storm_abre_storm_MESMO_com_a_sessao_presa_no_123(base, frame_curto, frame_interno):
    """O espelho: sessão presa no 123, clique na linha do Storm tem de abrir Storm."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, sticky=["plano"])
        _clica_na_linha(page, "storm", frame_curto)
        page.wait_for_function("() => typeof _openMethod !== 'undefined' && _openMethod === 'storm123'",
                                timeout=15000)
        estado = page.evaluate(
            "() => ({metodo: _openMethod, plano: camadaVisivel('plano'), storm: camadaVisivel('storm')})")
        assert estado["metodo"] == "storm123", estado
        assert estado["storm"] is True, ("storm clicado mas a leitura do Storm não acendeu", estado)
        assert estado["plano"] is False, ("storm clicado mas o plano continuou na tela", estado)
        browser.close()

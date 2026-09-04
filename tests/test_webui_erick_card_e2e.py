"""O card MÉTODO ERICK nas leituras de fundo (task 20260904-003).

Samyr (03/09 19:55): *"no diário só aparecem Setup123 e Recuo à média; não deveria
ter um card priorizando a análise do Erick?"*. O card aparece PRIMEIRO nas leituras
do 1w/1d, com o estado/decisão/EMAs do método; nos frames menores (4h/1h) ele fica
FORA DO FRAME (só o cabeçalho, sem card), porque o método decide no fundo diário/
semanal. O dado do card vem de ``erick_reading`` no ``actionable`` — o front só
renderiza; a decisão é a de ``erick_reading_dict`` (soldada em test_erick_reading_dict).
"""

import json
import re

import pytest

from tests.test_webui_frame_e_cor_e2e import DESKTOP, sobe_servidor

pytestmark = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None


@pytest.fixture
def base(tmp_path):
    yield from sobe_servidor(tmp_path)


_ERICK = {
    "disponivel": True, "fora_do_frame": False, "frame": "4h", "frame_label": "4 horas (intradiário)",
    "degraded": False, "estado": "AGIR", "acao": "AGIR",
    "entrada": "preço recuou até a média agora (EMA 8 99,00 · EMA 21 98,00) — é o ponto de entrada no recuo",
    "saida": "realizar na resistência acima (topo anterior: 110,00) — pegar a maior parte e sair antes da reversão",
    "peso": "meia posição", "peso_racional": "monta fracionado no toque da média",
    "trend": "alta", "trend_pt": "alta", "close": 99.2, "e8": 99.0, "e21": 98.0, "e50": 95.0,
    "emas": {"8": 99.0, "21": 98.0, "50": 95.0}, "at_media": True, "extended": False, "below": False,
    "drop": {"classification": None}, "drop_line": "",
    "gate": False, "gate_faltam": [], "tese": {}, "earnings": "sem balanço na janela",
    "ausentes": [], "rsi_divergence": {"measured": True, "kind": "bullish", "detail": "fundo do preço caiu"},
    "pattern_line": "1-2-3 de compra em formação", "levels_line": "invalida 96,00 · alvo 110,00",
    "fine_timing": "", "estado_note": "",
}


def _snap(erick):
    plano = {
        "symbol": "AVGO", "price": 99.2, "timeframe": "diário (referência)", "horizon": "dias",
        "setup_state": "aguardar_rompimento", "setup_source": "123",
        "pattern": None, "buy_zone": None, "realize_zone": None, "pullback_zone": None,
        "invalidation": None, "stop": None, "target": None, "risk_reward": None,
        "erick_reading": erick,
    }
    r = {"verdict": "Hold", "final_decision": "", "timeframe": "1d", "as_of_price": 99.2,
         "actionable": plano, "live_price": None, "price_chart": None, "degraded": [],
         "bull": "", "bear": "", "research_manager": "", "investment_plan": "", "trader_plan": "",
         "risk_decision": "", "market_report": "", "sentiment_report": "", "news_report": "",
         "fundamentals_report": "", "erick_report": "", "drop_nature": {}, "derivatives_report": ""}
    return {"run_id": "R-ERK", "ticker": "AVGO", "date": "2026-09-03", "asset_type": "stock",
            "status": "done", "elapsed": 1, "cost": {"usd": 0.0}, "verdict": "Hold",
            "verdict_timeframe": "1d", "result": r}


def _abre(page, base_url, erick):
    snap = _snap(erick)

    def handler(route):
        u = route.request.url
        if "/api/execucao" in u:
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"card": None}))
        elif "/api/status/" in u or re.search(r"/api/run/[^/]+$", u):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(snap))
        else:
            route.continue_()

    page.route(re.compile(r"/api/"), handler)
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_function("() => typeof watchRun === 'function'")
    page.evaluate("() => watchRun('R-ERK')")
    page.wait_for_selector("#setupCards:not(.hidden)")


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_card_do_Metodo_Erick_aparece_PRIMEIRO_no_diario(base):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, _ERICK)
        # existe e é o PRIMEIRO card
        ordem = page.evaluate(
            "() => [...document.querySelectorAll('#setupCards .setup-card')].map(c => c.className)")
        assert ordem and "sc-erick" in ordem[0], ("Erick não é o primeiro card", ordem)
        txt = page.evaluate(
            "() => document.querySelector('.setup-card.sc-erick').innerText.replace(/\\s+/g,' ')")
        assert "Método Erick" in txt, txt
        assert "AGIR" in txt and "meia posição" in txt, txt
        assert "EMA 8" in txt and "99,00" in txt, ("alinhamento das EMAs", txt)
        assert "sequência de 3 candles (Erick)" in txt, ("1-2-3 do Erick separado do pivô", txt)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_fora_do_frame_nao_desenha_card_mas_avisa(base):
    fora = {"disponivel": False, "fora_do_frame": True, "frame": "4h"}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, fora)
        assert page.query_selector(".setup-card.sc-erick") is None, "não devia haver card no 4h"
        nota = page.query_selector(".sc-erick-fora")
        assert nota is not None, "faltou o cabeçalho 'fora do frame'"
        t = nota.inner_text()
        assert "Método Erick" in t and "fora do frame" in t, t
        browser.close()

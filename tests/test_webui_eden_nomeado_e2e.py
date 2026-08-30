"""ÉDEN NOMEADO EM TODA SUPERFÍCIE (task 20260830-036).

*"nos cards de texto onde usamos Éden, identifica Éden de Alta e de Baixa na menção."*

O card mostrava "MME 8 × MME 80" com os dois valores e nunca dizia de QUE Éden se
tratava. Aqui se mede o outro lado do pedido, que é o que impede a correção de apodrecer:
**o nome sai de um lugar só**. Cada superfície (card, selo, etiqueta na vela, legenda,
nota do gráfico, chip de R:R) recebe o rótulo PRONTO no payload e o exibe — nenhuma
escreve o seu. Foi escrevendo rótulo à mão em cada superfície que a tela ganhou três
jeitos de dizer timeframe (DA-095).
"""

import json
import re

import pytest

from tests.test_webui_frame_e_cor_e2e import DESKTOP, TELEFONE, sobe_servidor
from tests.test_webui_um_grafico_um_metodo_e2e import _CHART, _PLANO, _STORM
from tradingagents.dataflows import price_structure as ps

pytestmark = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None


@pytest.fixture
def base(tmp_path):
    yield from sobe_servidor(tmp_path)


def _eden(estado, **extra):
    """O Éden como o backend o monta — com os campos de NOME."""
    return {"disponivel": estado != "indisponivel", "ema_rapida": 468.0,
            "ema_lenta": 492.0, "preco": 465.58,
            "alinhado": estado in ("alta", "baixa"),
            "direcao": {"alta": "compra", "baixa": "venda"}.get(estado),
            "zona_neutra": estado in ("armadilha", "neutra"),
            "armadilha": estado == "armadilha",
            "motivo": "MME 8 abaixo da MME 80 e preço abaixo das duas",
            **ps._eden_nomes(estado), **extra}


def _storm(estado, *, opera=True, veto=None):
    return {**_STORM, "eden": _eden(estado), "opera": opera, "veto": veto,
            "motivo": "…"}


def _abre(page, base_url, storm):
    r = {"verdict": None, "final_decision": "", "timeframe": "1d",
         "as_of_price": 465.58, "actionable": {**_PLANO, "storm": storm},
         "live_price": None, "price_chart": _CHART, "degraded": [],
         "bull": "", "bear": "", "research_manager": "", "investment_plan": "",
         "trader_plan": "", "risk_decision": "", "market_report": "",
         "sentiment_report": "", "news_report": "", "fundamentals_report": "",
         "erick_report": "", "drop_nature": {}, "derivatives_report": "",
         "setup123": False, "storm123": True}
    snap = {"run_id": "R-036", "ticker": "MSFT", "date": "2026-08-29",
            "asset_type": "stock", "status": "done", "elapsed": 2,
            "cost": {"usd": 0.0}, "verdict": None, "verdict_timeframe": "1d",
            "result": r}

    def handler(route):
        url = route.request.url
        if "/api/execucao" in url:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"card": None}))
        elif "/api/status/" in url or re.search(r"/api/run/[^/]+$", url):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(snap))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)
    page.goto(base_url, wait_until="networkidle")
    page.evaluate("() => watchRun('R-036')")
    page.wait_for_selector("#setupCards:not(.hidden)")
    page.wait_for_timeout(300)


_SUPERFICIES = """() => {
  const cv = document.getElementById('priceChart');
  return {
    card: document.getElementById('setupCards').innerText.replace(/\\s+/g, ' '),
    titles: [...document.querySelectorAll('#setupCards [title]')]
              .map(e => e.getAttribute('title')).join(' | '),
    rotulos: JSON.parse(cv.dataset.rotulos123 || '[]').join(' | '),
    legenda: document.getElementById('chartLegend').innerText.replace(/\\s+/g, ' '),
    nota: document.getElementById('chartNote').innerText.replace(/\\s+/g, ' '),
    chip: cv.dataset.rr || '',
  };
}"""


# ─────────────── o card diz QUAL Éden, em vez de só como ele é medido ─────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [DESKTOP, TELEFONE], ids=["desktop", "telefone"])
@pytest.mark.parametrize("estado,rotulo", [("alta", "Éden de Alta"),
                                           ("baixa", "Éden de Baixa")])
def test_o_card_nomeia_o_eden_por_direcao(base, viewport, estado, rotulo):
    """DENTE: "MME 8 × MME 80 · 468,00 × 492,00" dizia COMO o filtro é medido e nunca
    QUAL foi o resultado — que é a única coisa que decide se o setup opera."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre(page, base, _storm(estado))
        m = page.evaluate(_SUPERFICIES)
        assert rotulo in m["card"], m["card"]
        # as duas médias NÃO se perdem: é com elas que se confere o veto
        assert "468,00 × 492,00" in m["card"], m["card"]
        assert "MME 8 × MME 80" in m["card"], m["card"]
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_selo_do_card_tambem_diz_qual(base):
    """"filtro Éden · opera" era uma menção sem direção no lugar mais lido do card."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, _storm("alta"))
        card = page.evaluate(_SUPERFICIES)["card"]
        assert "Éden de Alta" in card and "filtro Éden" not in card, card
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_equivalencia_com_a_doutrina_vai_no_title(base):
    """Quem leu "Éden de compra" no material do Stormer precisa reconhecer o que está
    na tela — sem que o rótulo volte a ser o do sinal."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, _storm("baixa"))
        m = page.evaluate(_SUPERFICIES)
        assert "na doutrina do Stormer, Éden de venda" in m["titles"], m["titles"]
        assert "Éden de venda" not in m["card"], ("no corpo, só Alta/Baixa", m["card"])
        browser.close()


# ─────────── os estados SEM direção têm nome próprio, nunca um genérico ───────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("estado,trecho", [
    ("armadilha", "ARMADILHA"),
    ("neutra", "ZONA NEUTRA"),
    ("desalinhado", "sem Éden"),
    ("indisponivel", "Éden indisponível"),
])
def test_cada_estado_sem_direcao_se_nomeia(base, estado, trecho):
    """"sem Éden" pra tudo apagava a ARMADILHA — repique dentro de tendência contrária,
    o caso mais caro da lista — dentro do caso mais banal."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, _storm(estado, opera=False, veto="não opera"))
        card = page.evaluate(_SUPERFICIES)["card"]
        assert trecho in card, (estado, card)
        browser.close()


# ─────────────── o gráfico usa o MESMO nome (forma curta) ────────────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [DESKTOP, TELEFONE], ids=["desktop", "telefone"])
def test_a_vela_a_legenda_a_nota_e_o_chip_dizem_QUAL_eden_vetou(base, viewport):
    """"não opera — armadilha" e "não opera — Éden de Baixa" são vetos diferentes, e só
    o segundo se resolve esperando. A etiqueta na vela leva a forma CURTA: ali o espaço
    é a largura de um candle (DA-101 — encolhe, não infla)."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre(page, base, _storm("baixa", opera=False,
                                 veto="padrão de venda contra Éden de Baixa"))
        m = page.evaluate(_SUPERFICIES)
        assert "não opera — Éden de Baixa" in m["rotulos"], ("na VELA", m["rotulos"])
        assert "Éden de Baixa" in m["legenda"], ("na LEGENDA", m["legenda"])
        assert "Éden de Baixa" in m["nota"], ("na NOTA", m["nota"])
        assert "Éden de Baixa" in m["chip"], ("no CHIP de R:R", m["chip"])
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_nenhuma_superficie_escreve_o_proprio_nome(base):
    """O ponto que impede a correção de apodrecer: TODAS as superfícies mostram
    exatamente o rótulo que o produtor mandou. Uma escrevendo o seu é a DA-095 de novo."""
    canonico = ps._EDEN_ROTULO["armadilha"]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, _storm("armadilha", opera=False, veto="armadilha na zona neutra"))
        m = page.evaluate(_SUPERFICIES)
        assert canonico[0] in m["card"], (canonico[0], m["card"])
        for onde in ("rotulos", "legenda", "nota", "chip"):
            texto = m[onde]
            if "Éden" in texto or "armadilha" in texto:
                assert canonico[1] in texto or canonico[0] in texto, (onde, texto)
        browser.close()

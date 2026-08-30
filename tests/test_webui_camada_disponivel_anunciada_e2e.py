"""LEITURA DISPONÍVEL E DESLIGADA SE ANUNCIA (task 20260830-033).

*"eu não vi nenhum desenho do storm123 nos gráficos que analisei."*

Não era bug do desenho — era efeito colateral das camadas. Duas coisas somadas:

  1. o plano de uma run Padrão/Erick **não trazia** a leitura do Storm, então nem o
     botão da camada existia (o Storm não estava desligado: estava ausente);
  2. o gráfico passou a desenhar só a leitura do método aberto (DA-088) — correto, foi
     o que ele pediu — e nada na tela dizia que a outra existia.

Trocamos "mistura tudo" por "sumiu e não avisou", que é pior: o primeiro ele consegue
desfazer. O que estes testes travam é o estado **disponível-e-desligado** dito em voz
alta, no gráfico, com o clique que liga.
"""

import json
import re

import pytest

from tests.test_webui_frame_e_cor_e2e import DESKTOP, TELEFONE, sobe_servidor
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


def _snap(metodo, *, com_storm=True):
    """Uma análise de `metodo` — com a leitura do Storm no payload, que é como o
    worker monta desde esta task (o Storm viaja em TODA run, $0 de LLM)."""
    plano = {**_PLANO, "storm": _STORM} if com_storm else dict(_PLANO)
    r = {"verdict": None, "final_decision": "", "timeframe": "1d",
         "as_of_price": 465.58, "actionable": plano,
         "live_price": None, "price_chart": _CHART, "degraded": [],
         "bull": "", "bear": "", "research_manager": "", "investment_plan": "",
         "trader_plan": "", "risk_decision": "", "market_report": "",
         "sentiment_report": "", "news_report": "", "fundamentals_report": "",
         "erick_report": "", "drop_nature": {}, "derivatives_report": "",
         "setup123": metodo == "setup123", "storm123": metodo == "storm123"}
    return {"run_id": "R-033", "ticker": "MSFT", "date": "2026-08-29",
            "asset_type": "stock", "status": "done", "elapsed": 2,
            "cost": {"usd": 0.0}, "verdict": None, "verdict_timeframe": "1d",
            "result": r}


def _abre(page, base_url, metodo, *, com_storm=True):
    snap = _snap(metodo, com_storm=com_storm)

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
    page.evaluate("() => watchRun('R-033')")
    page.wait_for_selector("#setupCards:not(.hidden)")
    page.wait_for_timeout(300)


_AVISO = """() => {
  const el = document.getElementById('camadasAviso');
  return {
    visivel: !!el && !el.classList.contains('hidden'),
    txt: el ? el.innerText.replace(/\\s+/g, ' ').trim() : '',
    botoes: [...document.querySelectorAll('#camadasAviso .cav-btn')]
              .map(b => b.dataset.camada),
  };
}"""

_DESENHO = """() => ({
  zonas: planZones(document.getElementById('priceChart')._actionable || {}).map(z => z.tag),
  pontos: JSON.parse(document.getElementById('priceChart').dataset.pat123 || '[]')
            .map(p => p.familia),
})"""


# ───────────────── o aviso existe, nomeia a leitura e liga num clique ─────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [DESKTOP, TELEFONE], ids=["desktop", "telefone"])
def test_analise_padrao_ANUNCIA_que_existe_leitura_do_storm(base, viewport):
    """DENTE: o Storm sumia do gráfico numa análise Padrão e nada dizia que ele
    existia."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre(page, base, "padrao")
        # antes: o desenho é só o do método aberto (a DA-088 continua valendo)
        antes = page.evaluate(_DESENHO)
        assert set(antes["pontos"]) == {"plano"}, antes
        assert not any("Storm" in t for t in antes["zonas"]), antes

        m = page.evaluate(_AVISO)
        assert m["visivel"], "leitura disponível e desligada tem de se anunciar"
        assert "Storm123" in m["txt"], m
        assert "não está desenhada" in m["txt"], ("o aviso diz o ESTADO, não só o nome", m)
        assert m["botoes"] == ["storm"], m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [DESKTOP, TELEFONE], ids=["desktop", "telefone"])
def test_um_clique_no_aviso_desenha_a_leitura_e_o_aviso_some(base, viewport):
    """O caminho de ligar é UM clique, no próprio gráfico — não um menu a descobrir."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre(page, base, "padrao")
        page.click('#camadasAviso .cav-btn[data-camada="storm"]')
        page.wait_for_timeout(350)
        depois = page.evaluate(_DESENHO)
        assert "storm" in set(depois["pontos"]), ("o Storm tem de aparecer na vela", depois)
        assert any("Storm" in t for t in depois["zonas"]), depois
        # e o aviso se cala: não há mais leitura disponível fora do gráfico
        assert not page.evaluate(_AVISO)["visivel"], "aviso não pode virar ruído fixo"
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_aviso_vale_nos_DOIS_sentidos(base):
    """Numa run do Storm quem fica de fora é o Setup123 — e ele também se anuncia.
    Regra de tela que só vale num sentido é exceção, não regra."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, "storm123")
        m = page.evaluate(_AVISO)
        assert m["visivel"] and m["botoes"] == ["plano"], m
        assert "Setup123" in m["txt"], m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_sem_leitura_de_fora_NAO_ha_aviso(base):
    """O outro lado da régua: sem isto, qualquer banner passaria nos testes acima."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, "padrao", com_storm=False)
        assert not page.evaluate(_AVISO)["visivel"], (
            "não existe leitura do Storm neste plano — não há o que anunciar")
        browser.close()


# ───────────────────── o caminho de ligar tem de estar À VISTA ────────────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_no_celular_o_aviso_e_o_seletor_ficam_com_o_grafico_na_tela(base):
    """Botão abaixo da dobra tem o mesmo efeito prático do sumiço. Com o gráfico na
    tela, o seletor de camadas E o aviso têm de estar visíveis junto dele."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE)
        _abre(page, base, "padrao")
        page.locator("#chartCard").scroll_into_view_if_needed()
        page.wait_for_timeout(250)
        caixas = page.evaluate("""() => {
          const r = (id) => { const e = document.getElementById(id);
            const b = e.getBoundingClientRect();
            return {top: b.top, bottom: b.bottom, h: b.height}; };
          return {aviso: r('camadasAviso'), sel: r('camadasSelector'),
                  canvas: r('priceChart'), vh: window.innerHeight};
        }""")
        for nome in ("aviso", "sel"):
            b = caixas[nome]
            assert b["h"] > 0, (nome, b)
            assert b["top"] >= 0 and b["bottom"] <= caixas["vh"], (
                f"{nome} fora do viewport do celular", caixas)
        # e o gráfico está ali com eles — o aviso não pode empurrar o canvas pra fora
        assert caixas["canvas"]["top"] < caixas["vh"], caixas
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_storm_VETADO_pelo_eden_TAMBEM_se_anuncia(base):
    """SUPERSEDE a regra de algumas horas atrás ("só se anuncia o que desenha", com o
    desenho exigindo ``opera``). O padrão vetado passou a ser DESENHADO — com o veto
    escrito na vela —, então a camada dele tem o que ligar e o aviso volta a valer.

    A regra que fica é a mesma dos dois lados: **camada que desenha se oferece**. O que
    o veto tira são os NÍVEIS, não a figura."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        vetado = {**_STORM, "opera": False, "qualidade": "ruim",
                  "veto": "sem Éden alinhado — a MME 8 está acima da MME 80"}
        snap = _snap("padrao")
        snap["result"]["actionable"] = {**_PLANO, "storm": vetado}

        def handler(route):
            url = route.request.url
            if "/api/execucao" in url:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"card": None}))
            elif "/api/status/" in url or re.search(r"/api/run/[^/]+$", url):
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps(snap))
            else:
                route.continue_()
        page.route(re.compile(r"/api/"), handler)
        page.goto(base, wait_until="networkidle")
        page.evaluate("() => watchRun('R-033')")
        page.wait_for_selector("#setupCards:not(.hidden)")
        page.wait_for_timeout(300)

        m = page.evaluate(_AVISO)
        assert m["visivel"] and m["botoes"] == ["storm"], m
        assert page.evaluate("() => camadasDisponiveis(document.getElementById"
                             "('priceChart')._actionable || {})") == ["plano", "storm"]
        # e ligar de fato DESENHA o padrão, marcado como vetado — sem níveis
        page.click('#camadasAviso .cav-btn[data-camada="storm"]')
        page.wait_for_timeout(350)
        d = page.evaluate(_DESENHO)
        assert "storm" in set(d["pontos"]), ("o padrão vetado é desenhado", d)
        assert not any("Storm" in t for t in d["zonas"]), ("mas sem NÍVEL operável", d)
        # o card continua com o veto escrito
        txt = page.evaluate("() => document.getElementById('setupCards').innerText")
        assert "NÃO OPERA" in txt and "sem Éden alinhado" in txt, txt
        browser.close()

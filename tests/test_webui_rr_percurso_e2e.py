"""Na TELA: o R:R baixo vem com a razão, e nunca sozinho (task 20260830-008).

A conta está certa — o que faltava era a tela dizer o que ela significa. Um `0,09:1`
sozinho lê-se como "o método dá trade ruim"; ao lado do `5,97:1` que o setup oferecia
NO GATILHO, lê-se "cheguei tarde". São conclusões opostas sobre o mesmo dado, e a
diferença entre elas é o que estes testes travam:

  * padrão ACIONADO E ESTICADO → os dois R:R na tela, o percurso medido e o motivo
    escrito; e o estado do card distingue de relance um trade que já andou 91% do
    caminho de um rompimento de ontem;
  * padrão ACIONADO E RECENTE → também os dois números, mas próximos — é o
    contraponto que impede a leitura preguiçosa "acionado = ruim";
  * padrão NÃO ACIONADO → uma linha só. Ali a entrada É o gatilho, e um segundo
    número seria o mesmo preço escrito duas vezes (DA-077).
"""

import json
import re

import pytest

from tests.test_webui_frame_e_cor_e2e import (
    _CHART,
    DESKTOP,
    TELEFONE,
    sobe_servidor,
)
from tradingagents.webui import timeutil

pytestmark = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None

_HOJE = timeutil.today()


@pytest.fixture
def base(tmp_path):
    yield from sobe_servidor(tmp_path)


# Os números do print: venda, gatilho 517,35 · stop 526,92 · alvo 460,21.
def _plano(preco, state, rr):
    return {
        "symbol": "MSFT", "price": preco, "as_of": "2026-08-28 17:30",
        "timeframe": "4 horas (intradiário)", "horizon": "dias",
        "setup_state": "aguardar_rompimento", "setup_source": "123",
        "buy_zone": None, "realize_zone": None, "pullback_zone": None,
        "pattern": {"p1": {"date": "2026-07-10", "price": 470.0},
                    "p2": {"date": "2026-08-02", "price": 517.35},
                    "p3": {"date": "2026-08-15", "price": 522.0},
                    "trigger": 517.35, "state": state, "direction": "venda"},
        "invalidation": {"price": 522.0, "meaning": "retomada do ponto 3"},
        "stop": {"label": "stop (SL)", "price": 526.92, "anchor": 522.0, "atr": 9.8,
                 "basis": "invalidação + folga de 0.5·ATR14"},
        "target": {"label": "fundo anterior", "price": 460.21, "same_as_realize": False},
        "risk_reward": rr,
    }


_ESTICADO = _plano(465.58, "acionado", {
    "entry": 465.58, "entry_basis": "preço atual (padrão já acionado)",
    "risk": 61.34, "reward": 5.37, "rr": 0.09, "note": None,
    "no_gatilho": {"entry": 517.35, "entry_basis": "gatilho — perda da mínima do ponto 2",
                   "risk": 9.57, "reward": 57.14, "rr": 5.97, "note": None},
    "andado_pct": 90.6, "sobra_pct": 9.4,
    "motivo": ("o gatilho ficou para trás: o preço já andou 91% do caminho até o alvo "
               "e sobra 9%. O R:R daqui mede o que RESTA, não o que o setup ofereceu."),
})

_RECENTE = _plano(512.0, "acionado", {
    "entry": 512.0, "entry_basis": "preço atual (padrão já acionado)",
    "risk": 14.92, "reward": 51.79, "rr": 3.47, "note": None,
    "no_gatilho": {"entry": 517.35, "entry_basis": "gatilho — perda da mínima do ponto 2",
                   "risk": 9.57, "reward": 57.14, "rr": 5.97, "note": None},
    "andado_pct": 9.4, "sobra_pct": 90.6,
    "motivo": ("o gatilho ficou para trás: o preço já andou 9% do caminho até o alvo "
               "e sobra 91%. O R:R daqui mede o que RESTA, não o que o setup ofereceu."),
})

_FORMANDO = _plano(521.0, "formando", {
    "entry": 517.35, "entry_basis": "gatilho — perda da mínima do ponto 2",
    "risk": 9.57, "reward": 57.14, "rr": 5.97, "note": None,
})


def _snap(actionable):
    return {
        "run_id": "R-008", "ticker": "MSFT", "date": "2026-08-29", "asset_type": "stock",
        "status": "done", "elapsed": 2, "cost": {"usd": 0.0},
        "verdict": None, "verdict_timeframe": "4h",
        "result": {
            "setup123": True, "verdict": None, "final_decision": "",
            "timeframe": "4h", "as_of_price": actionable["price"],
            "actionable": actionable,
            "live_price": {"price": actionable["price"], "change_pct": -1.0,
                           "currency": "USD", "sessao": "fechado",
                           "rotulo": "último fechamento", "as_of": "29/08 16:00",
                           "regular_price": actionable["price"],
                           "fuso": "America/New_York", "em": _HOJE},
            "price_chart": dict(_CHART, timeframe="4h"), "degraded": [],
            "bull": "", "bear": "", "research_manager": "", "investment_plan": "",
            "trader_plan": "", "risk_decision": "", "market_report": "",
            "sentiment_report": "", "news_report": "", "fundamentals_report": "",
            "erick_report": "", "drop_nature": {}, "derivatives_report": "",
        },
    }


def _abre(page, base_url, actionable):
    snap = _snap(actionable)

    def handler(route):
        url = route.request.url
        if "/api/status/" in url or re.search(r"/api/run/[^/]+$", url):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(snap))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)
    page.goto(base_url, wait_until="networkidle")
    page.evaluate("() => watchRun('R-008')")
    page.wait_for_selector("#setupCards:not(.hidden)")
    page.wait_for_timeout(200)


_LE = """() => {
  const el = document.getElementById('setupCards');
  const linha = (k) => {
    for (const r of el.querySelectorAll('.sc-row')) {
      const n = r.querySelector('.sc-k');
      if (n && n.innerText.trim().toLowerCase() === k) {
        return {valor: (r.querySelector('.sc-v') || {}).innerText || '',
                base: (r.querySelector('.sc-basis') || {}).innerText || ''};
      }
    }
    return null;
  };
  return {
    agora: linha('risco/retorno agora'), simples: linha('risco/retorno'),
    gatilho: linha('no gatilho'), percurso: linha('percurso do setup'),
    estado: (el.querySelector('.sc-now') || {}).innerText || '',
    chip: document.getElementById('priceChart').dataset.rr || '',
    txt: el.innerText,
  };
}"""


# ───────────────────────── acionado e ESTICADO (o caso do print) ──────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [DESKTOP, TELEFONE], ids=["desktop", "telefone"])
def test_acionado_esticado_mostra_os_dois_rr_e_o_motivo(base, viewport):
    """DENTE: a tela mostrava só o 0,09:1, e quem lê conclui que o método dá trade
    ruim. O 5,97:1 do gatilho é o que diz que o método entregou — o que faltou foi
    chegar a tempo."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre(page, base, _ESTICADO)
        m = page.evaluate(_LE)
        assert m["agora"] and "0,09:1" in m["agora"]["valor"], m
        assert m["gatilho"] and "5,97:1" in m["gatilho"]["valor"], m
        assert "oferecia a quem entrou em 517,35" in m["gatilho"]["base"], m
        assert m["percurso"], ("a régua do percurso tem de estar na tela", m)
        assert "andou 91%" in m["percurso"]["valor"], m
        assert "sobra 9%" in m["percurso"]["valor"], m
        assert "o que RESTA" in m["percurso"]["base"], m
        # o número baixo NUNCA aparece sozinho
        assert m["simples"] is None, ("com dois números o rótulo diz QUAL é qual", m)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_estado_do_card_distingue_esticado_de_fresco(base):
    """A distinção de RELANCE que o critério pede: "acionado" sozinho não separa um
    rompimento de ontem de um trade que já andou 91% do caminho."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, _ESTICADO)
        esticado = page.evaluate(_LE)
        assert "acionado" in esticado["estado"], esticado
        assert "andou 91%" in esticado["estado"], esticado

        _abre(page, base, _RECENTE)
        recente = page.evaluate(_LE)
        assert "andou 9%" in recente["estado"], recente
        assert esticado["estado"] != recente["estado"], (esticado, recente)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_chip_do_grafico_explica_o_numero_pelo_percurso(base):
    """No gráfico o espaço é curto: entre "risco 11x o retorno" (que só repete que é
    pouco) e "andou 91% do caminho" (que diz por quê), vale o segundo."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, _ESTICADO)
        m = page.evaluate(_LE)
        assert "0,09:1" in m["chip"], m["chip"]
        assert "andou 91%" in m["chip"], m["chip"]
        browser.close()


# ──────────────────────────── acionado e RECENTE ─────────────────────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_acionado_recente_continua_com_R_R_de_verdade(base):
    """O contraponto: acionado NÃO é sinônimo de ruim. Aqui os dois números são
    próximos e o de agora é ótimo — se a tela rebaixasse todo acionado, este sumiria
    junto com o esticado, e seriam duas mentiras em vez de uma."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, _RECENTE)
        m = page.evaluate(_LE)
        assert "3,47:1" in m["agora"]["valor"], m
        assert "5,97:1" in m["gatilho"]["valor"], m
        assert "sobra 91%" in m["percurso"]["valor"], m
        # e nada de alarme: R:R bom não vira aviso (DA-078)
        assert "risco > retorno" not in m["txt"], m["txt"]
        browser.close()


# ────────────────────────────── NÃO acionado ─────────────────────────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_nao_acionado_tem_UMA_linha_de_rr_e_nenhum_percurso(base):
    """Ali a entrada É o gatilho: um segundo número seria o mesmo preço escrito duas
    vezes, e repetir dado é o defeito que a DA-077 combate."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, _FORMANDO)
        m = page.evaluate(_LE)
        assert m["simples"] and "5,97:1" in m["simples"]["valor"], m
        assert m["agora"] is None and m["gatilho"] is None, m
        assert m["percurso"] is None, m
        assert "andou" not in m["estado"], m
        assert "em formação" in m["estado"], m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [DESKTOP, TELEFONE], ids=["desktop", "telefone"])
def test_as_linhas_novas_nao_cortam_nem_estouram(base, viewport):
    """DA-062: duas linhas a mais no card não podem trazer de volta o defeito que a
    DA-078 regra 11 fechou."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre(page, base, _ESTICADO)
        m = page.evaluate("""() => {
          const el = document.getElementById('setupCards');
          return {cortados: [...el.querySelectorAll('*')]
                    .filter(e => e.scrollWidth > e.clientWidth + 1)
                    .map(e => (e.className || '').toString().slice(0, 40)),
                  rola: document.documentElement.scrollWidth >
                        document.documentElement.clientWidth};
        }""")
        assert m["cortados"] == [], m
        assert not m["rola"], m
        browser.close()

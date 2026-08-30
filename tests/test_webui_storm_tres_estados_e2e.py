"""PADRÃO DETECTADO É PADRÃO DESENHADO — os três estados do Storm (task 20260830-034).

A causa raiz do *"eu não vi nenhum desenho do storm123 nos gráficos que analisei"*: o
padrão vetado pelo Éden era **detectado**, era **descrito no card com todos os níveis**
e simplesmente **não era desenhado**. O usuário lia "Storm123 de compra · NÃO OPERA" no
card e não achava nada na vela — a tela contradizendo a si mesma. E não era efeito das
camadas: o ``opera === true`` na condição do desenho é anterior a elas.

A regra que fica: **nunca sumir em silêncio**. Três estados, distintos e DECLARADOS:

  * **operável** — cor do Storm, contorno sólido, NÍVEIS na tela;
  * **vetado** — mesma cor (a estrutura é real e atual), peso menor, contorno tracejado
    e a palavra "não opera — Éden". Sem níveis: o gráfico é a figura operável, e traçar
    o gatilho de um trade que a regra proíbe é convidar a operá-lo;
  * **invalidado** — cinza, mais apagado, e a palavra "invalidado" (DA-091).

Precedência: morto ganha de vetado. O veto descreve um setup que ainda existe; a
invalidação, um que não existe mais.
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


OPERAVEL = _STORM
VETADO = {**_STORM, "opera": False, "qualidade": "ruim",
          "veto": "padrão de venda contra Éden de compra — operar contra o Éden é o "
                  "caso que a regra proíbe",
          "motivo": "o Éden está de compra e o padrão é de venda"}
MORTO = {**_STORM, "pattern": {**_STORM["pattern"], "invalidado": True,
                               "invalidado_em": "2026-08-27"}}
# Morto E vetado ao mesmo tempo: a precedência tem de ser observável, não presumida.
MORTO_E_VETADO = {**VETADO, "pattern": MORTO["pattern"]}


def _abre(page, base_url, storm):
    r = {"verdict": None, "final_decision": "", "timeframe": "1d",
         "as_of_price": 465.58, "actionable": {**_PLANO, "storm": storm},
         "live_price": None, "price_chart": _CHART, "degraded": [],
         "bull": "", "bear": "", "research_manager": "", "investment_plan": "",
         "trader_plan": "", "risk_decision": "", "market_report": "",
         "sentiment_report": "", "news_report": "", "fundamentals_report": "",
         "erick_report": "", "drop_nature": {}, "derivatives_report": "",
         "setup123": False, "storm123": True}
    snap = {"run_id": "R-034", "ticker": "MSFT", "date": "2026-08-29",
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
    page.evaluate("() => watchRun('R-034')")
    page.wait_for_selector("#setupCards:not(.hidden)")
    page.wait_for_timeout(300)


_LE = """() => ({
  pontos: JSON.parse(document.getElementById('priceChart').dataset.pat123 || '[]')
            .filter(p => p.familia === 'storm'),
  rotulos: JSON.parse(document.getElementById('priceChart').dataset.rotulos123 || '[]'),
  zonas: planZones(document.getElementById('priceChart')._actionable || {})
           .map(z => z.tag),
  legenda: document.getElementById('chartLegend').innerText.replace(/\\s+/g, ' '),
  nota: document.getElementById('chartNote').innerText.replace(/\\s+/g, ' '),
  chip: document.getElementById('priceChart').dataset.rr || '',
  estado: stormEstado((document.getElementById('priceChart')._actionable || {}).storm),
})"""


# ───────────────────── o padrão vetado APARECE (o dente) ──────────────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [DESKTOP, TELEFONE], ids=["desktop", "telefone"])
def test_padrao_vetado_pelo_eden_e_DESENHADO_na_vela(base, viewport):
    """DENTE: o card dizia "Storm123 de venda · NÃO OPERA" e o gráfico não tinha
    nada — nem os três pontos, nem stop, nem alvo, nem gatilho."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre(page, base, VETADO)
        m = page.evaluate(_LE)
        assert m["estado"] == "vetado", m
        assert [d["lab"] for d in m["pontos"]] == ["1", "2", "3"], m
        assert [d["preco"] for d in m["pontos"]] == ["474,00", "436,00", "466,00"], m
        assert all(d["vetado"] for d in m["pontos"]), m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_vetado_DIZ_que_e_vetado_na_vela_na_legenda_e_na_nota(base):
    """Desenhar sem declarar seria trocar um defeito por outro: o padrão vetado tem a
    mesma aparência de um operável se ninguém escrever o estado."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, VETADO)
        m = page.evaluate(_LE)
        assert any("não opera" in r for r in m["rotulos"]), ("na VELA", m["rotulos"])
        assert "não opera — Éden" in m["legenda"], ("na LEGENDA", m["legenda"])
        assert "não operável" in m["nota"] and "Éden" in m["nota"], ("na NOTA", m["nota"])
        # a nota carrega o MOTIVO do veto, não só o rótulo
        assert "contra o Éden" in m["nota"], m["nota"]
        # e o chip não cala: gráfico sem chip é indistinguível de gráfico sem setup
        assert "Éden veta" in m["chip"], m["chip"]
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_vetado_NAO_ganha_nivel_operavel(base):
    """A fronteira: a FIGURA aparece, os NÍVEIS não. Traçar o gatilho de um trade que a
    regra proíbe é convidar a operá-lo — e o card tem cada número."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, VETADO)
        m = page.evaluate(_LE)
        assert not any("Storm" in t for t in m["zonas"]), m["zonas"]
        assert not any("452" in r for r in m["rotulos"]), ("o gatilho, não", m["rotulos"])
        pilulas = page.evaluate(
            "() => JSON.parse(document.getElementById('priceChart').dataset.axisPills || '[]')")
        assert not any("452" in t for t in pilulas), ("nem na régua", pilulas)
        browser.close()


# ─────────────────── os três estados são DISTINTOS entre si ───────────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_operavel_vetado_e_invalidado_nao_se_confundem(base):
    """Três estados que se parecem são um estado só. Cada um tem de dizer o seu nome —
    e o operável é o único que não precisa dizer nada, porque é o normal."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)

        _abre(page, base, OPERAVEL)
        vivo = page.evaluate(_LE)
        _abre(page, base, VETADO)
        vet = page.evaluate(_LE)
        _abre(page, base, MORTO)
        morto = page.evaluate(_LE)

        assert [vivo["estado"], vet["estado"], morto["estado"]] == \
            ["operavel", "vetado", "invalidado"], (vivo, vet, morto)
        # o operável desenha níveis; os outros dois, não
        assert any("Storm" in t for t in vivo["zonas"]), vivo["zonas"]
        assert vet["zonas"] == morto["zonas"] == [t for t in vet["zonas"] if "Storm" not in t]
        # cor: o morto sai do vocabulário dos vivos; o vetado continua nele
        assert morto["pontos"][0]["cor"].lower() == "#6b7280", morto["pontos"][0]
        assert vet["pontos"][0]["cor"] == vivo["pontos"][0]["cor"], (vet, vivo)
        # palavra: cada desvio se nomeia, e o normal não escreve nada
        assert not any("não opera" in r or "invalidado" in r for r in vivo["rotulos"]), vivo
        assert any("não opera" in r for r in vet["rotulos"]), vet
        assert any("invalidado" in r for r in morto["rotulos"]), morto
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_morto_ganha_de_vetado(base):
    """Precedência declarada: o veto descreve um setup que ainda existe; a invalidação,
    um que não existe mais. Um padrão morto E vetado é MORTO."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, MORTO_E_VETADO)
        m = page.evaluate(_LE)
        assert m["estado"] == "invalidado", m
        assert any("invalidado" in r for r in m["rotulos"]), m["rotulos"]
        assert not any("não opera" in r for r in m["rotulos"]), m["rotulos"]
        assert m["pontos"][0]["cor"].lower() == "#6b7280", m["pontos"][0]
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_sem_padrao_nao_ha_o_que_desenhar_nem_o_que_declarar(base):
    """O outro lado da régua: "nunca sumir em silêncio" vale para padrão DETECTADO.
    Sem padrão, não há estado nenhum a inventar."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, {**VETADO, "pattern": None})
        m = page.evaluate(_LE)
        assert m["estado"] is None, m
        assert m["pontos"] == [], m
        assert "não opera" not in m["legenda"], m["legenda"]
        browser.close()

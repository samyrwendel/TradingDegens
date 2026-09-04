"""FANTASMA na tela + a faixa do ponto 3 (task 20260830-013).

*"Deve mudar a cor do 123 se invalidou (tipo um fantasma) e avisar no card com
detalhes que invalidou, se tiver em formação de 123, marcar onde deve ser a nova
formação do 3."*

O que se mede aqui:

  * o padrão morto sai do vocabulário de COR dos vivos (azul de compra / laranja de
    venda) e vira cinza — e perde o que convida a operar: a linha do gatilho e a
    pílula dele no eixo;
  * o card DIZ que invalidou com detalhe: qual nível, QUANDO, e o que significa pra
    quem estava posicionado — não um selo;
  * a faixa do ponto 3 aparece com a CONDIÇÃO escrita, e em cor neutra: é espera, não
    nível operável;
  * vale nos DOIS detectores e nos dois sentidos.
"""

import json
import re

import pytest

from tests.test_webui_frame_e_cor_e2e import DESKTOP, TELEFONE, sobe_servidor
from tests.test_webui_um_grafico_um_metodo_e2e import _CHART, _snap

pytestmark = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None


@pytest.fixture
def base(tmp_path):
    yield from sobe_servidor(tmp_path)


_PONTOS = [{"date": "2026-08-24", "price": 470.0},
           {"date": "2026-08-25", "price": 440.0},
           {"date": "2026-08-26", "price": 462.0}]


def _plano(*, invalidado=False, projecao=None, direcao="venda"):
    return {
        "symbol": "MSFT", "price": 465.58, "as_of": "2026-08-28T17:30:00-04:00",
        "timeframe": "diário (referência)", "horizon": "dias",
        "setup_state": "aguardar_rompimento", "setup_source": "123",
        "buy_zone": None, "realize_zone": None, "pullback_zone": None,
        "pattern": {"p1": _PONTOS[0], "p2": _PONTOS[1], "p3": _PONTOS[2],
                    "trigger": 440.0, "state": "formando", "direction": direcao,
                    "invalidado": invalidado,
                    "invalidado_em": "2026-08-27" if invalidado else None},
        "invalidation": {"price": 462.0, "meaning": "retomada do ponto 3"},
        "stop": {"label": "stop (SL)", "price": 466.0, "anchor": 462.0, "atr": 4.0,
                 "basis": "invalidação + folga de 0.5·ATR14"},
        "target": {"label": "fundo anterior", "price": 414.0, "same_as_realize": False},
        "risk_reward": {"entry": 440.0, "entry_basis": "gatilho", "risk": 26.0,
                        "reward": 26.0, "rr": 1.0, "note": None},
        "projecao_p3": projecao,
    }


_PROJ = {"direcao": "venda", "caso": "novo_apos_invalidacao", "low": 440.0,
         "high": 470.0, "price": 455.0, "gatilho_futuro": 440.0,
         "condicao": ("vira um 1-2-3 de venda se fizer um TOPO abaixo de 470,00 "
                      "(romper esse nível mata a formação) e depois perder 440,00")}


def _abre(page, base_url, plano):
    snap = _snap("setup123")
    snap["result"]["actionable"] = plano
    snap["result"]["price_chart"] = dict(
        _CHART, timeframe="1d",
        markers={**_CHART["markers"], "pattern_123": plano["pattern"]})

    def handler(route):
        url = route.request.url
        if "/api/status/" in url or re.search(r"/api/run/[^/]+$", url):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(snap))
        elif "/api/execucao" in url:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"card": None}))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)
    page.goto(base_url, wait_until="networkidle")
    page.evaluate("() => watchRun('R-009')")
    page.wait_for_selector("#setupCards:not(.hidden)")
    page.wait_for_timeout(250)


_LE = """() => {
  const el = document.getElementById('setupCards');
  const card = el.querySelector('.sc-123');
  return {
    txt: el.innerText,
    estado: (el.querySelector('.sc-123 .sc-now') || {}).innerText || '',
    classes: card ? card.className : '',
    cor: (() => { const p = document.getElementById('priceChart')._chart;
      return patColor((p.markers || {}).pattern_123); })(),
    corViva: (() => { const p = document.getElementById('priceChart')._chart;
      const pat = (p.markers || {}).pattern_123;
      return patColor({...pat, invalidado: false}); })(),
    pilulas: JSON.parse(document.getElementById('priceChart').dataset.axisPills || '[]'),
    zonas: planZones(document.getElementById('priceChart')._actionable || {})
             .map(z => ({tag: z.tag, cor: z.color})),
  };
}"""


# ───────────────────────── (2) o fantasma no gráfico ───────────────────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("direcao", ["compra", "venda"])
def test_padrao_invalidado_sai_do_vocabulario_de_cor_dos_vivos(base, direcao):
    """DENTE: um 1-2-3 morto continuava com a MESMA cor de um vivo, e a cor é a
    primeira coisa que se lê."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, _plano(invalidado=True, direcao=direcao))
        m = page.evaluate(_LE)
        assert m["cor"] != m["corViva"], ("o morto tem de mudar de cor", m)
        assert m["cor"].lower() == "#6b7280", m["cor"]
        # cinza de verdade: sem canal dominante, ao contrário do azul/laranja vivos
        r, g, b = (int(m["cor"][i:i + 2], 16) for i in (1, 3, 5))
        assert max(r, g, b) - min(r, g, b) < 40, ("cinza, não uma cor de método", m["cor"])
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_padrao_vivo_NAO_vira_fantasma(base):
    """O outro lado da régua: sem isto, qualquer cinza passaria no teste acima."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, _plano(invalidado=False))
        m = page.evaluate(_LE)
        assert m["cor"] == m["corViva"], m
        assert m["cor"].lower() != "#6b7280", m["cor"]
        assert "sc-fantasma" not in m["classes"], m["classes"]
        browser.close()


# ─────────────────── (3) o card diz que morreu, COM detalhe ────────────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [DESKTOP, TELEFONE], ids=["desktop", "telefone"])
def test_o_card_declara_a_morte_com_nivel_data_e_consequencia(base, viewport):
    """Não é um selo: qual nível foi perdido, QUANDO, e o que isso significa pra quem
    estava posicionado."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre(page, base, _plano(invalidado=True))
        m = page.evaluate(_LE)
        assert "sc-fantasma" in m["classes"], m["classes"]
        assert "INVALIDADO" in m["txt"], m["txt"]
        assert "462,00" in m["txt"], ("o nível perdido", m["txt"])
        assert "27/08" in m["txt"], ("QUANDO morreu", m["txt"])
        assert "perdeu a premissa" in m["txt"], ("o que significa", m["txt"])
        assert "não existe mais" in m["txt"], m["txt"]
        # e o estado do card para de descrever um setup vivo
        assert m["estado"].startswith("invalidado"), m["estado"]
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_venda_e_a_compra_explicam_a_morte_com_a_SUA_estrutura(base):
    """Espelho, não texto genérico: na compra morrem os fundos ascendentes, na venda
    os topos descendentes."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, _plano(invalidado=True, direcao="compra"))
        compra = page.evaluate(_LE)["txt"]
        assert "fundos deixaram de ser ascendentes" in compra, compra
        _abre(page, base, _plano(invalidado=True, direcao="venda"))
        venda = page.evaluate(_LE)["txt"]
        assert "topos deixaram de ser descendentes" in venda, venda
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_morto_perde_o_que_convida_a_operar(base):
    """A pílula do gatilho no eixo é onde o olho procura preço operável. Pôr ali o
    gatilho de um setup extinto é convidar a operá-lo."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, _plano(invalidado=False))
        vivo = page.evaluate(
            "() => (document.getElementById('priceChart').dataset.axisPills || '')")
        _abre(page, base, _plano(invalidado=True))
        morto = page.evaluate(
            "() => (document.getElementById('priceChart').dataset.axisPills || '')")
        assert vivo, ("o canvas tem de expor as pílulas — sem isso este teste não "
                      "mede nada", vivo)
        assert "440" in vivo, vivo
        assert "440" not in morto, ("gatilho de padrão morto no eixo", morto)
        browser.close()


# ──────────────── (4) a faixa do ponto 3, com a condição escrita ──────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [DESKTOP, TELEFONE], ids=["desktop", "telefone"])
def test_a_faixa_do_ponto_3_aparece_com_a_condicao_escrita(base, viewport):
    """A "preparação para acompanhar a hora de entrar": a faixa E a regra que a
    valida — uma faixa sem condição seria um retângulo sem sentido."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre(page, base, _plano(invalidado=True, projecao=_PROJ))
        m = page.evaluate(_LE)
        # Num setup MORTO a projeção do ponto 3 é um CANDIDATO NOVO, não a
        # continuação do que já terminou (task 20260904-002): o rótulo diz isso. A
        # faixa e a condição escrita seguem — só o nome do bloco muda.
        assert "candidato novo — ponto 3" in m["txt"], m["txt"]
        assert "440,00–470,00" in m["txt"], m["txt"]
        assert "TOPO abaixo de 470,00" in m["txt"], ("a condição, escrita", m["txt"])
        assert "mata a formação" in m["txt"], m["txt"]
        # e no gráfico: faixa NEUTRA, porque é espera e não nível operável
        faixa = next((z for z in m["zonas"] if "ponto 3" in z["tag"]), None)
        assert faixa is not None, m["zonas"]
        r, g, b = (int(faixa["cor"][i:i + 2], 16) for i in (1, 3, 5))
        assert not (g > r + 30 and g > b + 30), ("faixa de espera não pode ser verde "
                                                 "de compra", faixa)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_sem_faixa_a_tela_DECLARA_a_ausencia_em_vez_de_desenhar_chute(base):
    """O critério explícito: quando a regra não delimita, declara-se ausente."""
    sem = {"direcao": "compra", "caso": "novo_apos_invalidacao", "low": None,
           "high": None,
           "motivo": "o ponto 1 (440,00) foi perdido — não há 1-2-3 de compra em gestação."}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, _plano(invalidado=True, projecao=sem))
        m = page.evaluate(_LE)
        assert "sem faixa a marcar" in m["txt"], m["txt"]
        assert "foi perdido" in m["txt"], m["txt"]
        assert not any("ponto 3" in z["tag"] for z in m["zonas"]), (
            "sem faixa no dado, sem retângulo no gráfico", m["zonas"])
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_com_padrao_vivo_nao_ha_preparacao_na_tela(base):
    """Ali o ponto 3 já existe — o que falta é o gatilho, que a tela já marca."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, _plano(invalidado=False, projecao=None))
        m = page.evaluate(_LE)
        assert "preparação" not in m["txt"], m["txt"]
        browser.close()

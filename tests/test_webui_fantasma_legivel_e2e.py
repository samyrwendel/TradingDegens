"""ESMAECIDO NÃO É APAGADO — o contraste do fantasma, medido no pixel (task 20260830-048).

O Samyr circulou o gráfico no celular e perguntou *"aqui não fez o 1-2-3 do storm?"*.
A legenda dizia "1-2-3 de compra (invalidado)" com o losango cinza, e a nota abaixo
prometia textualmente que **"os pontos ficam em cinza como história"**. Na área que ele
circulou não havia ponto nenhum.

A DA-107 já tinha resolvido o caso do padrão que está fora do enquadramento: quando o
ponto cai fora da janela, a tela declara onde ele está. Ali não era isso — os três
pontos estavam ENQUADRADOS, o comando de desenho ocorreu, e mesmo assim não havia o
que ver. O defeito era de CONTRASTE: o cinza ``#6b7280`` pintado a 45% de opacidade
sobre o painel **preto puro** (``#000``) rende ``(48,50,54)`` na tela — **1,6:1**. O
piso da WCAG 1.4.11 para objeto gráfico não textual é 3:1. "Esmaecido" tinha virado
"apagado", e a nota prometia o que a tela não entregava.

O que este módulo mede, e por que assim:

  * o contraste **do pixel**, não da intenção. ``dataset.pat123`` diz onde cada
    marcador foi pintado; daí se lê o canvas com ``getImageData`` e se acha a TINTA do
    marcador (o pixel que é um múltiplo exato da cor sobre o preto — antialiasing
    incluído). Telemetria dizendo "desenhei" foi exatamente o que deixou a suíte verde
    por cima do defeito que o usuário estava olhando;
  * no viewport do CELULAR, que é onde ele viu — 390x844 com ``device_scale_factor``
    3, como o aparelho;
  * e a **régua dos dois lados**: o morto tem de passar do piso de legibilidade E
    continuar claramente abaixo do vivo. Devolver o fantasma à cor dos vivos trocaria
    um defeito por outro — morto e vivo voltariam a competir.
"""

import json
import re

import pytest

from tests.test_webui_frame_e_cor_e2e import TELEFONE, sobe_servidor
from tests.test_webui_um_grafico_um_metodo_e2e import _CHART, _PLANO, _STORM

pytestmark = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None


# O piso da WCAG 1.4.11 (objeto gráfico não textual) contra o fundo do painel. Não é
# um número escolhido aqui: é o limite abaixo do qual a norma considera que a forma
# deixa de ser percebida.
PISO_LEGIVEL = 3.0
# E o teto: o morto não pode chegar perto do vivo. 0,7 do contraste do vivo mantém a
# ordem de leitura ("o que ainda vale salta primeiro") com folga observável.
TETO_SUBORDINADO = 0.7


@pytest.fixture
def base(tmp_path):
    yield from sobe_servidor(tmp_path)


MORTO = {**_STORM, "pattern": {**_STORM["pattern"], "invalidado": True,
                               "invalidado_em": "2026-08-27"}}


def _abre(page, base_url, storm, *, setup123=False):
    r = {"verdict": None, "final_decision": "", "timeframe": "1d",
         "as_of_price": 465.58, "actionable": {**_PLANO, "storm": storm},
         "live_price": None, "price_chart": _CHART, "degraded": [],
         "bull": "", "bear": "", "research_manager": "", "investment_plan": "",
         "trader_plan": "", "risk_decision": "", "market_report": "",
         "sentiment_report": "", "news_report": "", "fundamentals_report": "",
         "erick_report": "", "drop_nature": {}, "derivatives_report": "",
         "setup123": setup123, "storm123": True}
    snap = {"run_id": "R-048", "ticker": "MSFT", "date": "2026-08-29",
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
    page.evaluate("() => watchRun('R-048')")
    page.wait_for_selector("#setupCards:not(.hidden)")
    page.wait_for_timeout(300)


# A TINTA DO MARCADOR, ACHADA NO PIXEL.
#
# Composição sobre preto é multiplicação: um traço da cor ``C`` pintado com opacidade
# ``a`` sai da tela valendo ``a·C``, e o antialiasing só faz variar o ``a``. Então
# procura-se, na caixa do marcador, o pixel que melhor se explica como múltiplo de
# ``C`` (erro por canal pequeno) e com o MAIOR ``a`` — esse é o miolo do traço. Pixels
# de vela, média ou etiqueta não se explicam por ``C`` e caem fora sozinhos.
_TINTA = """([alvo, hex, rx, ry]) => {
  const cv = document.getElementById('priceChart');
  const dpr = window.devicePixelRatio || 1;
  const ctx = cv.getContext('2d');
  const C = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
  const cc = C[0] * C[0] + C[1] * C[1] + C[2] * C[2];
  const x0 = Math.max(0, Math.round((alvo.px - rx) * dpr));
  const y0 = Math.max(0, Math.round((alvo.py - ry) * dpr));
  const w = Math.min(cv.width - x0, Math.round(2 * rx * dpr));
  const h = Math.min(cv.height - y0, Math.round(2 * ry * dpr));
  if (w <= 0 || h <= 0) return null;
  const d = ctx.getImageData(x0, y0, w, h).data;
  const lin = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92
                                                     : Math.pow((v + 0.055) / 1.055, 2.4); };
  let melhor = null, fundo = 1e9;
  for (let i = 0; i < d.length; i += 4) {
    const p = [d[i], d[i + 1], d[i + 2]];
    const lp = 0.2126 * lin(p[0]) + 0.7152 * lin(p[1]) + 0.0722 * lin(p[2]);
    if (lp < fundo) fundo = lp;
    const a = (p[0] * C[0] + p[1] * C[1] + p[2] * C[2]) / cc;
    if (a <= 0.02) continue;
    const err = Math.max(Math.abs(p[0] - a * C[0]), Math.abs(p[1] - a * C[1]),
                         Math.abs(p[2] - a * C[2]));
    if (err > 10) continue;
    if (!melhor || a > melhor.a) melhor = { a, rgb: p, L: lp };
  }
  if (!melhor) return null;
  melhor.fundo = fundo;
  melhor.contraste = (melhor.L + 0.05) / (fundo + 0.05);
  return melhor;
}"""

_PONTOS = ("() => JSON.parse(document.getElementById('priceChart').dataset.pat123 "
           "|| '[]')")


def _mede(page, familia, hex_cor, raio=13):
    """Contraste da tinta do PONTO 2 da família (o do meio, o mais longe das bordas)."""
    pts = [p for p in page.evaluate(_PONTOS) if p["familia"] == familia]
    assert pts, f"nenhum ponto da família {familia} foi pintado"
    assert all(p["naVista"] for p in pts), ("ponto fora do enquadramento — esta medição "
                                            "só vale com o padrão ENQUADRADO", pts)
    alvo = next(p for p in pts if p["lab"] == "2")
    tinta = page.evaluate(_TINTA, [alvo, hex_cor, raio, raio])
    assert tinta, (f"não achei a tinta {hex_cor} na caixa do marcador — o comando de "
                   f"desenho ocorreu e nenhum pixel saiu", alvo)
    return tinta


# ─────────────── o dente: o fantasma tem de ser LEGÍVEL no celular ───────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_fantasma_passa_do_piso_de_legibilidade_no_celular(base):
    """DENTE: 1,6:1 sobre o painel preto. A nota prometia "os pontos ficam em cinza" e
    não havia pixel algum pra achar."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE, device_scale_factor=3)
        _abre(page, base, MORTO)
        cor = page.evaluate("() => stormColor((document.getElementById('priceChart')"
                            "._actionable || {}).storm.pattern)")
        tinta = _mede(page, "storm", cor)
        assert tinta["contraste"] >= PISO_LEGIVEL, (
            f"fantasma a {tinta['contraste']:.2f}:1 contra o painel — abaixo do piso "
            f"de {PISO_LEGIVEL}:1 (WCAG 1.4.11)", tinta)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_fantasma_continua_SUBORDINADO_ao_vivo(base):
    """O outro lado da régua: sem isto, devolver o fantasma à cor dos vivos passaria —
    e morto e vivo voltariam a competir pela atenção."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE, device_scale_factor=3)

        _abre(page, base, _STORM)
        vivo = _mede(page, "storm", page.evaluate(
            "() => stormColor((document.getElementById('priceChart')._actionable || {})"
            ".storm.pattern)"))

        _abre(page, base, MORTO)
        morto = _mede(page, "storm", page.evaluate(
            "() => stormColor((document.getElementById('priceChart')._actionable || {})"
            ".storm.pattern)"))

        assert morto["contraste"] < vivo["contraste"] * TETO_SUBORDINADO, (
            f"morto a {morto['contraste']:.2f}:1 e vivo a {vivo['contraste']:.2f}:1 — "
            f"o morto tem de ficar abaixo de {TETO_SUBORDINADO:.0%} do vivo", morto, vivo)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_preco_do_ponto_morto_tambem_se_le(base):
    """O preço ao lado do marcador levava a MESMA opacidade — um número que ninguém lê
    não diz a que altura o ponto aconteceu, que é pra que ele existe."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE, device_scale_factor=3)
        _abre(page, base, MORTO)
        geo = page.evaluate("() => JSON.parse(document.getElementById('priceChart')"
                            ".dataset.rotulos123Geo || '[]')")
        precos = [g for g in geo if re.fullmatch(r"[\d.,]+", g["text"])]
        assert precos, ("nenhum preço pintado ao lado dos pontos", geo)
        cor = page.evaluate("() => stormColor((document.getElementById('priceChart')"
                            "._actionable || {}).storm.pattern)")
        for g in precos:
            # caixa APERTADA na etiqueta (altura 15px) pra não medir o anel do
            # marcador por engano — os dois usam a mesma tinta, e medir o anel aqui
            # deixaria passar um preço invisível.
            alvo = {"px": g["x"] + g["w"] / 2, "py": g["y"]}
            tinta = page.evaluate(_TINTA, [alvo, cor, g["w"] / 2, 7])
            assert tinta and tinta["contraste"] >= PISO_LEGIVEL, (
                f"o preço {g['text']} do ponto morto não se lê", tinta, g)
        browser.close()


# ────────── a AMOSTRA DA LEGENDA também é o fantasma (a armadilha da DA-106) ──────────
#
# O caminho óbvio pra levantar o contraste do marcador é clarear ``COR_FANTASMA``. Ele
# tem um preço que o canvas não mostra: a amostra da legenda usa a MESMA cor como
# ``background`` em opacidade cheia, e um cinza claro o bastante pra vencer o alpha de
# 0,45 no gráfico (ex.: ``#c9ced6``, 13,3:1) fica MAIS luminoso que o azul do método
# vivo (9,5:1) — o morto vira o chip mais brilhante da legenda. Por isso o fix mora na
# opacidade e não no token, e por isso esta régua fica aqui: a próxima tentativa de
# clarear o cinza falha antes de chegar à tela.
_LUM = r"""(hex) => {
  const c = hex.startsWith('rgb')
    ? hex.match(/\d+/g).slice(0, 3).map(Number)
    : [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
  const lin = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92
                                                     : Math.pow((v + 0.055) / 1.055, 2.4); };
  return 0.2126 * lin(c[0]) + 0.7152 * lin(c[1]) + 0.0722 * lin(c[2]);
}"""


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_amostra_do_fantasma_na_legenda_nao_ofusca_a_do_vivo(base):
    """A legenda pinta a cor CHEIA. Clarear o cinza pra salvar o gráfico faria o morto
    brilhar mais que o método vivo justamente onde se decodifica a tela."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE, device_scale_factor=3)
        _abre(page, base, MORTO)
        amostra = page.evaluate("""() => {
            const sw = document.querySelector('#chartLegend .sw.dia');
            return sw ? getComputedStyle(sw).backgroundColor : null;
        }""")
        assert amostra, "a legenda tem de trazer a amostra do losango do Storm"
        morto = page.evaluate(_LUM, amostra)
        vivo = page.evaluate(_LUM, "#7cb0ff")     # ZONE_COLORS.storm, o método vivo
        assert morto < vivo, (
            f"a amostra do morto ({amostra}, L={morto:.3f}) ficou mais luminosa que a "
            f"do vivo (L={vivo:.3f}) — na legenda o morto passaria a mandar", amostra)
        browser.close()

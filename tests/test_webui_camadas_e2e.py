"""As camadas são DO USUÁRIO, e valem em qualquer frame (task 20260830-010).

"Eu deveria poder selecionar a camada do que eu quero ver" + "no time frame que eu
quiser". Isso muda o requisito da 009: não é só esconder o que não é do método
aberto, é dar o CONTROLE a ele.

O que estes testes travam:

  * o gráfico ABRE na camada do método — ninguém deve precisar configurar nada pra
    ver o próprio resultado;
  * depois do primeiro toque a escolha é dele e PERSISTE nas análises seguintes
    (obrigar a reconfigurar a cada tela é transformar preferência em tarefa);
  * a escolha atravessa a troca de TIMEFRAME;
  * ligar as duas leituras é legítimo — é comparação —, e aí **nenhum rótulo fica
    anônimo**, nem os pontos numerados, que é o que gerou a confusão do print;
  * e a liberdade de frame NÃO apaga qual é o frame do VEREDITO: ver o 15m é direito
    dele, achar que o 15m é o veredito é o defeito (DA-085).

Tudo medido também em viewport de celular — é lá que ele usa e onde a poluição dói.
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


def _abre(page, base_url, metodo, *, primeiro=True):
    """Abre a análise. `/api/chart` responde em qualquer frame com o MESMO plano —
    aqui o que se mede é a camada atravessando a troca, não o recálculo."""
    snap = _snap(metodo)
    plano = snap["result"]["actionable"]

    def handler(route):
        url = route.request.url
        if "/api/chart" in url:
            tf = (re.search(r"[?&]tf=([^&]+)", url) or [None, "1d"])[1]
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "timeframe": tf, "actionable": plano,
                "timeframes": ["1w", "1d", "4h", "1h", "15m"],
                "price_chart": {**_CHART, "timeframe": tf}, "degraded": []}))
        elif "/api/status/" in url or re.search(r"/api/run/[^/]+$", url):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(snap))
        else:
            route.continue_()
    if primeiro:
        page.route(re.compile(r"/api/"), handler)
        page.goto(base_url, wait_until="networkidle")
    else:
        page.route(re.compile(r"/api/"), handler)
    page.evaluate("() => watchRun('R-009')")
    page.wait_for_selector("#setupCards:not(.hidden)")
    page.wait_for_timeout(200)


_LE = """() => ({
  camadas: [..._camadas].sort(),
  tocado: _camadasTocado,
  zonas: planZones(document.getElementById('priceChart')._actionable || {}).map(z => z.tag),
  botoes: [...document.querySelectorAll('#camadasSelector .camada-btn')].map(
    b => ({nome: b.innerText.trim(), on: b.classList.contains('is-active')})),
  medias: (() => { const m = mediasVisiveis(document.getElementById('priceChart')._actionable || {});
    return {ma: [...m.ma].sort(), ema: [...m.ema].sort()}; })(),
  tf: _tf, veredito: _verdictTf,
  carimbo: document.getElementById('priceChart').dataset.tf || '',
  vtf: (document.getElementById('verdictTf') || {}).innerText || '',
  legenda: document.getElementById('chartLegend').innerText.replace(/\\s+/g, ' ').trim(),
})"""


# ──────────────────────── abre no método, depois é dele ───────────────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [DESKTOP, TELEFONE], ids=["desktop", "telefone"])
def test_abre_na_camada_do_metodo_sem_pedir_configuracao(base, viewport):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre(page, base, "storm123")
        m = page.evaluate(_LE)
        assert m["camadas"] == ["storm"], m
        assert m["tocado"] is False, ("nada tocado ainda", m)
        assert all("Storm" in z for z in m["zonas"]), m["zonas"]
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_escolha_do_usuario_PERSISTE_na_proxima_analise(base):
    """O pedido, ao pé da letra. A 009 zerava a cada análise; ele quer o contrário."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, "storm123")
        page.click('.camada-btn[data-camada="plano"]')
        page.click('.camada-btn[data-camada="mms"]')
        page.wait_for_timeout(200)
        escolhido = page.evaluate(_LE)
        assert escolhido["camadas"] == ["mms", "plano", "storm"], escolhido
        assert escolhido["tocado"] is True, escolhido

        _abre(page, base, "storm123", primeiro=False)   # outra análise, mesma sessão
        depois = page.evaluate(_LE)
        assert depois["camadas"] == escolhido["camadas"], (
            "a escolha dele não pode se perder de uma análise pra outra", depois)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_preferencia_nunca_deixa_o_grafico_vazio(base):
    """Liberdade tem um chão: se a preferência não acende nenhuma leitura que ESTE
    plano tem, a do método volta. Gráfico em branco não é liberdade, é defeito."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, "storm123")
        page.click('.camada-btn[data-camada="storm"]')   # desliga a única leitura
        page.wait_for_timeout(150)
        assert page.evaluate("() => planZones(document.getElementById('priceChart')._actionable).length") == 0

        # agora abre uma análise SEM Storm: a preferência não acende nada ali
        _abre(page, base, "setup123", primeiro=False)
        m = page.evaluate(_LE)
        assert "plano" in m["camadas"], ("o chão do método volta", m)
        assert m["zonas"], m
        browser.close()


# ─────────────────────── a camada atravessa o timeframe ───────────────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("tf", ["4h", "1h", "15m"])
def test_a_camada_escolhida_vale_em_qualquer_frame(base, tf):
    """"No time frame que eu quiser" — a escolha não pode se perder ao trocar."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, "storm123")
        page.click('.camada-btn[data-camada="plano"]')
        page.wait_for_timeout(150)
        page.click(f'.tf-btn[data-tf="{tf}"]')
        page.wait_for_function(f"() => _tf === '{tf}' && _tfPendente === null")
        page.wait_for_timeout(200)
        m = page.evaluate(_LE)
        assert m["camadas"] == ["plano", "storm"], (tf, m)
        # e continuam NOMEADAS no frame novo
        for z in m["zonas"]:
            assert z.startswith("Setup123 · ") or z.startswith("Storm123 "), (tf, z)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_ver_outro_frame_nao_apaga_qual_e_o_do_veredito(base):
    """A interação com a DA-085: ver o 15m é direito dele; achar que o 15m é o
    veredito é o defeito. O carimbo continua dizendo, e o gráfico se declara
    exploratório."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE)
        _abre(page, base, "storm123")
        page.click('.camada-btn[data-camada="plano"]')
        page.wait_for_timeout(150)
        page.click('.tf-btn[data-tf="15m"]')
        page.wait_for_function("() => _tf === '15m' && _tfPendente === null")
        page.wait_for_timeout(250)
        m = page.evaluate(_LE)
        assert m["tf"] == "15m" and m["veredito"] == "1d", m
        assert "exploratório" in m["carimbo"].lower(), ("o gráfico se declara", m)
        assert "diário" in m["vtf"].lower(), ("o carimbo do veredito continua", m["vtf"])
        tarja = page.inner_text("#setupCards")
        assert "EXPLORATÓRIO" in tarja.upper(), tarja[:200]
        browser.close()


# ─────────────── duas leituras juntas: comparação, nada anônimo ───────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [DESKTOP, TELEFONE], ids=["desktop", "telefone"])
def test_as_duas_leituras_juntas_e_nada_sem_dono(base, viewport):
    """Ligar as duas é o VALOR (comparação). O que não pode é o que gerou o print:
    dois stops a 0,39 um do outro sem etiqueta de família."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre(page, base, "storm123")
        page.click('.camada-btn[data-camada="plano"]')
        page.wait_for_timeout(250)
        m = page.evaluate(_LE)
        stops = [z for z in m["zonas"] if "stop" in z]
        assert len(stops) == 2, m["zonas"]
        assert any(z.startswith("Setup123 ") for z in stops), stops
        assert any(z.startswith("Storm123 ") for z in stops), stops
        for z in m["zonas"]:
            assert z.startswith("Setup123 · ") or z.startswith("Storm123 "), z
        # os PONTOS numerados também se identificam (a colisão de numeração)
        assert "Setup123 1-2-3" in m["legenda"], m["legenda"]
        assert "Storm123 1-2-3" in m["legenda"], m["legenda"]
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_as_medias_sao_escolha_separada_e_a_do_eden_acompanha_o_storm(base):
    """Duas perguntas diferentes: quais LEITURAS e quais MÉDIAS. A EMA 80 é exceção
    declarada — ela é metade do filtro Éden, e um veto que não se confere não é veto."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, "storm123")
        m = page.evaluate(_LE)
        assert m["medias"] == {"ma": [], "ema": ["8", "80"]}, m["medias"]

        page.click('.camada-btn[data-camada="mms"]')
        page.wait_for_timeout(150)
        com = page.evaluate(_LE)
        assert com["medias"]["ma"] == ["20", "200", "50"], com["medias"]
        assert "MMS20" in com["legenda"] and "MMS200" in com["legenda"], com["legenda"]

        page.click('.camada-btn[data-camada="emas"]')
        page.wait_for_timeout(150)
        tudo = page.evaluate(_LE)
        assert tudo["medias"]["ema"] == ["21", "50", "8", "80"], tudo["medias"]
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_seletor_nao_estoura_a_tela_do_telefone(base):
    """DA-062: quatro botões e dois rótulos de grupo num viewport de 390px."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE)
        _abre(page, base, "storm123")
        m = page.evaluate("""() => {
          const el = document.getElementById('camadasSelector');
          return {cortados: [...el.querySelectorAll('*')]
                    .filter(e => e.scrollWidth > e.clientWidth + 1)
                    .map(e => (e.innerText || '').slice(0, 30)),
                  rola: document.documentElement.scrollWidth >
                        document.documentElement.clientWidth,
                  visivel: el.getBoundingClientRect().height > 0};
        }""")
        assert m["visivel"], m
        assert m["cortados"] == [], m["cortados"]
        assert not m["rola"], m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_etiqueta_CURTA_do_grafico_tambem_se_nomeia(base):
    """No telefone o gráfico desenha a etiqueta CURTA — ela existe pra caber em ~300px.
    Se só a longa se nomeasse, a tela onde a poluição dói mais continuaria com faixa
    anônima: "recuo MMS50 (inativa) 470,00" sem dizer de qual método é."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE)
        _abre(page, base, "storm123")
        page.click('.camada-btn[data-camada="plano"]')
        page.wait_for_timeout(250)
        curtas = page.evaluate(
            """() => planZones(document.getElementById('priceChart')._actionable)
                     .map(z => z.tagCurto).filter(Boolean)""")
        assert curtas, "alguma faixa tem forma curta"
        for c in curtas:
            assert c.startswith("Setup123") or c.startswith("Storm123"), c
        # e o que foi realmente PINTADO no canvas carrega o nome
        pintados = page.evaluate(
            """() => JSON.parse(document.getElementById('priceChart').dataset.levelLabels || '[]')""")
        assert pintados, pintados
        for t in pintados:
            assert t.startswith("Setup123") or t.startswith("Storm123"), (t, pintados)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_grafico_desenhado_fora_do_fluxo_de_abrir_analise_nao_sai_em_branco(base):
    """DENTE nascido desta task: o estado das camadas começava vazio, e qualquer
    desenho que não passasse por ``watchRun`` — o CONFRONTO é o caso real — saía sem
    faixa nenhuma e com a legenda vazia. Conjunto NÃO inicializado (`null`) é
    diferente de conjunto vazio (ele desligou tudo), e só o primeiro cai no padrão."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, "setup123")
        m = page.evaluate("""() => {
          _camadas = null;                    // estado como nasce, sem análise aberta
          const z = planZones(document.getElementById('priceChart')._actionable);
          const med = mediasVisiveis(document.getElementById('priceChart')._actionable);
          return {zonas: z.length, ma: [...med.ma].length};
        }""")
        assert m["zonas"] > 0, ("gráfico em branco por estado não inicializado", m)
        assert m["ma"] > 0, m
        # e desligar TUDO na tela continua sendo respeitado (vazio ≠ não inicializado)
        page.click('.camada-btn[data-camada="plano"]')
        page.wait_for_timeout(200)
        assert page.evaluate(
            "() => planZones(document.getElementById('priceChart')._actionable).length") == 0
        browser.close()

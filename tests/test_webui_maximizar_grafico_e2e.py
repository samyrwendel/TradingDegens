"""MAXIMIZAR O GRÁFICO (DA-134).

*"quero uma opção de maximizar o gráfico pra eu ver melhor os detalhes."* O canvas
tem **380px fixos**, e essa é a causa comum de três queixas do mesmo dia: o alvo
caindo fora do enquadramento, *"12 das 15 marcas de recuo fora"*, e a dica de zoom
comendo proporção grande do desenho.

**Sobreposição em CSS, não Fullscreen API**, e o motivo é onde o ganho é maior: no
iPhone o Safari não abre elemento não-vídeo em tela cheia — a API seria o recurso
falhando calado justamente no aparelho de 390px. A sobreposição dá a mesma área
menos a barra do navegador, funciona igual em todo lugar, e **mantém os controles**
do card (frame, camadas, legenda), porque maximizar não pode custar o que faz o
gráfico valer.

O que cada teste guarda:

* a área de desenho CRESCE de verdade — medida em pixels, não em classe;
* **zoom e deslocamento sobrevivem** à ida e à volta (é o estado que o usuário
  construiu com a mão);
* o canvas é REDESENHADO na dimensão nova (senão é borrão, não gráfico maior);
* **Esc volta**, e o botão de voltar continua visível no modo maximizado;
* o telefone ganha a tela inteira;
* o ícone é vetor, nunca emoji.
"""

import pytest

from tests.test_webui_frame_e_cor_e2e import (
    DESKTOP,
    TELEFONE,
    _abre,
    sobe_servidor,
    sync_playwright,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def base(tmp_path):
    yield from sobe_servidor(tmp_path)


_ESTADO = """() => {
  const card = document.getElementById('chartCard');
  const cv = document.getElementById('priceChart');
  const btn = document.getElementById('chartMaxBtn');
  const r = cv.getBoundingClientRect();
  const vis = (e) => !!(e && e.getClientRects().length);
  return {
    max: card.classList.contains('is-max'),
    w: Math.round(r.width), h: Math.round(r.height),
    area: Math.round(r.width * r.height),
    bufW: cv.width, bufH: cv.height,
    btnVisivel: vis(btn), pressed: btn.getAttribute('aria-pressed'),
    title: btn.title,
    icone: getComputedStyle(btn).backgroundImage,
    texto: btn.textContent,
    view: cv._view ? {v0: cv._view.v0, v1: cv._view.v1} : null,
    vview: cv._vview ? {lo: cv._vview.lo, hi: cv._vview.hi} : null,
    tf: cv._tf,
    // os controles continuam na tela: maximizar não pode custá-los
    temTf: vis(document.getElementById('tfSelector')),
    temLegenda: vis(document.getElementById('chartLegend')),
    scrollTravado: document.body.classList.contains('chart-max-aberto'),
  };
}"""


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_maximizar_CRESCE_a_area_de_desenho_de_verdade(base):
    """Medido em pixels: a classe podia estar lá e o canvas continuar de 380px."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        antes = page.evaluate(_ESTADO)
        assert antes["max"] is False, antes
        # a altura normal varia com a largura da tela (380 · 500 · 560), então o que
        # se crava é o CRESCIMENTO, não o número de partida
        assert 300 <= antes["h"] <= 600, ("altura de partida inesperada", antes)

        page.click("#chartMaxBtn")
        page.wait_for_timeout(250)
        dep = page.evaluate(_ESTADO)
        assert dep["max"] is True, dep
        # a área quase DOBRA; a altura sozinha cresce menos porque numa tela larga
        # ela já partia de 560px — quem completa o ganho é a largura, que deixa de
        # dividir a fileira com o painel de teses.
        assert dep["area"] > antes["area"] * 1.8, ("cresceu pouco", antes, dep)
        assert dep["h"] > antes["h"] * 1.25, (antes["h"], dep["h"])
        assert dep["w"] > antes["w"], ("a largura também tem de crescer", antes, dep)
        # e os controles do gráfico continuam ali
        assert dep["temTf"] and dep["temLegenda"], dep
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_canvas_e_REDESENHADO_na_dimensao_nova(base):
    """DENTE: sem redesenhar, o navegador ESTICA a resolução antiga — o resultado é
    borrão, não gráfico maior. O buffer do canvas tem de acompanhar."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        antes = page.evaluate(_ESTADO)
        page.click("#chartMaxBtn")
        page.wait_for_timeout(250)
        dep = page.evaluate(_ESTADO)
        assert dep["bufH"] > antes["bufH"], ("o buffer não acompanhou", antes, dep)
        # a proporção buffer/CSS é a mesma (nitidez preservada, não esticada)
        assert abs(dep["bufH"] / dep["h"] - antes["bufH"] / antes["h"]) < 0.05, (antes, dep)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_ZOOM_e_DESLOCAMENTO_sobrevivem_a_ida_e_a_volta(base):
    """É o estado que o usuário construiu com a mão. Resetá-lo ao maximizar faria
    o recurso custar exatamente o trabalho que ele veio facilitar."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        # zoom real, pelo caminho do usuário (roda sobre o canvas)
        page.evaluate("""() => {
          const cv = document.getElementById('priceChart');
          const r = cv.getBoundingClientRect();
          cv.dispatchEvent(new WheelEvent('wheel', {deltaY: -300, bubbles: true,
            cancelable: true, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2}));
        }""")
        page.wait_for_timeout(150)
        z = page.evaluate(_ESTADO)
        assert z["view"], "o zoom não pegou — o teste não provaria nada"

        page.click("#chartMaxBtn")
        page.wait_for_timeout(250)
        m = page.evaluate(_ESTADO)
        assert m["max"] and m["view"] == z["view"], ("resetou ao maximizar", z, m)

        page.click("#chartMaxBtn")
        page.wait_for_timeout(250)
        v = page.evaluate(_ESTADO)
        assert v["max"] is False and v["view"] == z["view"], ("resetou ao voltar", z, v)
        assert v["h"] == z["h"], ("não voltou ao tamanho normal", z["h"], v["h"])
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_ESC_volta_e_o_botao_continua_VISIVEL_maximizado(base):
    """Esc é o gesto que quem abriu uma sobreposição já tenta primeiro. E o caminho
    de volta pelo mouse não pode sumir junto com o resto da tela."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        page.click("#chartMaxBtn")
        page.wait_for_timeout(200)
        m = page.evaluate(_ESTADO)
        assert m["btnVisivel"] and m["pressed"] == "true", m
        assert "Restaurar" in m["title"] and "Esc" in m["title"], m["title"]
        assert m["scrollTravado"] is True, "a página atrás continuava rolando"

        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
        v = page.evaluate(_ESTADO)
        assert v["max"] is False and v["pressed"] == "false", v
        assert v["scrollTravado"] is False, v
        assert "Maximizar" in v["title"], v["title"]
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_icone_e_VETOR_e_troca_de_sentido(base):
    """DA-078 regra 7 + DA-076: SVG de traço fino, nunca emoji. E o ícone mostra o
    que o clique FAZ — setas saindo pra maximizar, entrando pra restaurar."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        a = page.evaluate(_ESTADO)
        assert "svg" in a["icone"].lower() and "stroke" in a["icone"], a["icone"][:80]
        assert a["texto"].strip() == "", ("o botão não carrega texto nem emoji", a["texto"])
        page.click("#chartMaxBtn")
        page.wait_for_timeout(200)
        b = page.evaluate(_ESTADO)
        assert b["icone"] != a["icone"], "o ícone não mudou de sentido"
        assert "stroke" in b["icone"], b["icone"][:80]
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_no_TELEFONE_maximizado_usa_a_tela_INTEIRA(base):
    """É onde o ganho é maior (390×844) — e é onde a Fullscreen API não abriria."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE)
        _abre(page, base, viewport=TELEFONE)
        antes = page.evaluate(_ESTADO)
        page.click("#chartMaxBtn")
        page.wait_for_timeout(300)
        dep = page.evaluate(_ESTADO)
        assert dep["max"] and dep["area"] > antes["area"] * 1.4, (antes, dep)
        # largura da tela inteira (o card sai da coluna e vira sobreposição)
        assert dep["w"] >= TELEFONE["width"] - 40, (dep["w"], TELEFONE["width"])
        assert dep["h"] > antes["h"], (antes["h"], dep["h"])
        # e continua com o seletor de frame: maximizar não custa os controles
        assert dep["temTf"], dep
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_altura_e_MEDIDA_e_nao_uma_conta_fixa(base):
    """O card leva avisos que aparecem e somem; qualquer constante erraria quando um
    deles mudasse de estado. DENTE: com um aviso a mais, o canvas encolhe — e o
    conjunto continua cabendo na tela sem rolar."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        page.click("#chartMaxBtn")
        page.wait_for_timeout(250)
        base_h = page.evaluate(_ESTADO)["h"]
        # acende um aviso que costuma estar apagado e remede
        page.evaluate("""() => {
          const el = document.getElementById('chartDegrade');
          el.classList.remove('hidden');
          el.textContent = 'aviso de teste ocupando uma linha inteira do card';
          ajustaAlturaMaximizada();
        }""")
        page.wait_for_timeout(200)
        depois = page.evaluate(_ESTADO)
        assert depois["h"] < base_h, ("a altura não reagiu ao aviso", base_h, depois["h"])
        cabe = page.evaluate("""() => {
          const c = document.getElementById('chartCard');
          return c.scrollHeight <= c.clientHeight + 24;
        }""")
        assert cabe, "o card maximizado passou a exigir rolagem"
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_titulo_sai_da_VISTA_mas_nao_do_DOCUMENTO(base):
    """"Gráfico de candles (candlestick) com faixas do plano" quebra em três linhas
    num telefone de 390px e come 75px do que a pessoa pediu pra ver — e numa tela
    inteira ocupada pelo gráfico ele é redundante com o próprio objeto.

    DENTE dos dois lados: some da vista, mas continua no documento (leitor de tela e
    a estrutura de cabeçalhos não perdem nada), e VOLTA ao restaurar."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE)
        _abre(page, base, viewport=TELEFONE)
        _le = """() => {
          const t = document.querySelector('#chartCard .section-title');
          const r = t.getBoundingClientRect();
          return {texto: t.textContent.trim(), altura: Math.round(r.height),
                  display: getComputedStyle(t).display};
        }"""
        a = page.evaluate(_le)
        assert a["altura"] > 20, a
        page.click("#chartMaxBtn")
        page.wait_for_timeout(300)
        m = page.evaluate(_le)
        assert m["altura"] <= 1, ("o título continuou comendo altura", m)
        assert m["texto"] == a["texto"], "o texto sumiu do documento"
        assert m["display"] != "none", ("`display: none` some do leitor de tela", m)
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        v = page.evaluate(_le)
        assert v["altura"] > 20, ("o título não voltou", v)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_so_o_que_esta_ACIMA_do_grafico_desconta_altura(base):
    """E "acima" é medido NA TELA, não na ordem do HTML: no telefone a legenda é
    irmã ANTERIOR ao gráfico no documento e aparece DEPOIS dele (task 020, `order`).
    Percorrer o DOM descontava dela como se estivesse em cima — 475px de 844 no
    aparelho onde o ganho devia ser maior."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE)
        _abre(page, base, viewport=TELEFONE)
        page.click("#chartMaxBtn")
        page.wait_for_timeout(350)
        m = page.evaluate("""() => {
          const card = document.getElementById('chartCard');
          const cv = document.getElementById('priceChart');
          const wrap = cv.closest('.chart-wrap');
          const topo = wrap.getBoundingClientRect().top;
          const leg = document.getElementById('chartLegend');
          return {
            legendaDepoisNaTELA: leg.getBoundingClientRect().top > topo,
            legendaAntesNoDOM: !!(leg.compareDocumentPosition(wrap) &
                                  Node.DOCUMENT_POSITION_FOLLOWING),
            canvas: Math.round(cv.getBoundingClientRect().height),
            tela: window.innerHeight,
          };
        }""")
        # a premissa do teste: os dois eixos DISCORDAM aqui — é o que torna o caso real
        assert m["legendaAntesNoDOM"] and m["legendaDepoisNaTELA"], m
        # e o canvas fica com a maior parte da tela, não com metade
        assert m["canvas"] > m["tela"] * 0.7, ("o canvas ficou pequeno demais", m)
        browser.close()

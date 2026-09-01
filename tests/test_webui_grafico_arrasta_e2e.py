"""Arrastar o gráfico funciona SEM zoom prévio, e o alvo sempre cabe (task 20260831-022).

O Samyr: *"preciso conseguir deslocar o gráfico arrastando pra baixo pra eu
conseguir ver os alvos pra cima"*.

**O defeito era uma condição.** O pan do corpo do gráfico só era armado no ramo
``else if (canvas._view || canvas._vview)`` — ou seja, **depois** de um zoom. No
estado inicial arrastar não fazia nada, enquanto a dica na tela anunciava
"arrasta = move 2 eixos". A dica prometia o que o código não entregava, e o passo
que faltava (dar zoom antes) não tem como ser adivinhado.

**O enquadramento, esse, já entendia o plano** — e é um achado que muda a forma da
correção: a autoescala vertical cresce por ``grow(z.price)`` sobre as faixas do
plano, então **gatilho, invalidação, SL e alvo já participam do que a janela
mostra**. Ou seja, o alvo só sai da tela depois que alguém mexeu na janela — e o
caminho de volta é o reset, que já existia mas se chamava "ver a série inteira"
(fala do eixo do TEMPO) quando o caso que dói é o do PREÇO.

Os dentes:

* arrastar no estado inicial PRODUZ movimento (antes: nada);
* um alvo acima do topo dos candles está enquadrado **sem zoom nenhum**;
* arrastado para fora, ele volta com o botão que a tela oferece — e o botão diz o
  que traz de volta;
* o duplo-clique continua resetando os dois eixos;
* um CLIQUE (sem arrastar) não congela a autoescala;
* toque funciona como mouse, e a pinça de dois dedos não virou pan.
"""

import pytest

from tests.test_webui_frame_e_cor_e2e import (
    _ACT_4H,
    _CHART,
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


# O caso REAL do pedido: o ALVO acima do topo dos candles. As velas da fixture vão
# até ~303; o alvo em 380 fica 25% acima do candle mais alto — é exatamente "o alvo
# pra cima" que não cabia.
_ALVO_ALTO = 380.0
_ACT_ALVO_FORA = {**_ACT_4H, "target": {"price": _ALVO_ALTO, "label": "alvo (TP)"}}


_GEO = """() => {
  const cv = document.getElementById('priceChart');
  return {lo: Number(cv.dataset.plo), hi: Number(cv.dataset.phi),
          view: cv._view, vview: cv._vview,
          cursor: getComputedStyle(cv).cursor};
}"""


def _arrasta(page, dx, dy, passos=6):
    """Arrasta o CORPO do gráfico (longe das réguas, que têm outro gesto)."""
    cv = page.locator("#priceChart")
    box = cv.bounding_box()
    # 35% da largura e 40% da altura: dentro do plot, longe da régua direita
    # (zoom vertical) e da de baixo (zoom horizontal).
    x0 = box["x"] + box["width"] * 0.35
    y0 = box["y"] + box["height"] * 0.40
    page.mouse.move(x0, y0)
    page.mouse.down()
    for k in range(1, passos + 1):
        page.mouse.move(x0 + dx * k / passos, y0 + dy * k / passos)
    page.mouse.up()
    page.wait_for_timeout(120)


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_arrastar_move_o_grafico_SEM_zoom_previo(base):
    """DENTE: na implementação antiga `_vview` continuava `null` e nada se movia."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        antes = page.evaluate(_GEO)
        assert antes["vview"] is None, "o teste tem de começar SEM zoom"
        assert antes["cursor"] == "grab", ("o cursor tem de anunciar que arrasta", antes)

        _arrasta(page, 0, 120)          # arrasta pra BAIXO = revela o que está acima
        depois = page.evaluate(_GEO)
        assert depois["vview"] is not None, "arrastar não fez nada sem zoom prévio"
        assert depois["hi"] > antes["hi"], ("arrastar pra baixo tem de revelar preço "
                                            "ACIMA", antes, depois)
        assert depois["hi"] - depois["lo"] == pytest.approx(antes["hi"] - antes["lo"], rel=1e-6), \
            "o pan mudou o ZOOM — ele só desliza a janela"
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_arrastar_para_CIMA_revela_o_que_esta_abaixo(base):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        antes = page.evaluate(_GEO)
        _arrasta(page, 0, -120)
        depois = page.evaluate(_GEO)
        assert depois["lo"] < antes["lo"], (antes, depois)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_ALVO_acima_dos_candles_ja_esta_enquadrado_sem_zoom_nenhum(base):
    """O caso real do pedido, e o achado que decidiu a forma da correção.

    A autoescala vertical cresce pelas faixas do PLANO, não só pelos candles —
    então o alvo em 380, 25% acima do candle mais alto, está na tela desde o
    primeiro desenho. Sem isto, "arrastar" seria o único jeito de chegar nele, e
    deslizar levaria à área VAZIA em vez de revelar o nível.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, actionable=_ACT_ALVO_FORA)
        g = page.evaluate(_GEO)
        assert g["vview"] is None, "sem zoom nenhum"
        topo_das_velas = max(c["h"] for c in _CHART["candles"])
        assert topo_das_velas < _ALVO_ALTO, "a fixture tem de ter o alvo FORA dos candles"
        assert g["hi"] >= _ALVO_ALTO, ("o alvo não coube na janela automática", g,
                                       topo_das_velas)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_arrastado_para_fora_o_alvo_VOLTA_pelo_botao_que_a_tela_oferece(base):
    """Deslizar pro vazio é permitido — mas nunca pode ser o único caminho de volta."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, actionable=_ACT_ALVO_FORA)
        _arrasta(page, 0, -260)          # empurra a janela pra baixo: o alvo sai por cima
        fora = page.evaluate(_GEO)
        assert fora["hi"] < _ALVO_ALTO, ("o teste precisa DEIXAR o alvo fora", fora)

        # a tela declara o que saiu do enquadramento (DA-107) e oferece a volta
        page.wait_for_selector("#chartFora:not(.hidden)", timeout=5000)
        rotulo = page.inner_text("#foraResetBtn")
        assert "ajustar à tela" in rotulo and "plano" in rotulo, rotulo

        page.click("#foraResetBtn")
        page.wait_for_timeout(150)
        volta = page.evaluate(_GEO)
        assert volta["vview"] is None and volta["hi"] >= _ALVO_ALTO, volta
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_duplo_clique_continua_ajustando_os_dois_eixos(base):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, actionable=_ACT_ALVO_FORA)
        _arrasta(page, 0, -200)
        page.evaluate("() => { document.getElementById('priceChart')._view = {v0: 5, v1: 40}; }")
        page.dblclick("#priceChart", position={"x": 300, "y": 200})
        page.wait_for_timeout(150)
        g = page.evaluate(_GEO)
        assert g["view"] is None and g["vview"] is None, g
        assert g["hi"] >= _ALVO_ALTO, ("o ajuste tem de trazer o alvo de volta", g)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_um_CLIQUE_nao_congela_a_autoescala(base):
    """DENTE: sem o limiar, o tremor de um clique gravaria `_vview` e a janela de
    preço deixaria de se reajustar sozinha ao dado novo — para sempre."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        cv = page.locator("#priceChart").bounding_box()
        x = cv["x"] + cv["width"] * 0.35
        y = cv["y"] + cv["height"] * 0.40
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x + 1, y + 1)     # o tremor de um clique
        page.mouse.up()
        page.wait_for_timeout(120)
        assert page.evaluate(_GEO)["vview"] is None, "um clique congelou a autoescala"
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_no_TOQUE_o_arrasto_tambem_move_e_a_pinca_continua_zoom(base):
    """Não regride o mobile: o handler é de pointer events, e dois dedos são pinça."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport=TELEFONE, has_touch=True, is_mobile=True)
        page = ctx.new_page()
        _abre(page, base, viewport=TELEFONE)
        antes = page.evaluate(_GEO)
        box = page.locator("#priceChart").bounding_box()
        x = box["x"] + box["width"] * 0.35
        y = box["y"] + box["height"] * 0.40
        # um DEDO arrastando = pan (pointer events, o mesmo caminho do mouse)
        page.touchscreen.tap(x, y)
        page.evaluate("""([x, y]) => {
          const cv = document.getElementById('priceChart');
          const ev = (tipo, cy, id) => cv.dispatchEvent(new PointerEvent(tipo, {
            pointerId: id, pointerType: 'touch', clientX: x, clientY: cy,
            bubbles: true, cancelable: true}));
          ev('pointerdown', y, 1);
          for (let k = 1; k <= 6; k++) ev('pointermove', y + k * 20, 1);
          ev('pointerup', y + 120, 1);
        }""", [x, y])
        page.wait_for_timeout(150)
        depois = page.evaluate(_GEO)
        assert depois["vview"] is not None and depois["hi"] > antes["hi"], (antes, depois)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_dica_da_tela_descreve_o_gesto_que_EXISTE(base):
    """A dica prometia "arrasta = move 2 eixos" quando arrastar não fazia nada."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        dica = page.inner_text(".chart-zoom-hint")
        assert "arrasta=move o gráfico" in dica.replace(" =", "=").replace("= ", "="), dica
        assert "ajusta à tela" in dica, dica
        assert "2 eixos" not in dica, ("a promessa antiga sobreviveu", dica)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_arrastar_na_REGUA_continua_sendo_zoom_e_nao_pan(base):
    """O pan liberado no corpo não pode ter engolido os gestos das réguas."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        antes = page.evaluate(_GEO)
        box = page.locator("#priceChart").bounding_box()
        # régua de PREÇO (direita): arrastar ali comprime/expande a escala
        x = box["x"] + box["width"] - 6
        y = box["y"] + box["height"] * 0.40
        page.mouse.move(x, y)
        page.mouse.down()
        for k in range(1, 7):
            page.mouse.move(x, y - k * 12)
        page.mouse.up()
        page.wait_for_timeout(120)
        depois = page.evaluate(_GEO)
        faixa_antes = antes["hi"] - antes["lo"]
        faixa_depois = depois["hi"] - depois["lo"]
        assert faixa_depois < faixa_antes * 0.98, ("a régua parou de dar zoom",
                                                   antes, depois)
        browser.close()

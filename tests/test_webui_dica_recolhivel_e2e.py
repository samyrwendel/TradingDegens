"""A ajuda dos gestos se recolhe, e a decisão fica lembrada (task 20260831-027).

*"dá pra recolher esse texto ou ocultar com o olhinho?"* — com print da caixa por
cima do **ponto 1 do padrão**. É ajuda permanente ocupando área de DADO: depois de
aprendido o gesto, ela só tapa a estrutura que o gráfico existe pra mostrar.

As escolhas, e cada uma tem um teste:

* **aberta por padrão** — ela ensina o gesto a quem chega; quem recolhe é que
  decidiu;
* **recolhida sobra o ÍCONE**, no mesmo lugar — sumir de vez faria a ajuda deixar
  de existir pra quem esquecesse o gesto, e afordância de volta escondido é o
  mesmo que nenhum;
* **a decisão é lembrada** (localStorage, a mesma disciplina da largura da lateral)
  — recolher não pode virar tarefa a refazer toda visita;
* **o ícone é VETOR** de traço fino (DA-078 regra 7), desenhado em SVG — nunca
  emoji (DA-076), então nada de "olhinho" em pictograma;
* **no telefone a caixa INTEIRA some** (a dica é de mouse): um botão sozinho, sem
  o texto que ele esconde, seria um controle que não faz nada visível.
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
  const box = document.getElementById('chartHintBox');
  const hint = document.getElementById('chartZoomHint');
  const btn = document.getElementById('chartHintBtn');
  const vis = (e) => !!(e && e.getClientRects().length);
  return {
    caixa: vis(box), dica: vis(hint), botao: vis(btn),
    off: box ? box.classList.contains('is-off') : null,
    expanded: btn ? btn.getAttribute('aria-expanded') : null,
    title: btn ? btn.title : null,
    guardado: localStorage.getItem('td_chart_hint'),
    icone: btn ? getComputedStyle(btn).backgroundImage : "",
    texto: hint ? hint.textContent : "",
  };
}"""


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_dica_nasce_ABERTA_e_recolhe_num_clique(base):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        a = page.evaluate(_ESTADO)
        assert a["dica"] and a["botao"] and a["off"] is False, a
        assert a["expanded"] == "true" and "Esconder" in a["title"], a
        # o texto é o da DA-122: ele descreve o gesto que existe
        assert "arrasta = move o gráfico" in a["texto"], a

        page.click("#chartHintBtn")
        b = page.evaluate(_ESTADO)
        assert b["dica"] is False, "a dica não recolheu"
        assert b["botao"] is True, ("recolhida tem de sobrar o ícone — sem ele a "
                                    "ajuda deixa de existir", b)
        assert b["expanded"] == "false" and "Mostrar" in b["title"], b
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_decisao_de_recolher_SOBREVIVE_a_visita_seguinte(base):
    """DENTE: sem persistir, recolher vira uma tarefa a refazer toda vez."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        page.click("#chartHintBtn")
        assert page.evaluate(_ESTADO)["guardado"] == "off"

        _abre(page, base)                      # nova visita (recarrega a página)
        m = page.evaluate(_ESTADO)
        assert m["off"] is True and m["dica"] is False, ("a preferência não sobreviveu", m)

        page.click("#chartHintBtn")            # e volta, também lembrado
        _abre(page, base)
        m2 = page.evaluate(_ESTADO)
        assert m2["off"] is False and m2["dica"] is True, m2
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_icone_e_VETOR_e_nao_emoji(base):
    """DA-078 regra 7 (ícone é vetor monocromático de traço fino) + DA-076 (sem
    emoji). DENTE: um "olhinho" em pictograma passaria no olho e reprovaria na regra."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        m = page.evaluate(_ESTADO)
        assert "svg" in m["icone"].lower(), ("o ícone tem de ser SVG", m["icone"][:80])
        assert "stroke" in m["icone"], ("traço fino, não preenchimento", m["icone"][:80])
        # nenhum caractere de pictograma no botão
        txt = page.evaluate("() => document.getElementById('chartHintBtn').textContent")
        assert txt.strip() == "", ("o botão não carrega texto nem emoji", txt)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_recolhida_a_dica_NAO_deixa_rastro_sobre_os_candles(base):
    """O motivo do pedido: a caixa tapava o ponto 1. Recolhida, o que sobra tem de
    ser pequeno o bastante pra não competir com o dado."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        antes = page.evaluate("""() => {
          const r = document.getElementById('chartHintBox').getBoundingClientRect();
          return r.width * r.height;
        }""")
        page.click("#chartHintBtn")
        depois = page.evaluate("""() => {
          const r = document.getElementById('chartHintBox').getBoundingClientRect();
          return r.width * r.height;
        }""")
        assert depois < antes * 0.15, ("recolher tem de devolver a área ao gráfico",
                                       antes, depois)
        assert depois < 700, ("o que sobra é um ícone, não uma caixa", depois)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_no_TELEFONE_a_caixa_inteira_some(base):
    """A dica é de MOUSE (roda, régua, arrasta). No telefone ela já não aparecia; o
    botão tem de sumir junto — controle sem o texto que esconde não faz nada."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE)
        _abre(page, base, viewport=TELEFONE)
        m = page.evaluate(_ESTADO)
        assert m["caixa"] is False and m["botao"] is False, m
        browser.close()

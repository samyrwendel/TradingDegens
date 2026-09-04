"""Vocabulário canônico da tela: FONTE e DIREÇÃO no veredito, R:R fora do morto,
direção+frame na badge de vigilância (task 20260904-002, DA-190).

Três defeitos do print do AVGO (Samyr, 03/09 19:50):

1. **Cabeçalho ambíguo.** "AUMENTAR · Overweight · veredito no Diário" — "AUMENTAR"
   é o rating dos ANALISTAS (Overweight), não o Setup123; e sozinho não diz aumentar
   O QUÊ nem em que direção. Overweight/Underweight são ajustes de uma posição
   COMPRADA. O cabeçalho passa a dizer a FONTE e a DIREÇÃO.
2. **R:R num trade morto.** Um Setup123 encerrado no alvo ainda imprimia
   "risco/retorno · risco 55,55 · retorno 4,78" a preço atual — a conta de um trade
   que já terminou. O morto mostra o DESFECHO, não o R:R.
3. **Badge muda.** As badges de vigilância dos setups de VENDA apareciam como
   "gatilho tocado 366,84" sem a palavra "venda" nem o frame — ao lado do veredito
   COMPRADO dos analistas, liam-se como sinal do mesmo lado.
"""

import pytest

from tests.test_webui_frame_e_cor_e2e import DESKTOP, sobe_servidor
from tests.test_webui_fantasma_do_setup_inteiro_e2e import _abre

pytestmark = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None


@pytest.fixture
def base(tmp_path):
    yield from sobe_servidor(tmp_path)


_CARD_123 = """() => {
  const c = document.querySelector('.setup-card.sc-123');
  return c ? c.innerText.replace(/\\s+/g, ' ') : '';
}"""


# ── (a) O CABEÇALHO diz FONTE e DIREÇÃO ────────────────────────────────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_cabecalho_do_veredito_nunca_mostra_acao_sem_direcao(base):
    """"AUMENTAR"/"REDUZIR" sozinhos não dizem DE QUÊ — Overweight/Underweight são
    ajustes de uma posição COMPRADA. O cabeçalho rotula a FONTE (analistas) e a
    DIREÇÃO, senão o rating do analista se lê como sinal do Setup123."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, ciclo_123="vivo")
        got = page.evaluate("""() => ({
          ow: analistaVerdictHtml('Overweight'),
          uw: analistaVerdictHtml('Underweight'),
          buy: analistaVerdictHtml('Buy'),
          sell: analistaVerdictHtml('Sell'),
          hold: analistaVerdictHtml('Hold'),
        })""")
        for k, v in got.items():
            assert "ANALISTAS" in v, (k, v)                       # fonte sempre declarada
        # Overweight/Underweight NUNCA sem a direção da posição
        assert "AUMENTAR POSIÇÃO COMPRADA" in got["ow"], got["ow"]
        assert "REDUZIR POSIÇÃO COMPRADA" in got["uw"], got["uw"]
        assert "(Overweight)" in got["ow"], got["ow"]            # rating inglês ao lado
        # DENTE: nunca a forma ambígua "AUMENTAR (Overweight)" — a ação vem SEMPRE
        # colada à direção da posição.
        assert "AUMENTAR (" not in got["ow"], ("ação sem direção no cabeçalho", got["ow"])
        assert "REDUZIR (" not in got["uw"], ("ação sem direção no cabeçalho", got["uw"])
        browser.close()


# ── (b) SETUP ENCERRADO não imprime R:R ────────────────────────────────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_card_do_Setup123_encerrado_nao_imprime_R_R(base):
    """Um trade que chegou ao alvo TERMINOU: a conta de risco/retorno a preço atual
    seria de um trade que já fechou. O card mostra o DESFECHO, não o R:R."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, ciclo_123="concluido_alvo")
        txt = page.evaluate(_CARD_123)
        assert "ENCERRADO NO ALVO" in txt, txt
        assert "risco/retorno" not in txt, ("R:R vivo num trade encerrado", txt)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_Setup123_VIVO_continua_imprimindo_R_R(base):
    """DENTE do exagero oposto: calar o R:R de todo mundo apagaria o número que diz
    se o setup VIVO vale o risco (invariante "o R:R nunca some")."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, ciclo_123="vivo")
        txt = page.evaluate(_CARD_123)
        assert "risco/retorno" in txt, ("o setup vivo perdeu o R:R", txt)
        browser.close()


# ── (c) BADGE de vigilância carrega direção + frame ────────────────────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_badge_de_vigilancia_carrega_direcao_e_frame(base):
    """Um "gatilho tocado 366,84" sem "venda 1h" ao lado do veredito COMPRADO dos
    analistas se lê como sinal do mesmo lado — e não é. A cor segue a DIREÇÃO."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, ciclo_123="vivo")
        got = page.evaluate("""() => {
          registraVigilancia([{ticker:'AVGO', nivel:'gatilho', preco_nivel:366.84,
            direcao:'venda', frame:'1h', metodo:'Setup123',
            quando:'2026-09-03T19:43:00-04:00', texto:'gatilho perdido'}]);
          const html = vigilanciaHtml('AVGO');
          const d = document.createElement('div'); d.innerHTML = html;
          const i = d.querySelector('.vig-i');
          return { text: i ? i.innerText.replace(/\\s+/g,' ') : '',
                   cls: i ? i.className : '' };
        }""")
        assert "venda" in got["text"] and "1h" in got["text"], got
        assert "gatilho" in got["text"] and "366,84" in got["text"] and "tocado" in got["text"], got
        # DENTE: sem a direção a badge falha — a classe de cor por direção tem de estar
        assert "vig-dir-venda" in got["cls"], ("badge sem direção", got["cls"])
        browser.close()

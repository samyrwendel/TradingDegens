"""O STORM ENCERRADO na tela — o quarto estado (DA-129).

Samyr: *"se já alcançou o alvo e voltou o setup continua válido? ou ele vira
história?"* A DA-126 já dava ao Storm a régua do desfecho; o que faltava é isto: o
Storm **calculava** o desfecho e não o mostrava em superfície nenhuma.

Com o Éden desalinhado — que é o caso comum quando o preço já andou e voltou — o
card dizia **"NÃO OPERA · qualidade ruim"** sobre um trade que tinha fechado NO
ALVO horas antes. O filtro do Éden responde *"vale a pena entrar agora?"*, e não
há entrada nenhuma a autorizar num trade que já terminou.

Precedência que fica: **encerrado ganha de vetado e de invalidado**. O veto fala de
um setup que se poderia operar; a invalidação, de um que deixou de existir; o
encerrado, de um que TERMINOU — e nada posterior o reabre.
"""

import pytest

from tests.test_webui_frame_e_cor_e2e import DESKTOP, sobe_servidor
from tests.test_webui_storm_tres_estados_e2e import _LE, VETADO, _abre
from tests.test_webui_um_grafico_um_metodo_e2e import _STORM

pytestmark = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None


@pytest.fixture
def base(tmp_path):
    yield from sobe_servidor(tmp_path)


# COERENTE com a leitura do fixture (venda, gatilho 452,00, alvo 414,00, stop
# 497,98): o desfecho tem de ser o desta leitura, senão o card conta um trade e as
# linhas abaixo contam outro — que é justamente o defeito sendo corrigido.
_DESFECHO = {"tipo": "alvo", "em": "2026-08-28 15:00", "price": 414.0,
             "entrada_em": "2026-08-28 13:00", "entrada": 452.0,
             "empate_na_barra": False}
_PAT_FIM = {**_STORM["pattern"], "invalidado": False,
            "invalidado_em": "2026-08-28 23:00", "desfecho": _DESFECHO,
            "encerrado": True, "acionado_em": "2026-08-28 13:00",
            "ciclo": "concluido_alvo"}

# ENCERRADO *e* vetado ao mesmo tempo — a precedência tem de ser observável, e é
# justamente a combinação que aparece na vida real: o preço andou, fechou o trade e
# voltou, e o Éden já não está alinhado.
ENCERRADO_E_VETADO = {**VETADO, "pattern": _PAT_FIM}
ENCERRADO_NO_STOP = {**VETADO, "pattern": {
    **_PAT_FIM, "desfecho": {**_DESFECHO, "tipo": "stop", "price": 497.98},
    "ciclo": "concluido_stop"}}

_CARD = """() => {
  const c = document.querySelector('.setup-card.sc-storm');
  return c ? c.innerText.replace(/\\s+/g, ' ') : '';
}"""


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_card_do_Storm_DIZ_o_desfecho_em_vez_do_veto(base):
    """DENTE: "NÃO OPERA · qualidade ruim" no topo de um trade que fechou no alvo
    são dois vereditos opostos sobre a mesma coisa — e o errado é o do filtro."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, ENCERRADO_E_VETADO)
        txt = page.evaluate(_CARD)
        assert "ENCERRADO NO ALVO" in txt, txt
        assert "NÃO OPERA" not in txt, ("o filtro falou sobre história", txt)
        # e diz o que aconteceu, com números conferíveis
        assert "414" in txt and "452" in txt, txt
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_encerrado_no_STOP_tambem_e_historia(base):
    """Simétrico: perder não é "não opera" nem "invalidado" — é ter terminado."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, ENCERRADO_NO_STOP)
        txt = page.evaluate(_CARD)
        assert "ENCERRADO NO STOP" in txt and "NÃO OPERA" not in txt, txt
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_estado_do_Storm_e_encerrado_e_ganha_do_vetado(base):
    """A precedência, na função que decide: encerrado > invalidado > vetado."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, ENCERRADO_E_VETADO)
        m = page.evaluate(_LE)
        assert m["estado"] == "encerrado", m
        assert "encerrado no alvo" in m["legenda"], ("na LEGENDA", m["legenda"])
        assert "não opera" not in m["legenda"].lower(), m["legenda"]
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_encerrado_NAO_e_fantasma(base):
    """Pintar de cinza um trade que chegou ao alvo diria que ele não existiu (DA-125).
    A regra vale igual nos dois métodos — a função da cor é a mesma."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, ENCERRADO_E_VETADO)
        fantasma = page.evaluate(
            "() => ehFantasma((document.getElementById('priceChart')._actionable || {})"
            ".storm.pattern)")
        assert fantasma is False
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_vetado_VIVO_continua_dizendo_NAO_OPERA(base):
    """DENTE do exagero oposto: sem este teste, um "encerrado" mal condicionado
    calaria o veto de todo Storm — e o aviso que a task 034 pôs na tela sumiria."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, VETADO)
        txt = page.evaluate(_CARD)
        assert "NÃO OPERA" in txt and "ENCERRADO" not in txt, txt
        assert page.evaluate(_LE)["estado"] == "vetado"
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_LEITURA_nao_diz_em_formacao_num_padrao_encerrado(base):
    """A mesma contradição, um nível mais fundo: o card dizia "ENCERRADO NO ALVO" no
    topo e "em formação" três linhas abaixo. O que forma as leituras é o PADRÃO — e
    ele terminou. A que decidiu o desfecho se identifica pelo preço de entrada; a
    outra fica SEM rótulo, porque afirmar o que ela virou seria inventar."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, ENCERRADO_E_VETADO)
        m = page.evaluate("""() => [...document.querySelectorAll(
          '.setup-card.sc-storm .sc-leitura')].map(e => ({
            k: e.querySelector('.sc-lk').textContent,
            st: e.querySelector('.sc-lstate').textContent }))""")
        assert m, "o card perdeu as leituras"
        assert not any("forma" in x["st"] for x in m), m
        assert sum(1 for x in m if "encerrado no alvo" in x["st"]) == 1, (
            "só a leitura que DECIDIU o desfecho leva o rótulo", m)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_num_Storm_VIVO_a_leitura_continua_dizendo_o_seu_estado(base):
    """DENTE do exagero oposto: sem isto, calar o rótulo de todo mundo seria a
    regressão — o estado da leitura é o que diz se aquele gatilho já rompeu."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, VETADO)
        m = page.evaluate("""() => [...document.querySelectorAll(
          '.setup-card.sc-storm .sc-lstate')].map(e => e.textContent)""")
        assert any(x.strip() for x in m), ("as leituras ficaram mudas", m)
        browser.close()

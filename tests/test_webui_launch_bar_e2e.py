"""E2E da barra de controle numa linha só (task de UI 010, pedido do Samyr).

O pedido: Ativo · Data · Tempo · Método · Analisar · ↻ na MESMA linha, e o bloco
MODELOS em duas linhas (rápido em cima, pesado embaixo) contando como UM elemento —
antes a barra quebrava em três faixas e o bloco de modelos ficava pendurado à
direita, cortado.

O que este arquivo trava, e por quê: uma linha só é fácil de conseguir com
``nowrap`` e fácil de ERRAR — foi o que aconteceu na primeira tentativa, com o
``nowrap`` preso à largura do VIEWPORT. Arrastar o resizer da lateral encolhe a
coluna do conteúdo sem tocar no viewport, e a página passou a rolar na horizontal
(``test_vertical_resizer_drags_and_persists`` pegou). O gatilho virou CONTAINER
QUERY, e é isso que se prova aqui: cabe numa linha quando há espaço, quebra quando
não há, e NUNCA gera scroll horizontal.
"""

import threading

import pytest

from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.server import make_server
from tradingagents.webui.store import HistoryStore

pytestmark = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None


@pytest.fixture
def base(tmp_path):
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "openai",
                     "deep_think_llm": "x", "quick_think_llm": "y"},
        store=HistoryStore(tmp_path))
    httpd = make_server("127.0.0.1", 0, runner=runner)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


def _medidas(page):
    return page.evaluate("""() => {
      const bar = document.querySelector('.launch-bar');
      const r = document.documentElement;
      const alt = (s) => { const e = document.querySelector(s); return e ? Math.round(e.getBoundingClientRect().height) : null; };
      const topo = (s) => { const e = document.querySelector(s); return e ? Math.round(e.getBoundingClientRect().top) : null; };
      return {
        barW: bar.scrollWidth, barC: bar.clientWidth, barH: Math.round(bar.getBoundingClientRect().height),
        tfsH: alt('.lb-tfs'), methodsH: alt('.lb-methods'), modelsH: alt('.lb-models'),
        topoAtivo: topo('.lb-ticker'), topoAnalisar: topo('#runBtn'), topoModelos: topo('.lb-models'),
        docW: r.scrollWidth, viewW: r.clientWidth,
      };
    }""")


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_barra_cabe_numa_linha_na_tela_larga(base):
    """1500px (a tela do Samyr): tudo na mesma faixa e sem estouro horizontal."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.goto(base, wait_until="networkidle")
        page.wait_for_selector(".lb-methods button")
        m = _medidas(page)
        # nenhuma quebra: a barra inteira cabe na largura que tem
        assert m["barW"] <= m["barC"] + 1, m
        # MODELOS em DUAS linhas — o par é o bloco alto que define a altura da barra
        assert m["modelsH"] >= 48, m
        # TEMPO também é duas fileiras desde a task 017, mas na MESMA forma do par de
        # modelos: a linha aqui era `tfsH <= 34` ("cinco pills não viram duas linhas")
        # e virou esta — o que ela sempre defendeu foi a ALTURA DA BARRA, não o número
        # de fileiras. Empilhar de propósito, dentro da altura que já existia, passa;
        # TEMPO passar de MODELOS (foi o que a pill de 30px fazia) cresce a barra e cai.
        assert m["tfsH"] == m["modelsH"], ("TEMPO tem de caber na forma de MODELOS", m)
        # MÉTODO virou DUAS fileiras na task 022 (o Storm é o quinto método): em cima
        # os que rodam modelo, embaixo os estruturais ($0). A linha aqui era
        # `methodsH <= 36` ("os métodos numa fileira só") — o que ela defendia era a
        # ALTURA DA BARRA, e é isso que continua travado: empilhar DENTRO da forma do
        # bloco de modelos passa; crescer além dela cai.
        assert m["methodsH"] == m["modelsH"], ("MÉTODO tem de caber na forma de MODELOS", m)
        assert m["barH"] - m["modelsH"] <= 20, ("a barra é o bloco mais alto + o rótulo", m)
        # e continua sendo UM elemento da MESMA linha: os três começam juntos
        # (o topo do bloco de modelos é o mais alto; ninguém foi empurrado pra baixo)
        assert m["topoModelos"] <= m["topoAtivo"], m
        assert abs(m["topoAnalisar"] - m["topoAtivo"]) < 40, m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_coluna_estreita_quebra_a_barra_em_vez_de_estourar_a_pagina(base):
    """DENTE do bug que a primeira tentativa introduziu: com a lateral arrastada o
    conteúdo encolhe SEM o viewport mudar. Uma linha só ali seria scroll horizontal
    na página inteira — a barra tem que quebrar, e a página não rolar.

    A lateral foi de 400px para 700px na task 022: com o MÉTODO em duas fileiras a
    barra ficou mais ESTREITA (a largura do grupo passou a ser a da fileira mais
    larga, não a soma dos cinco), então 400px já não a apertava o bastante pra
    forçar a quebra. O que se mede continua sendo o mesmo: apertada, ela QUEBRA; a
    página nunca rola."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.goto(base, wait_until="networkidle")
        page.wait_for_selector(".lb-methods button")
        page.evaluate("""() => {
          document.querySelector('main.layout').style.setProperty('--sidebar-w', '700px');
        }""")
        page.wait_for_timeout(150)
        m = _medidas(page)
        assert m["docW"] <= m["viewW"], ("a página passou a rolar na horizontal", m)
        assert m["barW"] <= m["barC"] + 1, m
        assert m["barH"] > 90, ("com pouca largura a barra tem que QUEBRAR", m)
        browser.close()

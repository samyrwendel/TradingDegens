"""E2E da barra de controle numa linha só (task de UI 010, pedido do Samyr;
redistribuída na task 034 a partir de 5 apps de referência — Quantfury/Krystal/
CoinMarketCap).

O pedido original: Ativo · Data · Tempo · Método · Analisar · ↻ na MESMA linha —
antes a barra quebrava em três faixas e o bloco de modelos ficava pendurado à
direita, cortado.

O que este arquivo trava, e por quê: uma linha só é fácil de conseguir com
``nowrap`` e fácil de ERRAR — foi o que aconteceu na primeira tentativa, com o
``nowrap`` preso à largura do VIEWPORT. Arrastar o resizer da lateral encolhe a
coluna do conteúdo sem tocar no viewport, e a página passou a rolar na horizontal
(``test_vertical_resizer_drags_and_persists`` pegou). O gatilho virou CONTAINER
QUERY, e é isso que se prova aqui: cabe numa linha quando há espaço, quebra quando
não há, e NUNCA gera scroll horizontal.

A task 034 apertou o orçamento de largura pra caber em 1280px de VIEWPORT (não só
container): ATIVO parou de crescer (fixo ~160px), TEMPO e MÉTODO viraram uma linha
só (eram duas fileiras empilhadas) e MODELOS colapsou num chevron — o número do
limiar da container query (abaixo) é MEDIDO contra esse orçamento novo.
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
      const fundo = (s) => { const e = document.querySelector(s); return e ? Math.round(e.getBoundingClientRect().bottom) : null; };
      return {
        barW: bar.scrollWidth, barC: bar.clientWidth, barH: Math.round(bar.getBoundingClientRect().height),
        tfsH: alt('.lb-tfs'), methodsH: alt('.lb-methods'), assetH: alt('.lb-asset'),
        toggleH: alt('.lb-model-toggle'),
        fundoAtivo: fundo('.lb-ticker'), fundoAnalisar: fundo('#runBtn'), fundoToggle: fundo('.lb-model-toggle'),
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
        assert m["docW"] <= m["viewW"], m
        # Task 034: TEMPO e MÉTODO viraram UMA linha só (eram duas fileiras
        # empilhadas) — a altura da pill (26px) é bem menor que o campo ATIVO
        # (rótulo + input, o bloco mais alto da barra agora).
        assert m["tfsH"] == 26, m
        assert m["methodsH"] == 26, m
        assert m["assetH"] > m["tfsH"], ("ATIVO+DATA (rótulo+input) é o bloco mais alto", m)
        assert m["barH"] == m["assetH"], ("a barra tem a altura do bloco mais alto", m)
        # MODELOS colapsou num chevron (task 034): mesma altura de botão (36px) do
        # ↻/Analisar, não mais um par de chips sempre visível.
        assert m["toggleH"] == 36, m
        # e continua sendo UM elemento da MESMA linha: todos compartilham o
        # rodapé (`align-items: flex-end`), não o topo — blocos de alturas
        # diferentes têm topos diferentes na mesma linha.
        assert m["fundoToggle"] == m["fundoAtivo"] == m["fundoAnalisar"], m
        # o painel de modelos nasce FECHADO — sob demanda, não sempre visível
        assert page.is_hidden("#launchModelsPop"), "o popover de modelos não pode nascer aberto"
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

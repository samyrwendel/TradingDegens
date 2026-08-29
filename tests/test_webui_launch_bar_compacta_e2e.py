"""A barra de controle recebe o rótulo curto — e o espaço liberado vai pro MODELO.

O que aconteceu: a task 012 encurtou o timeframe da LINHA DO SCAN, que passava por
uma lista PARALELA (``SCAN_TF_CURTO``) mantida à mão. A barra de controle lia de
``ALL_TFS`` e ficou com "Semanal | Diário | 4h | 1h | 15m" gordo — o print de
referência do Samyr era a barra.

Junto vinha "os espaços estão mal utilizados", e a causa maior não era o rótulo:
no HTML o GRUPO é ``<div class="lb-group lb-tf">`` e a PILL é
``<button class="lb-tf">`` — o MESMO nome. O CSS de pill (borda, fundo, padding)
caía também no div do grupo, desenhando uma caixa sem significado em volta de
TEMPO e de MÉTODO e comendo largura nas duas pontas. Com isso a barra ESTOURAVA a
linha em 1500px e o nome do modelo saía cortado em 120px ("claude-haiku-4-…").

O que se trava aqui: fonte ÚNICA (as três superfícies derivam da mesma lista), o
nome completo continuando acessível, o modelo cabendo INTEIRO, e a barra numa
linha só em 1500px sem nunca rolar na horizontal.

Números medidos antes/depois (1500px, modelos longos reais):
  grupo TEMPO      255px → 185px
  nome do modelo   cortado em 120 (pedia 181) → 181px inteiro
  barra em 1 linha  NÃO (estourava) → SIM
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


# Modelos LONGOS de verdade — é o caso que truncava no print do Samyr. Um id com
# prefixo de provedor (OpenRouter) prova de quebra que o encurtador ainda corta o
# "anthropic/" e mostra o nome.
_MODELOS_LONGOS = """() => {
  _llmCfg.quickModel = 'claude-haiku-4-5-20251001';
  _llmCfg.deepModel  = 'anthropic/claude-sonnet-5-20250929';
  renderLaunchBar();
}"""


def _abre(page, base):
    page.goto(base, wait_until="networkidle")
    page.wait_for_selector("#launchTfs button.lb-tf")
    page.evaluate(_MODELOS_LONGOS)
    page.wait_for_timeout(150)


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_barra_usa_o_rotulo_curto_e_guarda_o_completo_no_title_e_no_aria(base):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre(page, base)
        pills = page.evaluate("""() => [...document.querySelectorAll('#launchTfs button.lb-tf')]
            .map(b => ({tf: b.dataset.tf, txt: b.textContent.trim(),
                        title: b.getAttribute('title'), aria: b.getAttribute('aria-label')}))""")
        por_tf = {p_["tf"]: p_ for p_ in pills}
        # DENTE: era "Semanal"/"Diário" escritos por extenso na pill
        assert por_tf["1w"]["txt"] == "S" and por_tf["1d"]["txt"] == "D", pills
        assert [p_["txt"] for p_ in pills] == ["S", "D", "4h", "1h", "15m"], pills
        # o nome completo NÃO se perde — acessibilidade e clareza continuam
        assert por_tf["1w"]["aria"] == "Semanal" and por_tf["1d"]["aria"] == "Diário", pills
        assert "Semanal" in por_tf["1w"]["title"], pills
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_seletor_do_grafico_herda_a_mesma_fonte_sem_lista_paralela(base):
    """Fonte ÚNICA: barra, gráfico e scan derivam de ALL_TFS. Se alguém recriar uma
    lista à mão pra uma das superfícies, elas divergem — e é isso que se proíbe."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre(page, base)
        m = page.evaluate("""() => ({
          fonte: ALL_TFS.map(([tf, curto]) => [tf, curto]),
          curtos: Object.entries(TF_SHORT),
          completos: Object.entries(TF_LABEL),
          barra: [...document.querySelectorAll('#launchTfs button.lb-tf')].map(b => b.textContent.trim()),
          scan: ['1w', '1d', '4h', '1h', '15m'].map(f => {
            const d = document.createElement('div'); d.innerHTML = scanTfBadge(f);
            return d.firstChild.textContent.trim(); }),
        })""")
        curtos_da_fonte = [c for _, c in m["fonte"]]
        assert m["barra"] == curtos_da_fonte, m
        assert m["scan"] == curtos_da_fonte, ("o scan tem que sair da MESMA fonte", m)
        assert dict(m["curtos"]) == dict(m["fonte"]), m
        # e o completo continua existindo pra prosa ("veredito no Semanal")
        assert dict(m["completos"])["1w"] == "Semanal", m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_nome_do_modelo_cabe_inteiro_e_a_barra_fica_numa_linha_em_1500(base):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre(page, base)
        m = page.evaluate("""() => {
          const bar = document.querySelector('.launch-bar');
          const r = document.documentElement;
          return {
            modelos: [...document.querySelectorAll('.lbm-model')].map(e => ({
              txt: e.textContent, cortado: e.scrollWidth > e.clientWidth + 1 })),
            umaLinha: bar.scrollWidth <= bar.clientWidth + 1,
            docW: r.scrollWidth, viewW: r.clientWidth,
          };
        }""")
        # DENTE: os dois vinham cortados em 120px ("claude-haiku-4-…")
        assert all(not x["cortado"] for x in m["modelos"]), m
        assert "claude-haiku-4-5-20251001" in [x["txt"] for x in m["modelos"]], m
        # o "anthropic/" some (encurtador), o nome fica inteiro
        assert "claude-sonnet-5-20250929" in [x["txt"] for x in m["modelos"]], m
        # a barra numa linha só (item 1 da task 010) — e ANTES ela estourava
        assert m["umaLinha"], m
        assert m["docW"] <= m["viewW"], ("a página não pode rolar na horizontal", m)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_grupo_nao_se_veste_de_pill(base):
    """O div do grupo compartilha o nome de classe com o botão. Sem `button.` no
    seletor ele herdava borda/fundo/padding de pill — uma caixa sem significado em
    volta de TEMPO e MÉTODO, comendo largura nas duas pontas."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre(page, base)
        m = page.evaluate("""() => {
          const g = (s) => { const c = getComputedStyle(document.querySelector(s));
            return {borda: c.borderTopWidth, padL: c.paddingLeft, fundo: c.backgroundColor}; };
          return {grupoTf: g('.lb-group.lb-tf'), grupoMet: g('.lb-group.lb-method'),
                  pill: g('#launchTfs button.lb-tf')};
        }""")
        for nome in ("grupoTf", "grupoMet"):
            assert m[nome]["borda"] == "0px", (nome, m)
            assert m[nome]["padL"] == "0px", (nome, m)
            assert m[nome]["fundo"] in ("rgba(0, 0, 0, 0)", "transparent"), (nome, m)
        # a PILL continua sendo pill (o fix não pode ter apagado o estilo dela)
        assert m["pill"]["borda"] != "0px" and m["pill"]["padL"] != "0px", m

        # E a largura devolvida é MENSURÁVEL, não estética: com a caixa fantasma
        # nos DOIS grupos e o mesmo teto de 200px pro modelo, a barra estourava 3px
        # e o campo ATIVO ficava esmagado no próprio min-width (108px). Sem ela o
        # ATIVO respira (147px) e a barra cabe. É este número que responde
        # "espaços mal utilizados" — por isso é asserção, não observação.
        larg = page.evaluate("""() => {
          const bar = document.querySelector('.launch-bar');
          const t = document.querySelector('.lb-ticker');
          return {ticker: Math.round(t.getBoundingClientRect().width),
                  minW: parseFloat(getComputedStyle(t).minWidth),
                  sobra: bar.clientWidth - bar.scrollWidth};
        }""")
        assert larg["sobra"] >= 0, ("a barra não pode estourar a própria largura", larg)
        assert larg["ticker"] > larg["minW"] + 20, (
            "o campo ATIVO não pode ficar esmagado no mínimo pra barra caber", larg)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_em_1280_a_barra_quebra_sem_rolar_a_pagina(base):
    """Largura menor: pode quebrar (a container query da 010 manda), mas nem a
    página rola na horizontal nem o modelo volta a ser cortado."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 950})
        _abre(page, base)
        m = page.evaluate("""() => {
          const r = document.documentElement;
          return {docW: r.scrollWidth, viewW: r.clientWidth,
                  cortados: [...document.querySelectorAll('.lbm-model')]
                    .filter(e => e.scrollWidth > e.clientWidth + 1).length,
                  pills: [...document.querySelectorAll('#launchTfs button.lb-tf')]
                    .map(b => b.textContent.trim())};
        }""")
        assert m["docW"] <= m["viewW"], m
        assert m["cortados"] == 0, m
        assert m["pills"] == ["S", "D", "4h", "1h", "15m"], m
        browser.close()

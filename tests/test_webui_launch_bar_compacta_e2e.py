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
        # A pill perdeu a MOLDURA na DA-078 (regra 9: escolher não é agir, então a
        # escolha de frame é TEXTO). O que este teste sempre defendeu foi o GRUPO não
        # se vestir de pill; a pill em si agora é texto com respiro — e o respiro
        # continua lá, que é o alvo de clique.
        assert m["pill"]["borda"] == "0px", ("seletor de segmento é texto (DA-078)", m)
        assert m["pill"]["padL"] != "0px", ("mas o alvo de clique continua confortável", m)

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


# ---------------------------------------------------------------------------
# Task 017 — TEMPO em DUAS fileiras, contando como UM elemento da barra
#
# O rótulo curto da 016 deixou "S D 4h 1h 15m", mas cinco pills em fila ainda
# ocupavam 185px. O bloco MODELOS já resolvia isso empilhando dois chips e
# contando como um elemento só; TEMPO passa a usar a MESMA gramática: macro
# (S · D) em cima, intradiário (4h · 1h · 15m) embaixo.
#
# Medido em 1500px com modelos longos reais:
#   grupo TEMPO            185px → 115px
#   campo ATIVO            147px → 178px  (a largura vai pro conteúdo, não pro gap)
#   altura da barra         73px →  73px  (a mesma: MODELOS já era o bloco mais alto)
#   1 linha até (container)         1174px → 1104px  →  em 1440 a barra deixou de
#                                   quebrar (139px em duas fileiras → 73px em uma)
# ---------------------------------------------------------------------------

_LINHAS = """() => [...document.querySelectorAll('#launchTfs .lb-tf-row')]
    .map(r => ({faixa: [...r.classList].find(c => c.startsWith('is-')),
                pills: [...r.querySelectorAll('button.lb-tf')].map(b => b.dataset.tf)}))"""

# align-items: flex-end — quem está na MESMA fileira da barra divide o mesmo rodapé.
_FILEIRAS_DA_BARRA = """() => {
  const bar = document.querySelector('.launch-bar');
  const bottoms = [...bar.children].map(c => Math.round(c.getBoundingClientRect().bottom));
  return new Set(bottoms).size;
}"""


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_tempo_vira_duas_fileiras_e_o_corte_sai_da_faixa_do_frame(base):
    """A quebra é SEMÂNTICA (macro × intradiário) e vem declarada no próprio frame,
    em ALL_TFS. Não pode existir uma lista de "quem vai em cima" mantida à parte —
    foi assim que o rótulo curto da 012 chegou só ao scan (task 016)."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre(page, base)
        linhas = page.evaluate(_LINHAS)
        # DENTE: era UMA fileira com os cinco frames
        assert [li["pills"] for li in linhas] == [["1w", "1d"], ["4h", "1h", "15m"]], linhas
        assert [li["faixa"] for li in linhas] == ["is-macro", "is-intra"], linhas
        # e o agrupamento é DERIVADO da fonte única, não escrito de novo aqui
        da_fonte = page.evaluate("""() => {
          const g = {};
          for (const [tf, , , faixa] of ALL_TFS) (g[faixa] ||= []).push(tf);
          return Object.entries(g);
        }""")
        assert [(li["faixa"].replace("is-", ""), li["pills"]) for li in linhas] == [
            (faixa, tfs) for faixa, tfs in da_fonte], (linhas, da_fonte)
        # a ordem de leitura (e do tab) continua decrescente, sem virar duas listas
        assert page.evaluate("""() => [...document.querySelectorAll('#launchTfs button.lb-tf')]
            .map(b => b.textContent.trim())""") == ["S", "D", "4h", "1h", "15m"]
        # o bloco é um RETÂNGULO: a fileira de cima tem menos pills, então elas
        # esticam pra fechar a largura em vez de deixar um degrau à direita — e isso
        # é de graça, a largura do grupo já é a da fileira mais larga (a de baixo).
        sobra = page.evaluate("""() => [...document.querySelectorAll('#launchTfs .lb-tf-row')]
            .map(r => { const pills = [...r.querySelectorAll('button.lb-tf')];
              return Math.round(r.getBoundingClientRect().right
                                - pills[pills.length - 1].getBoundingClientRect().right); })""")
        assert sobra == [0, 0], ("nenhuma fileira pode terminar num degrau", sobra)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_as_duas_fileiras_contam_como_um_elemento_e_nao_crescem_a_barra(base):
    """O par de MODELOS já ocupava duas fileiras sem esticar a barra — a altura dela
    é a do bloco mais alto. TEMPO tem de caber DENTRO dessa mesma altura: duas pills
    de 26px + 4px de gap = os 56px do par de modelos. Se a pill voltar aos 30px
    (``line-height: var(--lh-12)``), TEMPO passa MODELOS e a barra cresce."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre(page, base)
        m = page.evaluate("""() => {
          const h = (s) => Math.round(document.querySelector(s).getBoundingClientRect().height);
          return {barra: h('.launch-bar'), tempo: h('.lb-group.lb-tf'),
                  modelos: h('.lb-group.lb-model'), pill: h('#launchTfs button.lb-tf'),
                  chip: h('.lb-model-pick')};
        }""")
        assert m["tempo"] == m["modelos"], ("TEMPO tem de ter a mesma forma de MODELOS", m)
        assert m["pill"] == m["chip"], ("a pill e o chip de modelo medem o mesmo", m)
        # DENTE: com a pill em 30px isto vira 82 > 73 e a barra inteira cresce
        assert m["barra"] == m["modelos"], ("a barra continua com a altura do bloco mais alto", m)
        # e a barra segue sendo UMA fileira em 1500 (item 1 da task 010)
        assert page.evaluate(_FILEIRAS_DA_BARRA) == 1, "a barra não pode quebrar em duas"
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_largura_economizada_vai_pro_conteudo_e_nao_pro_gap(base):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre(page, base)
        m = page.evaluate("""() => {
          const w = (s) => Math.round(document.querySelector(s).getBoundingClientRect().width);
          const t = document.querySelector('.lb-ticker');
          return {tempo: w('.lb-group.lb-tf'), ticker: w('.lb-ticker'),
                  base: parseFloat(getComputedStyle(t).flexBasis),
                  gapBarra: getComputedStyle(document.querySelector('.launch-bar')).columnGap,
                  gapCtrls: getComputedStyle(document.querySelector('.lb-ctrls')).columnGap,
                  gapTfs: getComputedStyle(document.querySelector('#launchTfs')).rowGap};
        }""")
        # DENTE: 185px em fileira única; empilhado cabe em ~115
        assert m["tempo"] <= 130, ("o grupo TEMPO tem de estreitar de verdade", m)
        # os 70px devolvidos NÃO viram respiro entre os blocos...
        assert m["gapBarra"] == "12px" and m["gapCtrls"] == "12px", m
        assert m["gapTfs"] == "4px", ("o gap interno é o do bloco MODELOS", m)
        # ...vão pro campo ATIVO, que antes vivia ESMAGADO abaixo da própria base
        assert m["ticker"] > m["base"], ("o ATIVO tem de deixar de encolher", m)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_em_1440_a_barra_deixou_de_quebrar(base):
    """O limiar da container query é um número MEDIDO (era 1176 = a largura que a
    barra ocupava numa linha). Empilhar o TEMPO devolveu 70px; se o limiar ficasse
    no número velho, a barra continuaria quebrando à toa entre 1106 e 1176 — e é
    exatamente onde cai o 1440 do MacBook, o print onde MODELOS ficava pendurado
    sozinho na segunda fileira."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 950})
        _abre(page, base)
        assert page.evaluate(_FILEIRAS_DA_BARRA) == 1, "em 1440 a barra cabe numa linha"
        m = page.evaluate("""() => ({
          barra: Math.round(document.querySelector('.launch-bar').getBoundingClientRect().height),
          modelos: Math.round(document.querySelector('.lb-group.lb-model').getBoundingClientRect().height),
          docW: document.documentElement.scrollWidth, viewW: document.documentElement.clientWidth,
          cortados: [...document.querySelectorAll('.lbm-model')]
            .filter(e => e.scrollWidth > e.clientWidth + 1).length,
        })""")
        assert m["barra"] == m["modelos"], m  # DENTE: 139px (duas fileiras) antes
        assert m["docW"] <= m["viewW"] and m["cortados"] == 0, m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_clicar_numa_pill_da_fileira_de_baixo_seleciona_o_frame(base):
    """As pills passaram a viver dentro de um <div> de fileira; o clique é delegado
    no #launchTfs. Se a delegação parasse no wrapper, o intradiário inteiro ficaria
    inclicável — a fileira de baixo é a que este teste aperta."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre(page, base)
        page.click("#launchTfs .lb-tf-row.is-intra button[data-tf='4h']")
        page.wait_for_timeout(80)
        m = page.evaluate("""() => ({barTf: _barTf,
          ativa: document.querySelector('#launchTfs button.lb-tf.is-active').dataset.tf,
          fileiraDaAtiva: [...document.querySelector(
            '#launchTfs button.lb-tf.is-active').closest('.lb-tf-row').classList]
            .find(c => c.startsWith('is-'))})""")
        assert m["barTf"] == "4h" and m["ativa"] == "4h", m
        assert m["fileiraDaAtiva"] == "is-intra", m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_no_telefone_o_tempo_continua_em_uma_fileira(base):
    """A segunda fileira existe pra estreitar o DESKTOP. No telefone a barra já é uma
    coluna de blocos: empilhar ali só somaria altura sem devolver largura nenhuma."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        _abre(page, base)
        m = page.evaluate("""() => {
          const bs = [...document.querySelectorAll('#launchTfs button.lb-tf')];
          const g = document.querySelector('#launchTfs').getBoundingClientRect();
          const r = bs.map(b => b.getBoundingClientRect());
          return {
            tops: [...new Set(r.map(x => Math.round(x.top)))].length, pills: bs.length,
            larguras: r.map(x => Math.round(x.width)),
            preenche: Math.round(r[0].left - g.left) === 0
                   && Math.round(g.right - r[r.length - 1].right) === 0,
            docW: document.documentElement.scrollWidth, viewW: document.documentElement.clientWidth,
          };
        }""")
        assert m["pills"] == 5 and m["tops"] == 1, ("no 390 os cinco frames ficam em fila", m)
        # e a fila é a MESMA que já estava no ar: cinco pills de igual largura ocupando
        # a linha inteira (é o `flex: 1` do mobile — ele só alcança os botões se as
        # fileiras se dissolverem com `display: contents`).
        assert max(m["larguras"]) - min(m["larguras"]) <= 1, m
        assert m["preenche"], ("as pills ocupam a largura toda no telefone", m)
        assert m["docW"] <= m["viewW"], m
        browser.close()

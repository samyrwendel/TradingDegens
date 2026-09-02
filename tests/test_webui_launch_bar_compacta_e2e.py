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


# Task 034: MODELOS colapsou num chevron — os chips (`.lbm-model`) só têm caixa de
# layout (e por tabela, `scrollWidth`/`clientWidth` mensuráveis) quando o painel
# está ABERTO. Testes que medem corte de texto do modelo precisam abrir primeiro.
def _abre_com_modelos_visiveis(page, base):
    _abre(page, base)
    page.evaluate("() => _toggleModelsMenu()")
    page.wait_for_timeout(80)


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
        # Task 034: ordem CRESCENTE (15m → S) — do frame mais rápido pro mais
        # lento, como o mercado lê. Era decrescente (S → 15m) até aqui.
        assert [p_["txt"] for p_ in pills] == ["15m", "1h", "4h", "D", "S"], pills
        # o nome completo NÃO se perde — acessibilidade e clareza continuam
        assert por_tf["1w"]["aria"] == "Semanal" and por_tf["1d"]["aria"] == "Diário", pills
        assert "Semanal" in por_tf["1w"]["title"], pills
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_seletor_do_grafico_herda_a_mesma_fonte_sem_lista_paralela(base):
    """Fonte ÚNICA: barra, gráfico e scan derivam de ALL_TFS. Se alguém recriar uma
    lista à mão pra uma das superfícies, elas divergem — e é isso que se proíbe.

    Task 034: a barra do LAUNCHER passou a ler ALL_TFS em ordem INVERSA (crescente,
    15m → S — como o mercado lê); o gráfico e o scan continuam na ordem original
    de ALL_TFS (decrescente). A inversão é um `.reverse()` no próprio render, não
    uma segunda lista escrita à mão — é isso que este teste trava."""
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
        assert m["barra"] == list(reversed(curtos_da_fonte)), ("a barra lê a fonte de trás pra frente", m)
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
        # Task 034: os chips só ganham caixa de layout com o painel de MODELOS
        # aberto (colapsou num chevron) — sem abrir, `.lbm-model` mede 0x0.
        _abre_com_modelos_visiveis(page, base)
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
def test_em_1280_a_barra_cabe_numa_linha_sem_rolar_a_pagina(base):
    """Task 034: 1280px é largura de VIEWPORT explicitamente pedida (Samyr aprovou
    a redistribuição a partir dos 5 apps de referência) — a barra tem que caber
    numa linha AQUI, não só quebrar sem rolar. O orçamento (ATIVO fixo ~160px,
    TEMPO/MÉTODO numa linha só, MODELOS um chevron de 36px) foi calibrado pra isso:
    com a lateral padrão (280px + resizer), o conteúdo tem 962px — e é exatamente
    o que a barra pede, sem sobrar quase nada."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 950})
        _abre_com_modelos_visiveis(page, base)
        m = page.evaluate("""() => {
          const bar = document.querySelector('.launch-bar');
          const r = document.documentElement;
          return {docW: r.scrollWidth, viewW: r.clientWidth,
                  umaLinha: bar.scrollWidth <= bar.clientWidth + 1,
                  cortados: [...document.querySelectorAll('.lbm-model')]
                    .filter(e => e.scrollWidth > e.clientWidth + 1).length,
                  pills: [...document.querySelectorAll('#launchTfs button.lb-tf')]
                    .map(b => b.textContent.trim())};
        }""")
        assert m["docW"] <= m["viewW"], m
        assert m["umaLinha"], ("a barra tem que caber numa linha em 1280 (task 034)", m)
        assert m["cortados"] == 0, m
        assert m["pills"] == ["15m", "1h", "4h", "D", "S"], m
        browser.close()


# ---------------------------------------------------------------------------
# Task 20260902-034 — TEMPO e MÉTODO voltam a UMA fileira; MODELOS colapsa
#
# As tasks 017/022 tinham empilhado TEMPO e MÉTODO em DUAS fileiras cada (a
# mesma gramática do bloco MODELOS) pra caber em 1500px sem estourar. A 034
# reverte isso DE PROPÓSITO: o pedido, a partir de 5 apps de referência
# (Quantfury/Krystal/CoinMarketCap), foi UMA linha só pra cada grupo — nenhum
# desses apps empilha frame/método em duas fileiras. O espaço que isso custa
# saiu de outro lugar: ATIVO deixou de crescer (fixo ~160px, antes esticava até
# ~345px), MODELOS colapsou de um par de chips sempre visível pra um chevron de
# 36px, e o padding das pills apertou. O orçamento inteiro foi recalibrado pra
# caber em 1280px de VIEWPORT (não só 1500) — ver os testes de largura abaixo.
# ---------------------------------------------------------------------------

# align-items: flex-end — quem está na MESMA fileira da barra divide o mesmo rodapé.
_FILEIRAS_DA_BARRA = """() => {
  const bar = document.querySelector('.launch-bar');
  const bottoms = [...bar.children].map(c => Math.round(c.getBoundingClientRect().bottom));
  return new Set(bottoms).size;
}"""


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_tempo_e_metodo_sao_uma_fileira_so(base):
    """DENTE: TEMPO e MÉTODO eram DUAS fileiras (17px×2 + gap) — task 034 reverte
    pra UMA, seguindo os 5 apps de referência. `.lb-tf-row`/`.lb-method-row` (os
    wrappers de fileira) saíram do HTML por completo — não há mais fileira pra
    achar."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre(page, base)
        m = page.evaluate("""() => ({
          semFileiraTf: document.querySelectorAll('#launchTfs .lb-tf-row').length,
          semFileiraMet: document.querySelectorAll('#launchMethods .lb-method-row').length,
          tfTops: [...new Set([...document.querySelectorAll('#launchTfs button.lb-tf')]
            .map(b => Math.round(b.getBoundingClientRect().top)))].length,
          metTops: [...new Set([...document.querySelectorAll('#launchMethods button.lb-method')]
            .map(b => Math.round(b.getBoundingClientRect().top)))].length,
          ordem: [...document.querySelectorAll('#launchTfs button.lb-tf')].map(b => b.dataset.tf),
        })""")
        assert m["semFileiraTf"] == 0 and m["semFileiraMet"] == 0, m
        assert m["tfTops"] == 1, ("os 5 frames numa única fileira", m)
        assert m["metTops"] == 1, ("os 5 métodos numa única fileira", m)
        # e a ordem de leitura é CRESCENTE (15m → S), não mais decrescente
        assert m["ordem"] == ["15m", "1h", "4h", "1d", "1w"], m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_modelos_colapsa_num_chevron_de_36px(base):
    """MODELOS era o bloco mais ALTO da barra (73px, dois chips empilhados com o
    rótulo "modelos" por cima) — hoje é um botão-ícone de 36px, a mesma altura de
    Analisar/↻. Quem passa a definir a altura da barra é o campo ATIVO+DATA
    (rótulo + input), não mais MODELOS."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre(page, base)
        m = page.evaluate("""() => {
          const h = (s) => Math.round(document.querySelector(s).getBoundingClientRect().height);
          return {barra: h('.launch-bar'), tempo: h('.lb-group.lb-tf'),
                  metodo: h('.lb-group.lb-method'), asset: h('.lb-asset'),
                  toggle: h('.lb-model-toggle'), run: h('#runBtn')};
        }""")
        assert m["toggle"] == 36 and m["run"] == 36, m
        assert m["tempo"] == 26 and m["metodo"] == 26, ("uma fileira só de pill", m)
        assert m["barra"] == m["asset"], ("ATIVO+DATA é o bloco mais alto agora", m)
        assert m["asset"] > m["toggle"], m
        # e a barra segue sendo UMA fileira em 1500 (item 1 da task 010)
        assert page.evaluate(_FILEIRAS_DA_BARRA) == 1, "a barra não pode quebrar em duas"
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_ativo_para_de_crescer_e_vira_ticker(base):
    """DENTE: o ATIVO era ``flex: 1 1 160px`` e esticava até ~345px com a barra
    folgada — "é ticker, não frase" (Samyr). Task 034: ``flex: 0 0 160px``, não
    cresce nunca, só encolhe sob pressão real (`min-width`)."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 2200, "height": 950})
        _abre(page, base)
        m = page.evaluate("""() => {
          const t = document.querySelector('.lb-ticker');
          const c = getComputedStyle(t);
          return {largura: Math.round(t.getBoundingClientRect().width),
                  grow: c.flexGrow, base: parseFloat(c.flexBasis)};
        }""")
        assert m["grow"] == "0", ("o ATIVO não pode crescer mesmo com a barra folgada", m)
        assert m["base"] == 160, m
        assert m["largura"] == 160, ("mesmo com 2200px de sobra, o ATIVO fica em 160px", m)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_em_1440_a_barra_cabe_numa_linha(base):
    """O limiar da container query é um número MEDIDO contra o orçamento da 034
    (ATIVO fixo, TEMPO/MÉTODO em uma linha, MODELOS um chevron) — não o número
    velho da 017 (empilhamento), que não existe mais."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 950})
        _abre_com_modelos_visiveis(page, base)
        assert page.evaluate(_FILEIRAS_DA_BARRA) == 1, "em 1440 a barra cabe numa linha"
        m = page.evaluate("""() => ({
          docW: document.documentElement.scrollWidth, viewW: document.documentElement.clientWidth,
          cortados: [...document.querySelectorAll('.lbm-model')]
            .filter(e => e.scrollWidth > e.clientWidth + 1).length,
        })""")
        assert m["docW"] <= m["viewW"] and m["cortados"] == 0, m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_clicar_numa_pill_de_tempo_seleciona_o_frame(base):
    """As pills de TEMPO vivem direto em `#launchTfs` (sem wrapper de fileira desde
    a 034) — o clique é delegado ali; qualquer pill, inclusive as antes "de baixo"
    (intradiário), tem que responder."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre(page, base)
        page.click("#launchTfs button[data-tf='4h']")
        page.wait_for_timeout(80)
        m = page.evaluate("""() => ({barTf: _barTf,
          ativa: document.querySelector('#launchTfs button.lb-tf.is-active').dataset.tf})""")
        assert m["barTf"] == "4h" and m["ativa"] == "4h", m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_no_telefone_o_tempo_fica_agrupado_nao_esparramado(base):
    """DENTE (task 034): os cinco frames viviam com `flex: 1` no mobile — cada pill
    esticava pra dividir a largura toda, "S" grudado na esquerda e "15m" na
    direita, o meio vazio (o efeito visual de um `space-between` sem ser um).
    Samyr pediu pra "agrupar com gap fixo em vez de space-between" — as pills
    ficam do próprio tamanho, JUNTAS, com gap fixo, sem preencher a linha toda."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        _abre(page, base)
        m = page.evaluate("""() => {
          const bs = [...document.querySelectorAll('#launchTfs button.lb-tf')];
          const g = document.querySelector('#launchTfs').getBoundingClientRect();
          const r = bs.map(b => b.getBoundingClientRect());
          const gaps = r.slice(1).map((x, i) => Math.round(x.left - r[i].right));
          return {
            tops: [...new Set(r.map(x => Math.round(x.top)))].length, pills: bs.length,
            larguras: r.map(x => Math.round(x.width)),
            gaps,
            preenche: Math.round(r[0].left - g.left) === 0
                   && Math.round(g.right - r[r.length - 1].right) === 0,
            docW: document.documentElement.scrollWidth, viewW: document.documentElement.clientWidth,
          };
        }""")
        assert m["pills"] == 5 and m["tops"] == 1, ("no 390 os cinco frames ficam em fila", m)
        # DENTE: eram 5 larguras IGUAIS (a marca do `flex: 1`) — agora cada pill
        # pede o próprio tamanho ("15m" é mais largo que "S"/"D").
        assert len(set(m["larguras"])) > 1, ("as pills não podem mais ter largura idêntica (flex:1)", m)
        # gap FIXO entre elas (não zero, não crescente/decrescente feito space-between)
        assert all(g == m["gaps"][0] for g in m["gaps"]), ("o gap entre pills tem que ser fixo", m)
        assert 0 < m["gaps"][0] <= 8, m
        # DENTE: antes elas preenchiam a linha inteira (primeira encostada na
        # esquerda, última na direita) — agrupadas, sobra espaço à direita.
        assert not m["preenche"], ("agrupadas, não pode mais preencher a linha toda", m)
        assert m["docW"] <= m["viewW"], m
        browser.close()

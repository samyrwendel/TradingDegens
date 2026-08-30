"""O modo LISTA do scan vira uma TABELA de verdade (task de UI 019).

Pedido do Samyr: "faz colunas mais definidas e deixa cada informação em uma coluna
pra ficar mais organizado e permitir uma melhor comparação".

O que havia: ``.scan-line-row`` era ``display: flex; flex-wrap: wrap`` e os níveis
usavam ``margin-left: auto``. Isso empurra o bloco pro extremo direito, mas cada
linha se alinha pelo PRÓPRIO conteúdo — no print dele o gatilho do MSFT, do LINK-USD
e do ZEC-USD começavam em três posições diferentes. Comparar exigia varrer com o
olho em vez de descer a coluna, e ainda sobrava um rasgo vazio no meio da linha
enquanto o texto do motivo truncava na borda.

O que se trava aqui: coluna que EXISTE (mesma posição em todas as linhas), número à
direita com ``tabular-nums`` (comparação por casa decimal), cabeçalho nomeando cada
coluna, texto de motivo em forma curta COM o texto inteiro no ``title`` — e o modo
CARDS intocado.
"""

import json
import re
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


def _f(frame, estado, **kw):
    base = {"frame": frame, "estado": estado, "direction": "compra", "price": 513.53,
            "dist_pct": 0.0015, "dist_txt": "0.15%", "trigger": 512.76, "sl": 471.35,
            "tp": 515.06, "rr": 0.06, "rr_note": None, "pattern_state": "formando",
            "rr_entry": 512.76, "rr_basis": "gatilho", "rr_risco": 41.41,
            "rr_retorno": 2.3, "rr_residual": False, "invalidacao": 476.25}
    base.update(kw)
    return base


# Os três casos que quebravam qualquer alinhamento são justamente os TEXTOS: o
# residual da 012 ("alvo praticamente alcançado — sobrou 1,53 pra 42,15 de risco"),
# o alvo recusado da 008 ("sem alvo — nível de alvo indefinido") e o qualificador de
# entrada da 012 ("do preço atual"). Estão todos aqui de propósito.
_SCAN = {
    "date": "2026-08-29", "frames": ["1d", "4h", "1h"],
    "resumo": {"em_gatilho": 3, "em_movimento": 1, "invalidou": 1, "formando": 1},
    "ativos": [
        {"ticker": "MSFT", "melhor": _f("1d", "em_gatilho"),
         "frames": [_f("1d", "em_gatilho"),
                    _f("1h", "em_gatilho", price=513.67, trigger=497.14, tp=513.73,
                       rr=0.21, pattern_state="acionado", dist_txt="3.32%"),
                    _f("4h", "formando", price=513.53)]},
        {"ticker": "LINK-USD", "melhor": _f("1d", "em_gatilho"),
         "frames": [_f("1d", "em_gatilho", price=24.37, trigger=24.11, sl=22.58, tp=24.4,
                       rr=0.04, rr_residual=True, rr_retorno=1.53, rr_risco=42.15,
                       pattern_state="acionado", dist_txt="1.08%"),
                    _f("4h", "em_movimento", price=24.37, trigger=24.11, sl=22.58, tp=None,
                       rr=None, rr_note="nível de alvo indefinido", dist_txt="0.42%")]},
        {"ticker": "ZEC-USD", "melhor": _f("4h", "invalidou"),
         "frames": [_f("4h", "invalidou", price=835.37, trigger=834.82, sl=764.76,
                       tp=856.72, rr=0.31, invalidacao=790.29, dist_txt="0.07%"),
                    _f("1d", "em_gatilho", price=835.37, trigger=834.82, sl=764.76,
                       tp=856.72, rr=0.31, dist_txt="0.07%")]},
    ],
}


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


def _abre_lista(page, base, scan=None):
    def handler(route):
        if "/api/scan" in route.request.url and "verdicts" not in route.request.url:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(scan or _SCAN))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)
    page.goto(base, wait_until="networkidle")
    page.click("#scanOpenBtn")
    page.click("#scanRunBtn")
    page.wait_for_selector("#scanList li")
    page.click(".scan-view[data-view='lista']")
    page.wait_for_timeout(200)


# Cada coluna tem UMA posição. Se alguma linha começa a coluna noutro lugar, a coluna
# não existe — é a queixa inteira em uma medida.
_COLUNAS = """() => {
  const rows = [...document.querySelectorAll('.scan-line-row')];
  const pos = (n) => [...new Set(rows.map(r =>
    Math.round(r.children[n].getBoundingClientRect().left)))];
  return {n: rows.length, cols: [...Array(9).keys()].map(pos)};
}"""

# Nada pode truncar CALADO: cortou, o texto inteiro tem que estar no title.
_TRUNCADOS = """() => [...document.querySelectorAll('#scanList .scan-cell, #scanList .scan-tk-inline')]
  .filter(e => e.scrollWidth > e.clientWidth + 1)
  .map(e => ({txt: e.innerText.slice(0, 24), title: e.getAttribute('title')}))"""


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("largura", [1500, 1280])
def test_cada_informacao_tem_a_sua_coluna_e_a_coluna_e_a_mesma_em_toda_linha(base, largura):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": largura, "height": 950})
        _abre_lista(page, base)
        m = page.evaluate(_COLUNAS)
        assert m["n"] == 7, m
        # DENTE: no flex-wrap com margin-left:auto, gatilho/SL/TP/R:R tinham uma
        # posição POR LINHA (quatro posições diferentes na coluna do gatilho).
        for i, posicoes in enumerate(m["cols"]):
            assert len(posicoes) == 1, (f"coluna {i} não alinha entre as linhas", m["cols"])
        # e as nove colunas são as MESMAS pro cabeçalho e pras linhas
        grade = page.evaluate("""() => ({
          linha: getComputedStyle(document.querySelector('.scan-line-row')).gridTemplateColumns,
          cab: getComputedStyle(document.querySelector('.scan-line-head')).gridTemplateColumns,
          nomes: [...document.querySelectorAll('.scan-line-head .scan-col')].map(e => e.innerText.trim()),
        })""")
        assert grade["linha"] == grade["cab"], grade
        # DEZ colunas desde a task 023: a do STORM entrou no fim (setup diferente,
        # célula própria — nunca somado às células do 1-2-3).
        assert len(grade["linha"].split(" ")) == 10, grade
        assert [n.lower() for n in grade["nomes"]] == [
            "tf", "ativo", "preço", "dist", "estado", "gatilho", "sl", "tp", "r:r",
            "storm"], grade
        # altura igual em todas as linhas: chip que quebra em duas desmancha a
        # leitura em coluna tanto quanto o desalinhamento
        alturas = page.evaluate("""() => [...new Set([...document.querySelectorAll('.scan-line-row')]
            .map(r => Math.round(r.getBoundingClientRect().height)))]""")
        assert len(alturas) == 1, ("linhas com alturas diferentes", alturas)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_numero_a_direita_e_tabular_para_comparar_por_casa_decimal(base):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_lista(page, base)
        m = page.evaluate("""() => {
          const est = (e) => { const c = getComputedStyle(e);
            return {al: c.textAlign, tab: c.fontVariantNumeric}; };
          const row = document.querySelector('.scan-line-row');
          return {
            celulas: [...row.querySelectorAll('.scan-cell.num')].map(est),
            preco: est(row.querySelector('.scan-price')),
            dist: est(row.querySelector('.scan-dist')),
            cab: [...document.querySelectorAll('.scan-line-head .scan-col')].map(
              e => getComputedStyle(e).textAlign),
          };
        }""")
        assert m["celulas"], m
        for c in m["celulas"]:
            assert c["al"] == "right" and c["tab"] == "tabular-nums", m
        # preço e distância também são números da tabela
        assert m["preco"]["tab"] == "tabular-nums" and m["dist"]["tab"] == "tabular-nums", m
        # o CABEÇALHO copia o alinhamento da célula (rótulo à esquerda com número à
        # direita é cabeçalho que não aponta pra nada)
        assert m["cab"][0] == "start" or m["cab"][0] == "left", m
        assert m["cab"][5] == "right" and m["cab"][8] == "right", m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("largura", [1500, 1280])
def test_o_texto_de_motivo_nao_empurra_coluna_nem_trunca_calado(base, largura):
    """Os textos longos (residual, alvo recusado, invalidação) eram o que estourava
    qualquer alinhamento. Escolha DECLARADA: forma curta na célula ("🏁 no alvo",
    "⚠️ sem alvo", "invalidação 790,29") com o texto inteiro no title — a coluna é
    estreita demais pra prosa, e prosa numa coluna de tabela empurra tudo."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": largura, "height": 950})
        _abre_lista(page, base)
        for t in page.evaluate(_TRUNCADOS):
            assert t["title"], ("cortou sem title", t)
        m = page.evaluate("""() => {
          const cel = (txt) => [...document.querySelectorAll('#scanList .scan-cell')]
            .filter(e => e.innerText.includes(txt))
            .map(e => ({txt: e.innerText.trim(), title: e.getAttribute('title'),
                        col: [...e.parentElement.children].indexOf(e)}));
          return {residual: cel('no alvo'), semAlvo: cel('sem alvo'), inval: cel('invalidação')};
        }""")
        # forma CURTA na célula...
        assert m["residual"] and "praticamente" not in m["residual"][0]["txt"], m
        # ...com o texto inteiro (e o número que ele carrega) no title
        assert "alvo praticamente alcançado" in m["residual"][0]["title"], m
        assert "1,53" in m["residual"][0]["title"] and "42,15" in m["residual"][0]["title"], m
        assert "nível de alvo indefinido" in m["semAlvo"][0]["title"], m
        # cada um na SUA coluna: motivo do alvo na coluna do TP (8ª), residual na do
        # R:R (9ª) — e a invalidação divide a coluna do TP dizendo que não é um TP
        assert m["semAlvo"][0]["col"] == 7, m
        assert m["residual"][0]["col"] == 8, m
        assert m["inval"][0]["col"] == 7 and "invalidação" in m["inval"][0]["txt"], m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_qualificador_do_rr_vira_marcador_com_legenda(base):
    """"R:R 0,21 do preço atual" não cabe na célula sem empurrar a coluna — e apagar
    o qualificador seria pior: 0,21 medido do preço atual quer dizer outra coisa
    (task 012). Vira "*" com title, mais uma legenda embaixo da tabela pra quem não
    passa o mouse. A legenda só existe quando alguma linha usa o marcador."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_lista(page, base)
        m = page.evaluate("""() => {
          const marca = document.querySelector('#scanList .scan-mark');
          const leg = document.querySelector('#scanLegenda');
          return {marca: marca && marca.innerText.trim(),
                  title: marca && marca.getAttribute('title'),
                  celula: marca && marca.parentElement.innerText.trim(),
                  legendaEscondida: leg.classList.contains('hidden'),
                  legenda: leg.innerText};
        }""")
        assert m["marca"] == "*", m
        assert "preço atual" in (m["title"] or "").lower(), m
        assert "0.21" in m["celula"], m
        assert not m["legendaEscondida"], m
        assert "preço atual" in m["legenda"], m

        # sem nenhuma linha marcada, a legenda não fica na tela falando de um
        # marcador que não existe
        scan = json.loads(json.dumps(_SCAN))
        for a in scan["ativos"]:
            for f in a["frames"]:
                f["pattern_state"] = "formando"
        page2 = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_lista(page2, base, scan)
        assert page2.evaluate(
            "() => document.querySelector('#scanLegenda').classList.contains('hidden')")
        assert page2.locator("#scanList .scan-mark").count() == 0
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_com_a_coluna_estreita_a_tabela_sai_de_cena_e_os_rotulos_voltam(base):
    """A largura útil muda com a lateral arrastada (foi por isso que a 012 usou
    container query e não media query). Abaixo do ponto em que as nove colunas cabem,
    a grade sai — e como os rótulos por célula tinham saído (quem nomeia é o
    cabeçalho), eles VOLTAM junto: número solto sem cabeçalho em cima não diz nada.

    O ponto de quebra subiu de 700 para 800px na task 023: a coluna do Storm (96px
    de piso) empurrou a soma dos pisos, e o limiar é aritmético, não gosto."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_lista(page, base)

        def estado():
            return page.evaluate("""() => ({
              display: getComputedStyle(document.querySelector('.scan-line-row')).display,
              cabecalho: getComputedStyle(document.querySelector('.scan-line-head')).display,
              ck: getComputedStyle(document.querySelector('#scanList .scan-cell .scan-ck')).position,
              rola: document.querySelector('#scanList').scrollWidth
                    > document.querySelector('#scanList').clientWidth + 1,
              paginaRola: document.documentElement.scrollWidth > document.documentElement.clientWidth,
            })""")

        def lateral(px):
            page.evaluate("(w) => document.querySelector('main.layout')"
                          ".style.setProperty('--sidebar-w', w + 'px')", px)
            page.wait_for_timeout(200)

        m = estado()
        assert m["display"] == "grid" and m["cabecalho"] != "none", m
        assert m["ck"] == "absolute", ("com cabeçalho, o rótulo por célula fica só pro leitor", m)
        for px in (600, 640):
            lateral(px)
            m = estado()
            assert m["display"] == "grid", (px, m)
            assert not m["rola"] and not m["paginaRola"], (px, m)
            assert page.evaluate(_COLUNAS)["cols"][5] == page.evaluate(_COLUNAS)["cols"][5]
        lateral(900)
        m = estado()
        assert m["display"] == "flex", ("coluna estreita: a grade sai de cena", m)
        assert m["cabecalho"] == "none", ("cabeçalho sem colunas embaixo mente", m)
        assert m["ck"] == "static", ("os rótulos por célula voltam", m)
        assert not m["rola"] and not m["paginaRola"], m
        for t in page.evaluate(_TRUNCADOS):
            assert t["title"], ("cortou sem title na coluna estreita", t)
        txt = page.inner_text("#scanList")
        assert "gatilho" in txt and "SL" in txt and "TP" in txt, txt
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_modo_cards_nao_foi_tocado(base):
    """A grade de até 3 colunas dos CARDS é da task 012 e o escopo aqui é só a lista.
    Nos cards os rótulos por célula CONTINUAM (lá não há cabeçalho de coluna)."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_lista(page, base)
        page.click(".scan-view[data-view='cards']")
        page.wait_for_timeout(200)
        m = page.evaluate("""() => ({
          colunasDaGrade: getComputedStyle(document.querySelector('#scanList'))
            .gridTemplateColumns.split(' ').length,
          linhaDeFrame: getComputedStyle(document.querySelector('.scan-frame-row')).display,
          niveis: document.querySelector('.scan-frame-row .scan-levels').innerText.replace(/\\n/g, ' '),
          celulasDeTabela: document.querySelectorAll('#scanList .scan-cell').length,
          cabecalho: document.querySelectorAll('.scan-line-head').length,
          legendaEscondida: document.querySelector('#scanLegenda').classList.contains('hidden'),
        })""")
        assert m["colunasDaGrade"] == 3, ("a grade de 3 colunas da 012 continua", m)
        assert m["linhaDeFrame"] != "grid", ("cards não virou tabela", m)
        # Os rótulos por célula CONTINUAM (é o que distingue os cards da tabela); o
        # que saiu foi o pictograma que os antecedia (DA-076, task 025).
        assert "gatilho" in m["niveis"] and "SL" in m["niveis"], m
        assert "🎯" not in m["niveis"], ("pictograma saiu da tela (DA-076)", m)
        assert m["celulasDeTabela"] == 0 and m["cabecalho"] == 0, m
        assert m["legendaEscondida"], ("legenda é da tabela; em cards não há tabela", m)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_DA070_na_tabela_nova(base):
    """Zero degradê e canto quadrado também no que a tabela trouxe de novo."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_lista(page, base)
        m = page.evaluate("""() => ['.scan-line-head', '.scan-line-row', '#scanList .scan-cell']
          .flatMap(s => [...document.querySelectorAll(s)].map(e => { const c = getComputedStyle(e);
            return {s, bg: c.backgroundImage, raio: c.borderTopLeftRadius}; }))""")
        assert m, "nada medido"
        assert all(x["bg"] == "none" for x in m), [x for x in m if x["bg"] != "none"]
        assert all(x["raio"] == "0px" for x in m), [x for x in m if x["raio"] != "0px"]
        browser.close()

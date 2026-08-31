"""E2E da lista do scan: busca, dois modos de apresentação e o timeframe destacado
(task de UI 010, pedidos 4 e 5 do Samyr).

Pedido 4: campo de BUSCA filtrando os resultados; modos CARDS e LISTA alternáveis;
o TIMEFRAME destacado no INÍCIO da linha — antes saía colado no preço
(``1d$513,530.15%``), sem respiro e sem hierarquia.

Pedido 5 (consequência do DA-070): ``.scan-frame-row`` e ``.scan-row`` trocam o
degradê de estado por borda-esquerda sólida; nada de gradiente em card, linha ou
chip. Isto é testável de verdade — ``getComputedStyle`` denuncia o gradiente.
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


def _frame(frame, estado, **kw):
    base = {"frame": frame, "estado": estado, "direction": "compra", "price": 513.53,
            "dist_pct": 0.0015, "dist_txt": "0.15%", "trigger": 512.76, "sl": 471.35,
            "tp": 515.06, "rr": 0.06, "rr_note": None, "pattern_state": "formando",
            "rr_entry": 512.76, "rr_basis": "gatilho — rompimento da máxima do ponto 2",
            "rr_risco": 41.41, "rr_retorno": 2.3, "rr_residual": False}
    base.update(kw)
    return base


_SCAN = {
    "date": "2026-08-29", "frames": ["1d", "4h", "1h"],
    "resumo": {"em_gatilho": 2, "formando": 1},
    "ativos": [
        {"ticker": "MSFT", "melhor": _frame("1d", "em_gatilho"),
         "frames": [_frame("1d", "em_gatilho"), _frame("4h", "formando"),
                    _frame("1h", "em_movimento")]},
        {"ticker": "NVDA", "melhor": _frame("1d", "em_gatilho"),
         "frames": [_frame("1d", "em_gatilho", price=217.55)]},
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


def _abre_scan(page, base):
    def handler(route):
        url = route.request.url
        if "/api/scan" in url and "verdicts" not in url and "/salvo" not in url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_SCAN))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)
    page.goto(base, wait_until="networkidle")
    # A visão padrão passou a ser SINAIS (DA-117). Estes testes medem a visão de
    # DADO (Cards/Lista), então ela é escolhida explicitamente — como um usuário
    # que clicou em "Cards" faria. O `/api/scan/salvo` não é mockado: vai ao
    # servidor de verdade, cujo results_dir é um tmp_path sem nada salvo.
    page.evaluate("() => localStorage.setItem('td_scan_view', 'cards')")
    page.reload(wait_until="networkidle")
    page.click("#scanOpenBtn")
    page.click("#scanRunBtn")
    page.wait_for_selector("#scanList li")


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_busca_filtra_a_lista(base):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_scan(page, base)
        assert page.locator("#scanList > li").count() == 2
        page.fill("#scanSearch", "nv")            # minúscula: a busca não é sensível
        page.wait_for_timeout(120)
        assert page.locator("#scanList > li").count() == 1
        assert "NVDA" in page.inner_text("#scanList")
        page.fill("#scanSearch", "XPTO")
        page.wait_for_timeout(120)
        assert page.locator(".scan-vazio").count() == 1   # ausência DECLARADA
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_dois_modos_de_apresentacao_e_a_escolha_persiste(base):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_scan(page, base)
        # CARDS: agrupa por ativo (2 li) e cada frame é uma sub-linha
        assert page.locator("#scanList > li.scan-row").count() == 2
        assert page.locator(".scan-frame-row").count() == 4

        page.click(".scan-view[data-view='lista']")
        page.wait_for_timeout(150)
        # LISTA: uma linha por ativo+frame, sem agrupar (4 linhas)
        assert page.locator("#scanList > li.scan-line-row").count() == 4
        assert page.locator("#scanList > li.scan-row").count() == 0
        assert page.evaluate("() => localStorage.getItem('td_scan_view')") == "lista"

        page.reload(wait_until="networkidle")
        page.click("#scanOpenBtn")
        page.click("#scanRunBtn")
        page.wait_for_selector("#scanList li")
        assert page.locator("#scanList > li.scan-line-row").count() == 4, "o modo não persistiu"
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_timeframe_destacado_no_inicio_da_linha(base):
    """Antes: ``1d$513,530.15%`` — o frame colado no preço. Agora o badge vem
    primeiro, com caixa própria e respiro."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_scan(page, base)
        m = page.evaluate("""() => {
          const row = document.querySelector('.scan-frame-row');
          const tf = row.querySelector('.scan-tf');
          const pr = row.querySelector('.scan-price');
          const cs = getComputedStyle(tf);
          return {tfLeft: tf.getBoundingClientRect().left, prLeft: pr.getBoundingClientRect().left,
                  gap: pr.getBoundingClientRect().left - tf.getBoundingClientRect().right,
                  minW: parseFloat(cs.minWidth), borda: cs.borderLeftWidth,
                  txt: row.innerText.slice(0, 12)};
        }""")
        assert m["tfLeft"] < m["prLeft"], m          # o frame vem ANTES do preço
        assert m["gap"] >= 6, ("sem respiro entre frame e preço", m)
        # largura FIXA (as linhas de um ativo alinham em coluna) e estreita — a
        # segunda rodada do pedido reduziu a pill; o que não pode é ela sumir.
        assert 20 <= m["minW"] <= 40, m
        assert m["borda"] != "0px", m                # é um badge, não texto solto
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_DA070_sem_degrade_e_sem_canto_arredondado_nas_linhas(base):
    """DA-070 conferida no que o navegador REALMENTE aplica, nos dois modos."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_scan(page, base)
        for seletor in (".scan-row", ".scan-frame-row"):
            m = page.evaluate(f"""() => {{
              const els = [...document.querySelectorAll('{seletor}')];
              return els.map(e => {{ const c = getComputedStyle(e);
                return {{bg: c.backgroundImage, raio: c.borderTopLeftRadius,
                         bordaEsq: c.borderLeftWidth}}; }});
            }}""")
            assert m, seletor
            assert all(x["bg"] == "none" for x in m), (seletor, m)      # zero degradê
            assert all(x["raio"] == "0px" for x in m), (seletor, m)     # card quadrado
        # o estado vira BORDA-ESQUERDA sólida (a cor continua informando)
        borda = page.evaluate("""() => getComputedStyle(
            document.querySelector('.scan-frame-row.em_gatilho')).borderLeftWidth""")
        assert borda != "0px", borda

        page.click(".scan-view[data-view='lista']")
        page.wait_for_timeout(150)
        m = page.evaluate("""() => [...document.querySelectorAll('.scan-line-row')]
            .map(e => { const c = getComputedStyle(e);
              return {bg: c.backgroundImage, raio: c.borderTopLeftRadius}; })""")
        assert m and all(x["bg"] == "none" and x["raio"] == "0px" for x in m), m
        browser.close()


# ---------------------------- segunda rodada de pedidos (task 012) --------------
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_cards_em_grade_de_ate_tres_colunas(base):
    """A visão Cards empilhava em coluna única e desperdiçava a largura (tela de
    1500px+). Vira grade — com TETO de 3, que é o pedido, e quebra pra baixo."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_scan(page, base)
        def colunas():
            return page.evaluate(
                "() => getComputedStyle(document.querySelector('#scanList'))"
                ".gridTemplateColumns.split(' ').length")

        def largura_coluna(px):
            # Arrasta a lateral: é a largura do CONTAINER que manda, não a do
            # viewport (mesma lição da barra de controle — a lateral é
            # redimensionável e o viewport não sabe disso).
            page.evaluate(
                "(w) => document.querySelector('main.layout')"
                ".style.setProperty('--sidebar-w', w + 'px')", px)
            page.wait_for_timeout(150)

        assert colunas() == 3, "coluna larga → 3 (o teto pedido)"
        largura_coluna(600)
        assert colunas() == 2, "coluna média → 2"
        largura_coluna(950)
        assert colunas() == 1, "coluna estreita → 1"
        largura_coluna(300)
        page.wait_for_timeout(150)
        page.click(".scan-view[data-view='lista']")
        page.wait_for_timeout(200)
        assert colunas() == 1, "lista não vira grade"
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_timeframe_em_pill_estreita_com_rotulo_curto(base):
    """Segunda rodada da mesma queixa: o chip com caixa própria resolveu o "colado
    no preço" e ficou GORDO, um por linha, competindo com o ativo. Vira pill
    estreita na gramática da barra de controle (S · D · 4h · 1h)."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_scan(page, base)
        m = page.evaluate("""() => {
          const tf = document.querySelector('.scan-frame-row .scan-tf');
          const tk = document.querySelector('.scan-tk');
          return {txt: tf.innerText.trim(), titulo: tf.getAttribute('title'),
                  larg: Math.round(tf.getBoundingClientRect().width),
                  fonteTf: parseFloat(getComputedStyle(tf).fontSize),
                  fonteTk: parseFloat(getComputedStyle(tk).fontSize)};
        }""")
        assert m["txt"] == "D", ("1d vira D, como na barra de controle", m)
        assert m["titulo"] == "1d", ("o frame completo fica no title", m)
        assert m["larg"] <= 40, ("pill ESTREITA", m)
        assert m["fonteTf"] < m["fonteTk"], ("o ATIVO é quem tem destaque", m)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_rr_residual_vira_texto_honesto_e_nao_zero_cru(base):
    """MSFT 1h de 29/08: acionado, gatilho 497,14, TP 513,73, preço 513,67 → R:R
    0.00. O número está certo (a entrada de um setup acionado é o preço atual), mas
    "0.00" lê-se como "setup sem retorno". A tela passa a dizer o que ele significa
    — e, quando o R:R vale, declara que foi medido do preço atual."""
    scan = json.loads(json.dumps(_SCAN))
    scan["ativos"][0]["frames"] = [_frame(
        "1h", "em_movimento", pattern_state="acionado", trigger=497.14, sl=484.97,
        tp=513.73, price=513.67, rr=0.0, rr_entry=513.67, rr_risco=28.70,
        rr_retorno=0.06, rr_residual=True,
        rr_basis="preço atual (padrão já acionado)")]

    def handler(route):
        url = route.request.url
        if "/api/scan" in url and "verdicts" not in url and "/salvo" not in url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(scan))
        else:
            route.continue_()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.route(re.compile(r"/api/"), handler)
        page.goto(base, wait_until="networkidle")
        page.evaluate("() => localStorage.setItem('td_scan_view', 'cards')")
        page.reload(wait_until="networkidle")
        page.click("#scanOpenBtn")
        page.click("#scanRunBtn")
        page.wait_for_selector("#scanList li")
        txt = page.inner_text("#scanList")
        assert "alvo praticamente alcançado" in txt, txt
        assert "0.00" not in txt, ("o zero cru sumiu da tela", txt)
        assert "sobrou" in txt and "28,7" in txt, ("diz o que sobrou pra quanto de risco", txt)

        # e o caso NÃO-residual acionado declara a base da entrada
        scan2 = json.loads(json.dumps(_SCAN))
        scan2["ativos"][0]["frames"] = [_frame("1h", "em_movimento", pattern_state="acionado",
                                               rr=0.66, rr_basis="preço atual (padrão já acionado)")]
        page.unroute(re.compile(r"/api/"))
        page.route(re.compile(r"/api/"), lambda r: (
            r.fulfill(status=200, content_type="application/json", body=json.dumps(scan2))
            if "/api/scan" in r.request.url and "verdicts" not in r.request.url
               and "/salvo" not in r.request.url else r.continue_()))
        page.click("#scanRunBtn")
        page.wait_for_timeout(400)
        txt2 = page.inner_text("#scanList")
        assert "0.66" in txt2 and "do preço atual" in txt2, txt2
        browser.close()

"""O scan não pode apagar o resultado anterior enquanto varre (task 014).

O defeito: ``runScan`` fazia ``scanList.innerHTML = ""`` ANTES do fetch. A
varredura leva 7,5s quente e ~12s fria, então não era um piscar — era o painel
VAZIO por vários segundos. Pior no ``catch``: só o resumo virava mensagem de
erro, e a lista já tinha sido zerada e não voltava. Um scan que falha (rede,
provedor, timeout) DESTRUÍA o último resultado bom e deixava o usuário sem nada.

É a mesma família do "erro no meio preserva as etapas concluídas": informação
boa não se descarta por causa de uma atualização que ainda não chegou.

Os três testes têm DENTE porque cada um falha na implementação antiga por um
motivo diferente: (a) durante o fetch a lista some; (b) na falha ela some E não
volta; (c) a primeira carga não pode ser confundida com "preservar" nada.

O fetch é represado DENTRO da página (``window.fetch`` trocado por um portão com
resolve/reject expostos) em vez de por ``page.route`` com sleep: o handler
síncrono do Playwright bloquearia o dispatcher e a asserção "enquanto pendura"
não teria como rodar.
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
            "rr_entry": 512.76, "rr_basis": "gatilho", "rr_risco": 41.41,
            "rr_retorno": 2.3, "rr_residual": False}
    base.update(kw)
    return base


_SCAN = {
    "date": "2026-08-29", "frames": ["1d", "4h", "1h"],
    "resumo": {"em_gatilho": 2, "formando": 1},
    "ativos": [
        {"ticker": "MSFT", "melhor": _frame("1d", "em_gatilho"),
         "frames": [_frame("1d", "em_gatilho"), _frame("4h", "formando")]},
        {"ticker": "NVDA", "melhor": _frame("1d", "em_gatilho"),
         "frames": [_frame("1d", "em_gatilho", price=217.55)]},
    ],
}
# Segundo scan, diferente do primeiro — prova que a SUBSTITUIÇÃO acontece quando
# o dado novo chega (preservar não pode virar "nunca atualiza").
_SCAN2 = json.loads(json.dumps(_SCAN))
_SCAN2["ativos"] = [{"ticker": "AMD", "melhor": _frame("1d", "em_gatilho"),
                     "frames": [_frame("1d", "em_gatilho", price=142.10)]}]
_SCAN2["resumo"] = {"em_gatilho": 1}


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


def _abre(page, base, payload=_SCAN):
    """Abre o painel e faz UM scan que dá certo — é o 'anterior' a preservar."""
    def handler(route):
        # `/api/scan/salvo` (o último scan em disco) NÃO é mockado: ele vai ao
        # servidor de verdade, cujo results_dir é um tmp_path sem nada salvo.
        # Assim estes três testes continuam medindo o que mediam — a preservação
        # DENTRO da sessão —, sem o painel já nascer com um resultado.
        url = route.request.url
        if "/api/scan" in url and "verdicts" not in url and "/salvo" not in url:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(payload))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)
    page.goto(base, wait_until="networkidle")
    # A visão padrão passou a ser SINAIS (DA-117). Este arquivo mede a
    # preservação do resultado na visão de DADO, então ela é escolhida
    # explicitamente — o assunto do teste é o dado que fica na tela, não
    # qual apresentação está ativa.
    page.evaluate("() => localStorage.setItem('td_scan_view', 'cards')")
    page.reload(wait_until="networkidle")
    page.click("#scanOpenBtn")
    page.click("#scanRunBtn")
    page.wait_for_selector("#scanList li")


_PORTAO = """() => {
  // Represa o PRÓXIMO /api/scan: devolve uma promessa que o teste resolve ou
  // rejeita quando quiser, congelando a UI no meio da varredura.
  window.__portao = {};
  const orig = window.fetch;
  window.fetch = (u, o) => {
    if (String(u).includes('/api/scan?')) {
      return new Promise((res, rej) => {
        window.__portao.ok = (body) => res(new Response(body, {
          status: 200, headers: {'Content-Type': 'application/json'}}));
        window.__portao.falha = (msg) => rej(new TypeError(msg));
      });
    }
    return orig(u, o);
  };
}"""


def _tickers(page):
    return page.evaluate(
        """() => [...document.querySelectorAll('#scanList .scan-tk')].map(e => e.textContent)""")


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_lista_anterior_fica_na_tela_durante_a_varredura(base):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre(page, base)
        assert _tickers(page) == ["MSFT", "NVDA"]

        page.evaluate(_PORTAO)
        page.click("#scanRunBtn")
        page.wait_for_function("() => !!window.__portao.ok")
        page.wait_for_timeout(120)

        # DENTE: na implementação antiga isto era [] — a lista tinha sido zerada.
        assert _tickers(page) == ["MSFT", "NVDA"], "o scan anterior sumiu da tela"
        m = page.evaluate("""() => ({
          esmaecida: document.querySelector('#scanList').classList.contains('is-atualizando'),
          botao: document.querySelector('#scanRunBtn').textContent,
          aviso: document.querySelector('#scanNotice').textContent,
          avisoVisivel: !document.querySelector('#scanNotice').classList.contains('hidden'),
          cobertura: document.querySelectorAll('#scanPanel .spinner, #scanPanel .overlay').length,
          carimbo: document.querySelector('#scanCarimbo').textContent,
          carimboVisivel: !document.querySelector('#scanCarimbo').classList.contains('hidden'),
        })""")
        assert m["esmaecida"], m           # indicador discreto: opacidade, não spinner
        assert m["cobertura"] == 0, m      # nada cobrindo o conteúdo
        assert "escaneando" in m["botao"], m
        assert m["avisoVisivel"] and "atualizando" in m["aviso"], m
        # A HORA do dado exibido saiu do aviso e virou linha PERMANENTE (task
        # 20260831-014): o painel agora abre com o último scan salvo, então o
        # carimbo tem de existir sempre, não só enquanto atualiza. Repetir o mesmo
        # horário nos dois lugares seria a duplicata da DA-077.
        assert m["carimboVisivel"], ("falta o carimbo do dado exibido", m)
        assert re.search(r"\d{2}:\d{2}", m["carimbo"]), ("falta a hora do dado exibido", m)
        assert not re.search(r"\d{2}:\d{2}", m["aviso"]), ("hora duplicada no aviso", m)

        # e quando o novo chega, SUBSTITUI (preservar ≠ congelar)
        page.evaluate("(b) => window.__portao.ok(b)", json.dumps(_SCAN2))
        page.wait_for_function("""() => document.querySelector('#scanRunBtn').textContent === 'Escanear'""")
        assert _tickers(page) == ["AMD"]
        assert page.evaluate(
            """() => document.querySelector('#scanNotice').classList.contains('hidden')""")
        assert not page.evaluate(
            """() => document.querySelector('#scanList').classList.contains('is-atualizando')""")
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_scan_que_falha_preserva_o_anterior_e_diz_de_quando_ele_e(base):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre(page, base)
        resumo_antes = page.evaluate("""() => document.querySelector('#scanSummary').textContent""")

        page.evaluate(_PORTAO)
        page.click("#scanRunBtn")
        page.wait_for_function("() => !!window.__portao.falha")
        page.evaluate("() => window.__portao.falha('Failed to fetch')")
        page.wait_for_function("""() => document.querySelector('#scanRunBtn').textContent === 'Escanear'""")

        # DENTE: antes, a falha deixava a tela VAZIA — a lista já tinha sido zerada
        # na linha 4785 e o catch só mexia no resumo.
        assert _tickers(page) == ["MSFT", "NVDA"], "a falha destruiu o resultado bom"
        m = page.evaluate("""() => ({
          aviso: document.querySelector('#scanNotice').textContent,
          erro: document.querySelector('#scanNotice').classList.contains('err'),
          visivel: !document.querySelector('#scanNotice').classList.contains('hidden'),
          resumo: document.querySelector('#scanSummary').textContent,
          esmaecida: document.querySelector('#scanList').classList.contains('is-atualizando'),
          carimbo: document.querySelector('#scanCarimbo').textContent,
          carimboVisivel: !document.querySelector('#scanCarimbo').classList.contains('hidden'),
        })""")
        assert m["visivel"] and m["erro"], m
        assert "falhou" in m["aviso"], m
        assert "Failed to fetch" in m["aviso"], ("o aviso tem que dizer O QUE falhou", m)
        # DE QUANDO é o dado continua obrigatório — agora no carimbo permanente.
        assert m["carimboVisivel"], ("falta DE QUANDO é o dado na tela", m)
        assert re.search(r"\d{2}:\d{2}", m["carimbo"]), ("falta DE QUANDO é o dado na tela", m)
        assert m["resumo"] == resumo_antes, ("o resumo do scan bom foi sobrescrito", m)
        assert not m["esmaecida"], ("acabou de atualizar: sai o esmaecido", m)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_primeira_carga_sem_anterior_mostra_a_varredura_e_nao_inventa_lista(base):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.goto(base, wait_until="networkidle")
        page.evaluate("() => localStorage.setItem('td_scan_view', 'cards')")
        page.reload(wait_until="networkidle")
        # O portão entra ANTES de abrir o painel: `openScanPanel` já dispara o
        # primeiro `runScan`, então é ELE a primeira carga — abrir e só depois
        # represar já teria um "anterior" na mão e o teste testaria outra coisa.
        page.evaluate(_PORTAO)
        page.click("#scanOpenBtn")
        page.wait_for_function("() => !!window.__portao.ok")
        page.wait_for_timeout(120)

        m = page.evaluate("""() => ({
          resumo: document.querySelector('#scanSummary').textContent,
          itens: document.querySelectorAll('#scanList li').length,
          avisoVisivel: !document.querySelector('#scanNotice').classList.contains('hidden'),
          esmaecida: document.querySelector('#scanList').classList.contains('is-atualizando'),
          carimboVisivel: !document.querySelector('#scanCarimbo').classList.contains('hidden'),
        })""")
        assert "varrendo" in m["resumo"], m       # não há o que preservar: texto de varredura
        assert m["itens"] == 0, m                 # e nada de lista vazia inventada
        assert not m["avisoVisivel"], ("sem anterior não existe 'mostrando o de tal hora'", m)
        assert not m["esmaecida"], ("nada a esmaecer na primeira carga", m)
        assert not m["carimboVisivel"], ("sem dado na tela não há carimbo de hora", m)

        # e a falha na PRIMEIRA carga continua dizendo o erro no resumo
        page.evaluate("() => window.__portao.falha('Failed to fetch')")
        page.wait_for_function("""() => document.querySelector('#scanRunBtn').textContent === 'Escanear'""")
        assert "Failed to fetch" in page.evaluate(
            """() => document.querySelector('#scanSummary').textContent""")
        browser.close()

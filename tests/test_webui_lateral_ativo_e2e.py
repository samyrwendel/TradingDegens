"""A lateral: o ATIVO nunca é a informação sacrificada (task 20260830-002).

Print do Samyr: "além do veredito ficar cobrindo o nome e as informações do ativo".
Na largura padrão os itens saíam `AMI…` com o nome `A…` e o preço cortado, enquanto
"Aguardar rompimento" aparecia INTEIRO ao lado.

A causa era a grade: `minmax(0, 1fr) auto auto`. A faixa `auto` do veredito, com texto
`nowrap`, toma o tamanho do conteúdo ANTES de a faixa `fr` receber o que sobra — então
o rótulo sobrevivia e a IDENTIDADE da linha sumia. Está invertido: o ativo é a chave.

O que estes testes travam, do mínimo (200px) ao máximo do resizer:
  * o SÍMBOLO do ativo nunca sai cortado;
  * o PREÇO nunca sai cortado — ele passou a ocupar a fileira inteira, porque na
    coluna 1 de uma lateral de 200px ele tinha 74px e precisa de ~100;
  * quem trunca é o VEREDITO, com o texto completo no ``title``;
  * zero pictograma na lateral (DA-076), com o estado ainda distinguível por cor.
"""

import json
import re
import threading

import pytest

from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.store import HistoryStore

pytestmark = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None

# As larguras que o resizer permite: mínimo declarado em app.js (_SIDEBAR_MIN = 200),
# o padrão do CSS (280) e arrastadas até o teto.
LARGURAS = (200, 240, 280, 340, 420, 560)

# "Aguardar rompimento" é o pior caso real do veredito 1-2-3 — é por ele que a
# coluna tem de ser dimensionada, e é ele que trunca quando não cabe.
_HIST = {"runs": [
    {"run_id": "R1", "ticker": "AMD", "date": "2026-08-29", "status": "done",
     "method": "setup123", "setup_state": "aguardar_rompimento", "verdict": None,
     "asset_type": "stock", "cost_usd": 0.0, "elapsed": 2, "count": 3},
    {"run_id": "R2", "ticker": "ZEC-USD", "date": "2026-08-29", "status": "done",
     "method": "setup123", "setup_state": "aguardar_pullback", "verdict": None,
     "asset_type": "crypto", "cost_usd": 0.0, "elapsed": 2, "count": 1},
    {"run_id": "R3", "ticker": "AMZN", "date": "2026-08-29", "status": "done",
     "method": "padrao", "verdict": "HOLD", "asset_type": "stock",
     "cost_usd": 0.1, "elapsed": 300, "count": 9},
    {"run_id": "R4", "ticker": "GOOGL", "date": "2026-08-29", "status": "done",
     "method": "setup123", "setup_state": "ativo", "verdict": None,
     "asset_type": "stock", "cost_usd": 0.0, "elapsed": 2, "count": 2},
]}

_MEDE = """() => [...document.querySelectorAll('.history li')].map(li => {
  const corta = (e) => e ? e.scrollWidth > e.clientWidth + 1 : false;
  const sym = li.querySelector('.h-sym');
  const ver = li.querySelector('.h-verdict');
  const pr = li.querySelector('.h-price');
  return {
    ticker: sym ? sym.innerText.trim() : '',
    simboloCortado: corta(sym),
    precoCortado: corta(pr),
    veredito: ver ? ver.innerText.trim() : '',
    vereditoCortado: corta(ver),
    vereditoTitle: ver ? (ver.getAttribute('title') || '') : '',
    corDoVeredito: ver ? getComputedStyle(ver).color : '',
  };
})"""


@pytest.fixture
def base(tmp_path):
    from tradingagents.webui.server import make_server

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


def _abre(page, base_url, largura):
    def handler(route):
        if "/api/history" in route.request.url:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(_HIST))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)
    page.goto(base_url, wait_until="networkidle")
    page.evaluate("(w) => document.querySelector('main.layout')"
                  ".style.setProperty('--sidebar-w', w + 'px')", largura)
    page.wait_for_selector(".history li", state="attached")
    page.wait_for_timeout(250)
    return page.evaluate(_MEDE)


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("largura", LARGURAS)
def test_o_ativo_nunca_e_a_informacao_sacrificada(base, largura):
    """DENTE: era o TICKER que virava "AMI…" enquanto o veredito saía inteiro."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        itens = _abre(page, base, largura)
        assert len(itens) == 4, itens
        cortados = [i for i in itens if i["simboloCortado"] or i["precoCortado"]]
        assert cortados == [], (f"o ativo foi cortado em {largura}px", cortados)
        # e o ticker está lá INTEIRO, não uma sobra dele
        assert {i["ticker"].split()[0] for i in itens} == {"AMD", "ZEC-USD", "AMZN", "GOOGL"}, itens
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_quem_trunca_e_o_VEREDITO_e_o_texto_inteiro_fica_no_title(base):
    """A inversão pedida: apertado, quem cede é o rótulo — e nada se perde, porque a
    frase completa continua acessível no ``title``."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        itens = _abre(page, base, 200)
        truncados = [i for i in itens if i["vereditoCortado"]]
        assert truncados, ("na largura mínima ALGUM veredito longo tem de ceder — "
                           "senão é o ativo que está cedendo", itens)
        for i in truncados:
            assert i["vereditoTitle"], ("truncar sem title é perder informação", i)
        # o pior caso real do 1-2-3 leva a frase completa no title
        amd = next(i for i in itens if i["ticker"].startswith("AMD"))
        assert "rompimento" in amd["vereditoTitle"].lower(), amd
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_na_largura_maxima_ninguem_trunca(base):
    """O outro lado da mesma régua: com espaço, tudo aparece inteiro — a coluna não
    ficou travada num teto pequeno."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        itens = _abre(page, base, 560)
        assert [i for i in itens if i["vereditoCortado"]] == [], itens
        assert [i for i in itens if i["simboloCortado"] or i["precoCortado"]] == [], itens
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_zero_pictograma_na_lateral_e_o_estado_ainda_se_distingue(base):
    """DA-076 no canto mais estreito da tela, onde cada pictograma roubava largura de
    um nome que já truncava. O estado continua legível de relance: cor + palavra."""
    faixas = ((0x2300, 0x23FF), (0x25A0, 0x25FF), (0x2600, 0x27BF),
              (0x2B00, 0x2BFF), (0x1F000, 0x1FAFF), (0xFE00, 0xFE0F))
    tipograficos = set("↻→←↑↓↔↗⇒✕×·—–…≥≤±✓○●◦▸▾")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        itens = _abre(page, base, 280)
        txt = page.inner_text(".sidebar")
        fora = [c for c in txt
                if c not in tipograficos and any(a <= ord(c) <= b for a, b in faixas)]
        assert fora == [], ("pictograma na lateral", fora)
        # e o estado continua distinguível: "Ativo" (verde) × "Aguardar…" (âmbar)
        cores = {i["veredito"]: i["corDoVeredito"] for i in itens}
        ativo = next(v for k, v in cores.items() if k.startswith("Ativo"))
        aguardar = next(v for k, v in cores.items() if k.startswith("Aguardar"))
        assert ativo != aguardar, cores
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_no_telefone_a_lateral_nao_estoura_a_pagina(base):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        _abre(page, base, 280)
        rola = page.evaluate("() => document.documentElement.scrollWidth > "
                             "document.documentElement.clientWidth")
        assert not rola
        browser.close()

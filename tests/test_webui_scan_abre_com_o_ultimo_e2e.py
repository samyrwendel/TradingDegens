"""Abrir o painel já mostra a última varredura conhecida (task 20260831-014).

O scan já rodava sozinho ao abrir (``openScanPanel`` → ``runScan``). O que não
havia era o que mostrar ENQUANTO ele roda: a task 014 fez o resultado anterior
sobreviver a uma re-varredura, mas só dentro da sessão do navegador — na
primeira carga não existe anterior, e a tela ficava vazia os 8–20s da varredura
(20,0s medidos depois de o Storm entrar).

Agora o servidor guarda o último scan COMPLETO em disco e a página o pinta antes
de a varredura começar. Os dentes:

* **(a)** com scan salvo, os itens estão na tela ENQUANTO a varredura ainda
  pendura — na implementação antiga a lista estava vazia até ela chegar;
* **(b)** o carimbo do dado é o do SERVIDOR (``gerado_em``), não o relógio de
  quando o JSON chegou: um scan das 09:07 lido do disco às 15h continua dizendo
  09:07, e a task 014 sozinha diria 15h;
* **(c)** salvo de ONTEM se declara — o dia entra na frente da hora e a linha
  ganha o aviso; só "14:32" seria indistinguível de um scan de agora;
* **(d)** quando a varredura nova chega, ela SUBSTITUI e o carimbo rejuvenesce
  (mostrar o velho não pode virar congelar o velho);
* **(e)** sem nada salvo, nada muda: "varrendo…", zero itens, zero carimbo — a
  lista vazia inventada seria lida como "não há nada em gatilho".

A varredura é represada DENTRO da página (``window.fetch``), não por
``page.route`` com sleep: o handler síncrono do Playwright bloquearia o
dispatcher inteiro e a asserção "enquanto pendura" não teria como rodar.
"""

import json
import re
import threading
from datetime import datetime, timedelta

import pytest

from tradingagents.webui import timeutil
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


def _scan(tickers, gerado_em):
    return {"date": "2026-08-31", "frames": ["1d", "4h", "1h"],
            "resumo": {"em_gatilho": len(tickers)}, "gerado_em": gerado_em,
            "ativos": [{"ticker": t, "melhor": _frame("1d", "em_gatilho"),
                        "frames": [_frame("1d", "em_gatilho")]} for t in tickers]}


def _hoje_as(h, m):
    """Carimbo de HOJE em Manaus, na hora pedida — comparável ao que a tela mostra."""
    return timeutil.stamp(timeutil.now().replace(hour=h, minute=m, second=0, microsecond=0))


def _ontem_ao_meio_dia():
    """Ontem 12:00 em Manaus.

    Meio-dia de propósito: o navegador decide "ontem" pelo fuso DELE, e um
    carimbo perto da meia-noite mudaria de dia com o fuso do container. O
    contexto já é fixado em Manaus abaixo; o meio-dia é o cinto por cima.
    """
    ontem = datetime.now(timeutil.MANAUS) - timedelta(days=1)
    return timeutil.stamp(ontem.replace(hour=12, minute=0, second=0, microsecond=0))


@pytest.fixture
def base(tmp_path):
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "openai",
                     "deep_think_llm": "x", "quick_think_llm": "y"},
        store=HistoryStore(tmp_path))
    httpd = make_server("127.0.0.1", 0, runner=runner)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", runner
    finally:
        httpd.shutdown()


# Represa o PRÓXIMO ``/api/scan?…`` (a varredura). O ``/api/scan/salvo`` não tem
# query string e passa direto pro servidor de verdade — é justamente ele que tem
# de responder rápido enquanto a varredura pendura.
_PORTAO = """() => {
  window.__portao = {};
  const orig = window.fetch;
  window.fetch = (u, o) => {
    if (String(u).includes('/api/scan?')) {
      return new Promise((res, rej) => {
        window.__portao.ok = (body) => res(new Response(body, {
          status: 200, headers: {'Content-Type': 'application/json'}}));
      });
    }
    return orig(u, o);
  };
}"""

_ESTADO = """() => ({
  tickers: [...document.querySelectorAll('#scanList .scan-tk')].map(e => e.textContent),
  carimbo: document.querySelector('#scanCarimbo').textContent,
  carimboVisivel: !document.querySelector('#scanCarimbo').classList.contains('hidden'),
  velho: document.querySelector('#scanCarimbo').classList.contains('is-velho'),
  botao: document.querySelector('#scanRunBtn').textContent,
  esmaecida: document.querySelector('#scanList').classList.contains('is-atualizando'),
  resumo: document.querySelector('#scanSummary').textContent,
  aviso: document.querySelector('#scanNotice').textContent,
  pendurado: !!window.__portao.ok,
})"""


def _abre_com_portao(page, url):
    page.goto(url, wait_until="networkidle")
    page.evaluate(_PORTAO)
    page.click("#scanOpenBtn")


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_abre_com_o_scan_salvo_antes_de_a_varredura_terminar(base):
    url, runner = base
    runner.scan_snapshot.save(_scan(["MSFT", "NVDA"], _hoje_as(9, 7)))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1500, "height": 950},
                                  timezone_id="America/Manaus")
        page = ctx.new_page()
        _abre_com_portao(page, url)

        # DENTE (a): antes, aqui não havia UM item — a lista só nascia com o fetch.
        page.wait_for_selector("#scanList li", timeout=5000)
        m = page.evaluate(_ESTADO)
        assert m["pendurado"], ("a varredura já tinha terminado — o teste não "
                                "provou nada", m)
        assert "escaneando" in m["botao"], m
        assert m["tickers"] == ["MSFT", "NVDA"], m
        # DENTE (b): a hora é a do SERVIDOR, não a de agora.
        assert m["carimboVisivel"] and "09:07" in m["carimbo"], m
        assert not m["velho"], ("scan de hoje não se marca como velho", m)
        assert m["esmaecida"], ("está atualizando: o salvo aparece esmaecido", m)
        assert "atualizando" in m["aviso"], m

        # DENTE (d): o novo chega e SUBSTITUI — carimbo inclusive.
        page.evaluate("(b) => window.__portao.ok(b)", json.dumps(
            _scan(["AMD"], _hoje_as(15, 42))))
        page.wait_for_function(
            """() => document.querySelector('#scanRunBtn').textContent === 'Escanear'""")
        m2 = page.evaluate(_ESTADO)
        assert m2["tickers"] == ["AMD"], m2
        assert "15:42" in m2["carimbo"] and "09:07" not in m2["carimbo"], m2
        assert not m2["esmaecida"] and m2["aviso"] == "", m2
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_scan_salvo_de_ontem_se_declara_na_tela(base):
    url, runner = base
    runner.scan_snapshot.save(_scan(["BTC-USD"], _ontem_ao_meio_dia()))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1500, "height": 950},
                                  timezone_id="America/Manaus")
        page = ctx.new_page()
        _abre_com_portao(page, url)
        page.wait_for_selector("#scanList li", timeout=5000)

        m = page.evaluate(_ESTADO)
        assert m["tickers"] == ["BTC-USD"], m
        # DENTE (c): só "12:00" seria indistinguível de um scan de agora.
        assert m["velho"], ("dado de ontem servido como se fosse de hoje", m)
        assert "ontem" in m["carimbo"].lower(), m
        assert "não é de hoje" in m["carimbo"], m
        assert re.search(r"\d{2}:\d{2}", m["carimbo"]), m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_sem_nada_salvo_a_abertura_continua_como_era(base):
    url, _ = base                       # results_dir limpo: nunca houve scan
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1500, "height": 950},
                                  timezone_id="America/Manaus")
        page = ctx.new_page()
        _abre_com_portao(page, url)
        page.wait_for_function("() => !!window.__portao.ok")
        page.wait_for_timeout(150)

        m = page.evaluate(_ESTADO)
        # DENTE (e): nada de lista vazia inventada nem carimbo de hora nenhuma.
        assert m["tickers"] == [], m
        assert "varrendo" in m["resumo"], m
        assert not m["carimboVisivel"], ("sem dado na tela não há carimbo", m)
        assert not m["esmaecida"], m
        assert m["aviso"] == "", m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_salvo_sem_carimbo_do_servidor_nao_entra_na_tela(base):
    """Sem ``gerado_em`` não há como datar — e datar errado é pior que não mostrar.

    O fallback do carimbo é o relógio local, que diria AGORA para um payload que
    pode ter dias. Um salvo sem carimbo é descartado e a tela cai na primeira
    carga: melhor esperar a varredura do que exibir dado velho com cara de novo.
    """
    url, runner = base
    sem_carimbo = _scan(["MSFT"], _hoje_as(9, 7))
    sem_carimbo.pop("gerado_em")
    runner.scan_snapshot.save(sem_carimbo)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1500, "height": 950},
                                  timezone_id="America/Manaus")
        page = ctx.new_page()
        _abre_com_portao(page, url)
        page.wait_for_function("() => !!window.__portao.ok")
        page.wait_for_timeout(150)

        m = page.evaluate(_ESTADO)
        assert m["tickers"] == [], ("dado sem data foi pra tela", m)
        assert "varrendo" in m["resumo"], m
        assert not m["carimboVisivel"], m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_no_celular_o_carimbo_cabe_e_nao_estoura(base):
    """390×844 (DA-062/DA-101): a linha nova não pode empurrar a largura da tela."""
    url, runner = base
    runner.scan_snapshot.save(_scan(["BTC-USD"], _ontem_ao_meio_dia()))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 390, "height": 844},
                                  timezone_id="America/Manaus")
        page = ctx.new_page()
        page.goto(url, wait_until="networkidle")
        page.evaluate(_PORTAO)
        # No celular a lista de observação nasce RECOLHIDA (syncHistoryCollapse
        # fecha o <details> abaixo de 900px), e o botão do scan mora dentro dela.
        page.evaluate("() => { document.getElementById('historyPanel').open = true; }")
        page.click("#scanOpenBtn")
        page.wait_for_selector("#scanList li", timeout=5000)

        m = page.evaluate("""() => {
          const el = document.querySelector('#scanCarimbo');
          const r = el.getBoundingClientRect();
          return {dir: r.right, esq: r.left, doc: document.documentElement.scrollWidth,
                  vis: getComputedStyle(el).display !== 'none', txt: el.textContent};
        }""")
        assert m["vis"] and "ontem" in m["txt"].lower(), m
        assert m["esq"] >= 0 and m["dir"] <= 390.5, ("o carimbo vazou do viewport", m)
        assert m["doc"] <= 390, ("a página passou a rolar na horizontal", m)
        browser.close()

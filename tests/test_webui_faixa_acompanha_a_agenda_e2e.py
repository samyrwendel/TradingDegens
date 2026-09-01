"""A FAIXA DE FRAMES do card ACOMPANHA a agenda — sem F5 (DA-141).

O Samyr: *"no gráfico temos o setup atualizado e na watchlist só quando atualiza a
página, exemplo AAPL"*. Era isso mesmo: :func:`carregaFaixaDoScan` tinha UMA
chamada em todo o front, dentro do ``init``. Depois do boot ninguém mais a
chamava, e ``_faixaScan`` ficava com o que existia quando a aba abriu.

O contraste que produzia o sintoma — e o agravante: a lista de observação se
repinta a cada 5s e o gráfico revalida sozinho no fechamento do candle (DA-118),
enquanto a agenda varre de hora em hora e GRAVA o ``last_scan.json`` (DA-114/116).
O servidor tinha dado novo a cada hora e a aba aberta nunca ia buscá-lo: quanto
mais tempo aberta, mais velha a faixa, e nada na tela dizia isso. Estado de horas
atrás com cara de agora, numa tela de operação.

Os dentes:

* com a página ABERTA e o scan salvo mudando no servidor, a faixa MUDA — o teste
  não recarrega nada, e é isso que ele mede;
* o gatilho é a PASSADA da agenda, perguntada ao servidor (``/api/agenda/scan``):
  trocar isto por um ``setInterval`` de N minutos em JavaScript derruba o teste,
  porque aí passariam a existir dois relógios;
* a releitura NÃO consome o "novo desde a última visita" — ela não passa pelo
  ``paintScan``, que é o bug que o comentário de ``_faixaScan`` documenta (a marca
  calculada e dada por vista antes de o painel existir);
* a releitura NÃO varre: é a mesma leitura de arquivo que o painel já fazia;
* aba em segundo plano: o navegador afrouxa o timer, então o retorno ao primeiro
  plano relê se a passada aconteceu enquanto ninguém olhava.
"""

import json
import re

import pytest

from tests.test_webui_frame_e_cor_e2e import DESKTOP, sobe_servidor

pytestmark = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None


@pytest.fixture
def base(tmp_path):
    yield from sobe_servidor(tmp_path)


def _linha(frame, estado, direction="compra"):
    return {"frame": frame, "estado": estado, "direction": direction,
            "price": 100.0, "trigger": 101.0, "dist_pct": 0.01,
            "pattern_state": "formando"}


def _scan(estado_1d, gerado_em):
    ativo = {"ticker": "AAA", "frames": [_linha("1d", estado_1d),
                                         _linha("4h", "formando"),
                                         _linha("1h", "em_movimento")]}
    ativo["melhor"] = ativo["frames"][0]
    return {"date": "2026-09-01", "frames": ["1d", "4h", "1h"],
            "gerado_em": gerado_em, "ativos": [ativo], "oportunidades": [],
            "resumo": {"em_gatilho": 1}}


# A passada das 11h e a das 12h, no MESMO ativo: o que era "na entrada" morreu.
_ANTES = _scan("em_gatilho", "2026-09-01T11:01:02-04:00")
_DEPOIS = _scan("invalidou", "2026-09-01T12:01:02-04:00")

_HIST = [{"run_id": "R-AAA", "ticker": "AAA", "date": "2026-09-01",
          "asset_type": "stock", "status": "done", "verdict": None,
          "elapsed": 1, "cost": {"usd": 0.0}, "finished_at": "2026-09-01 11:00"}]

# A classe do marcador do DIÁRIO — é ela que muda quando o dado muda.
_MARCA_D = """() => {
  const li = [...document.querySelectorAll('.history li')]
    .find(e => e.dataset.ticker === 'AAA');
  const f = li && li.querySelector('.h-faixa');
  if (!f) return null;
  const m = [...f.querySelectorAll('.fx-m')].find(x => x.textContent === 'D');
  return m ? m.className : null;
}"""


def _abre(page, base_url, estado, ler_em_segundos=1):
    """Sobe a tela com ``estado["scan"]`` servido — e MUTÁVEL entre requisições."""
    def handler(route):
        u = route.request.url
        if "/api/agenda/scan" in u:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"cadencia_min": 60, "atraso_s": 60,
                                           "margem_s": 0, "em_segundos": 1,
                                           "ler_em_segundos": ler_em_segundos}))
        elif "/api/scan/salvo" in u or "/api/scan" in u:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(estado["scan"]))
        elif "/api/history" in u:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"runs": _HIST}))
        elif "/api/watchlist" in u:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"tickers": [{"ticker": "AAA"}]}))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_selector(".history li", state="attached", timeout=15000)
    page.wait_for_selector(".h-faixa", state="attached", timeout=15000)


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_faixa_muda_com_o_scan_salvo_SEM_recarregar_a_pagina(base):
    """O DENTE central. Nenhum ``reload`` neste teste — é o ponto."""
    estado = {"scan": _ANTES}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, estado)
        assert "fx-agora" in page.evaluate(_MARCA_D), page.evaluate(_MARCA_D)

        # a agenda passou e gravou: o servidor tem dado novo, a aba continua aberta
        estado["scan"] = _DEPOIS
        page.wait_for_function(
            """() => {
              const li = [...document.querySelectorAll('.history li')]
                .find(e => e.dataset.ticker === 'AAA');
              const f = li && li.querySelector('.h-faixa');
              const m = f && [...f.querySelectorAll('.fx-m')]
                .find(x => x.textContent === 'D');
              return !!m && m.className.includes('fx-morreu');
            }""", timeout=15000)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_gatilho_e_a_PASSADA_perguntada_ao_SERVIDOR(base):
    """DENTE: trocar isto por um ``setInterval`` de N minutos em JavaScript faz o
    pedido a ``/api/agenda/scan`` sumir — e aí haveria dois relógios, o daqui e o
    de ``agenda.py``, sem ninguém saber qual manda."""
    estado = {"scan": _ANTES}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        page.add_init_script("""
          window.__agenda = [];
          window.__f0 = window.fetch;
          window.fetch = function (u, x) {
            if (String(u).indexOf('/api/agenda/scan') >= 0) window.__agenda.push(Date.now());
            return window.__f0(u, x);
          };
        """)
        _abre(page, base, estado)
        # a primeira leitura agenda a segunda, que agenda a terceira: o ciclo se
        # mantém sozinho — uma pergunta que não reagenda é uma faixa congelada
        # de novo, só que uma passada depois.
        page.wait_for_function("() => window.__agenda.length >= 2", timeout=15000)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_releitura_NAO_consome_o_novo_desde_a_ultima_visita(base):
    """DENTE do bug que o comentário de ``_faixaScan`` documenta.

    Na primeira versão o boot pintava o salvo pelo ``paintScan``, e isso CALCULAVA
    e dava por vista a marca de "novo" antes de o painel existir na tela — o sinal
    novo nunca chegava a aparecer como novo. A releitura periódica multiplicaria
    esse defeito por uma vez a cada passada.
    """
    estado = {"scan": _ANTES}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, estado)
        memoria = json.dumps({"AAA|compra|101": 1756742400000})
        page.evaluate("(m) => localStorage.setItem('td_sinais_vistos', m)", memoria)
        estado["scan"] = _DEPOIS
        page.wait_for_function(
            """() => {
              const f = document.querySelector('.h-faixa');
              const m = f && [...f.querySelectorAll('.fx-m')]
                .find(x => x.textContent === 'D');
              return !!m && m.className.includes('fx-morreu');
            }""", timeout=15000)
        depois = page.evaluate("() => ({"
                               "  memoria: localStorage.getItem('td_sinais_vistos'),"
                               "  scanData: _scanData, novos: _sinaisNovos.size })")
        assert depois["memoria"] == memoria, "a releitura mexeu na memória de visitas"
        assert depois["scanData"] is None, "a releitura passou pelo paintScan"
        assert depois["novos"] == 0, depois
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_releitura_NAO_varre(base):
    """É a MESMA leitura de arquivo que o painel já fazia: $0, sem LLM. Uma
    varredura por passada seria custo de verdade, e por aba aberta."""
    estado = {"scan": _ANTES}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        pedidos = []
        page.on("request", lambda r: pedidos.append(r.url))
        _abre(page, base, estado)
        page.wait_for_timeout(2500)   # espaço pra várias releituras acontecerem
        salvo = [u for u in pedidos if "/api/scan/salvo" in u]
        varreduras = [u for u in pedidos if re.search(r"/api/scan\?", u)]
        assert len(salvo) >= 2, ("a faixa não releu nada", salvo)
        assert not varreduras, ("a releitura disparou varredura", varreduras)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_voltar_pro_primeiro_plano_RELE_se_a_passada_aconteceu_atras_da_aba(base):
    """O navegador afrouxa (e no celular chega a parar) o ``setTimeout`` da aba de
    fundo. O timer é o caminho feliz; a volta da aba é a rede de segurança — a
    mesma disciplina do ``revalidaSeOCandleFechouEnquantoEuNaoOlhava``.

    Aqui o timer é longo de propósito (uma hora): se a faixa mudar, foi o retorno
    ao primeiro plano que a releu, e não o relógio.
    """
    estado = {"scan": _ANTES}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, estado, ler_em_segundos=3600)
        assert "fx-agora" in page.evaluate(_MARCA_D)

        estado["scan"] = _DEPOIS
        page.wait_for_timeout(1200)
        assert "fx-agora" in page.evaluate(_MARCA_D), "o timer longo já releu sozinho"

        # o navegador engoliu o timer: o alvo venceu enquanto a aba estava atrás
        page.evaluate("() => { _faixaAlvoMs = 1; }")
        page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
        page.wait_for_function(
            """() => {
              const f = document.querySelector('.h-faixa');
              const m = f && [...f.querySelectorAll('.fx-m')]
                .find(x => x.textContent === 'D');
              return !!m && m.className.includes('fx-morreu');
            }""", timeout=10000)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_agenda_fora_do_ar_RETENTA_a_pergunta_em_vez_de_encerrar_o_ciclo(base):
    """DENTE: o defeito não pode voltar pela porta do erro.

    Se a pergunta falha (rede oscilou, processo reiniciando) e o ciclo simplesmente
    termina, a faixa volta a congelar até o próximo F5 — que é exatamente o que esta
    entrega veio consertar, só que com um gatilho mais raro e mais difícil de ver. O
    que se repete é a PERGUNTA; quem decide quando ler continua sendo o servidor.
    """
    estado = {"scan": _ANTES}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        page.add_init_script("""
          window.__agenda = 0;
          window.__f0 = window.fetch;
          window.fetch = function (u, x) {
            if (String(u).indexOf('/api/agenda/scan') >= 0) window.__agenda++;
            return window.__f0(u, x);
          };
        """)

        def handler(route):
            u = route.request.url
            if "/api/agenda/scan" in u:
                route.fulfill(status=503, content_type="application/json", body="{}")
            elif "/api/scan/salvo" in u or "/api/scan" in u:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps(estado["scan"]))
            elif "/api/history" in u:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"runs": _HIST}))
            elif "/api/watchlist" in u:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"tickers": [{"ticker": "AAA"}]}))
            else:
                route.continue_()
        page.route(re.compile(r"/api/"), handler)
        page.goto(base, wait_until="domcontentloaded")
        page.wait_for_selector(".h-faixa", state="attached", timeout=15000)
        # o ciclo ficou ARMADO (um timer de re-pergunta), não morto
        armado = page.evaluate("() => !!_faixaTimer && _faixaAlvoMs === 0")
        assert armado, "a pergunta falhou e o ciclo morreu junto"
        assert page.evaluate("() => window.__agenda") >= 1
        browser.close()

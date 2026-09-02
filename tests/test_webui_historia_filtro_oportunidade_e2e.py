"""Filtro por OPORTUNIDADE na lista de observação (DA-156).

Pedido do Samyr: *"depois cria um botão para listar por oportunidade compra
venda em gatilho, algo assim"*. A lista já filtrava por CLASSE de ativo (Todos
· Ações · Cripto) — faltava o corte por oportunidade, e o dado já existe:
``sinais.oportunidades`` (a mesma conta que a faixa do card e o painel de
Sinais usam), já anexado em ``/api/scan/salvo``. O filtro é só apresentação —
zero chamada nova.

Estes testes travam:

1. o filtro usa a MESMA taxonomia do painel de Sinais (entrada/a_caminho/
   passou/conflito) — não um quarto vocabulário;
2. combina com a aba de classe (E, não OU): "Cripto + Na entrada" funciona;
3. estado vazio DECLARADO ("nenhum ativo em X agora"), nunca uma lista muda;
4. concorda com a DA-152: um ativo cuja ÚNICA leitura viva é invalidada não
   tem oportunidade nenhuma (a mesma fonte que já exclui `invalidou` da faixa).
"""

import json
import re

import pytest

from tests.test_webui_frame_e_cor_e2e import DESKTOP, TELEFONE, sobe_servidor

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


def _op(ticker, estado, metodo="123", direcao="compra", frame_lider="1d"):
    return {"ticker": ticker, "metodo": metodo, "metodo_rotulo":
            "Setup123" if metodo == "123" else "Storm123",
            "direcao": direcao if estado != "conflito" else None,
            "estado": estado, "frames": [frame_lider], "confluencia": 1,
            "frame_lider": frame_lider, "gatilho": 101.0, "sl": 95.0, "tp": 110.0,
            "preco": 100.0, "rr_gatilho": 1.5, "janela": None, "aviso": None,
            "entrada": None, "ordem": None, "chave": f"{ticker}|{metodo}|{estado}",
            "dissidentes": [], "total_frames": 3, "outro_metodo": None}


# AAA (ação): oportunidade NA ENTRADA. BBB (cripto): CONFLITO. CCC (ação):
# A CAMINHO. DDD (ação): sem oportunidade nenhuma (só leitura invalidada — tem
# de CONCORDAR com a faixa/DA-152 e não aparecer em filtro nenhum).
_ATIVOS = [
    {"ticker": "AAA", "frames": [_linha("1d", "em_gatilho", "compra"),
                                 _linha("4h", "formando", "compra")]},
    {"ticker": "BBB", "frames": [_linha("1d", "em_gatilho", "venda"),
                                 _linha("4h", "formando", "compra")]},
    {"ticker": "CCC", "frames": [_linha("1d", "formando", "compra")]},
    {"ticker": "DDD", "frames": [_linha("1d", "invalidou", "compra")]},
]
for _a in _ATIVOS:
    _a["melhor"] = _a["frames"][0]

_SCAN = {"date": "2026-08-31", "frames": ["1d", "4h", "1h"],
         "gerado_em": "2026-08-31T22:00:00-04:00",
         "ativos": _ATIVOS, "oportunidades": [
             _op("AAA", "entrada"),
             _op("BBB", "conflito", metodo="storm"),
             _op("CCC", "a_caminho"),
         ],
         "resumo": {}}

_HIST = [
    {"run_id": "R-AAA", "ticker": "AAA", "date": "2026-08-31", "asset_type": "stock",
     "status": "done", "verdict": None, "elapsed": 1, "cost": {"usd": 0.0},
     "finished_at": "2026-08-31 20:00"},
    {"run_id": "R-BBB", "ticker": "BBB", "date": "2026-08-31", "asset_type": "crypto",
     "status": "done", "verdict": None, "elapsed": 1, "cost": {"usd": 0.0},
     "finished_at": "2026-08-31 20:00"},
    {"run_id": "R-CCC", "ticker": "CCC", "date": "2026-08-31", "asset_type": "stock",
     "status": "done", "verdict": None, "elapsed": 1, "cost": {"usd": 0.0},
     "finished_at": "2026-08-31 20:00"},
    {"run_id": "R-DDD", "ticker": "DDD", "date": "2026-08-31", "asset_type": "stock",
     "status": "done", "verdict": None, "elapsed": 1, "cost": {"usd": 0.0},
     "finished_at": "2026-08-31 20:00"},
]


def _abre(page, base_url, scan=None, viewport=None):
    def handler(route):
        u = route.request.url
        if "/api/scan/salvo" in u or "/api/scan" in u:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(scan if scan is not None else _SCAN))
        elif "/api/history" in u:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"runs": _HIST}))
        elif "/api/watchlist" in u:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"tickers": [
                              {"ticker": a["ticker"]} for a in _ATIVOS]}))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_selector(".history li", state="attached", timeout=15000)
    page.wait_for_selector("#historyOpFilter:not(.hidden)", state="attached", timeout=15000)


def _tickers_na_lista(page):
    return page.evaluate("""() => [...document.querySelectorAll('.history li')]
      .map((li) => li.dataset.ticker).filter(Boolean)""")


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_chips_usam_a_MESMA_taxonomia_do_painel_de_sinais(base):
    """DENTE: nada de vocabulário novo — os rótulos dos chips são os mesmos de
    SINAL_SECOES (o painel de Sinais, DA-117), lidos DAQUELE array, não escritos
    de novo aqui."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        rotulos = page.evaluate("""() => {
          const chips = [...document.querySelectorAll('#historyOpFilter [data-op-filter]')]
            .filter((b) => b.dataset.opFilter)
            .map((b) => b.textContent.trim());
          const oficiais = SINAL_SECOES.map((s) => s.titulo);
          return {chips, oficiais};
        }""")
        # cada chip renderizado tem de casar com algum título oficial (prefixo,
        # já que o número vem colado no fim: "Na entrada 1")
        for chip_txt in rotulos["chips"]:
            assert any(chip_txt.startswith(t) for t in rotulos["oficiais"]), \
                (chip_txt, rotulos["oficiais"])
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_clicar_no_chip_filtra_a_lista(base):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        assert set(_tickers_na_lista(page)) == {"AAA", "BBB", "CCC", "DDD"}
        page.click('#historyOpFilter [data-op-filter="entrada"]')
        assert _tickers_na_lista(page) == ["AAA"]
        # clicar em "Todos" desliga
        page.click('#historyOpFilter [data-op-filter=""]')
        assert set(_tickers_na_lista(page)) == {"AAA", "BBB", "CCC", "DDD"}
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_combina_com_a_aba_de_classe_E_nao_OU(base):
    """"Cripto + Na entrada" tem de aplicar as DUAS condições — não uma OU
    outra. BBB é cripto mas está em CONFLITO, não em entrada: o cruzamento
    dá vazio, não BBB."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        page.click('#historyTabs [data-filter="crypto"]')
        assert _tickers_na_lista(page) == ["BBB"]
        page.click('#historyOpFilter [data-op-filter="entrada"]')
        assert _tickers_na_lista(page) == [], "cruzamento errado: BBB não está em entrada"
        txt = page.inner_text("#history")
        assert "nenhum ativo" in txt.lower() and "entrada" in txt.lower(), txt
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_conflito_e_a_caminho_tambem_filtram(base):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        page.click('#historyOpFilter [data-op-filter="conflito"]')
        assert _tickers_na_lista(page) == ["BBB"]
        page.click('#historyOpFilter [data-op-filter=""]')
        page.click('#historyOpFilter [data-op-filter="a_caminho"]')
        assert _tickers_na_lista(page) == ["CCC"]
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_invalidado_NAO_e_oportunidade_concorda_com_a_faixa_DA152(base):
    """DDD só tem leitura `invalidou` — não pode aparecer em NENHUM chip de
    oportunidade, a mesma exclusão que a DA-152 já aplica na faixa do card."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        chips_estados = page.evaluate("""() => [...document.querySelectorAll(
          '#historyOpFilter [data-op-filter]')].map((b) => b.dataset.opFilter).filter(Boolean)""")
        for estado in chips_estados:
            page.click(f'#historyOpFilter [data-op-filter="{estado}"]')
            assert "DDD" not in _tickers_na_lista(page), (estado, _tickers_na_lista(page))
            page.click('#historyOpFilter [data-op-filter=""]')
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_criterio_de_agregacao_declarado_no_title(base):
    """"O critério de agregação por frame/método declarado na tela ou no
    title" — o chip não pode prometer "AAPL na entrada" sem dizer que é
    QUALQUER frame/método, não todos."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        titulo = page.evaluate(
            """() => document.querySelector('#historyOpFilter [data-op-filter="entrada"]').title""")
        assert titulo, "chip sem title — critério não declarado"
        assert "qualquer" in titulo.lower(), titulo
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_sem_oportunidade_nenhuma_o_filtro_fica_escondido(base):
    """Sem scan salvo com nada pra contar, o filtro não promete o que não tem
    — some, em vez de mostrar chips com contagem 0."""
    vazio = {**_SCAN, "oportunidades": []}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        page.route(re.compile(r"/api/"), lambda route: (
            route.fulfill(status=200, content_type="application/json", body=json.dumps(vazio))
            if "/api/scan/salvo" in route.request.url or "/api/scan" in route.request.url
            else (route.fulfill(status=200, content_type="application/json",
                                body=json.dumps({"runs": _HIST}))
                  if "/api/history" in route.request.url
                  else route.fulfill(status=200, content_type="application/json",
                                     body=json.dumps({"tickers": []}))
                  if "/api/watchlist" in route.request.url else route.continue_())))
        page.goto(base, wait_until="domcontentloaded")
        page.wait_for_selector(".history li", state="attached", timeout=15000)
        page.wait_for_timeout(500)
        assert page.eval_on_selector(
            "#historyOpFilter", "el => el.classList.contains('hidden')") is True
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [DESKTOP, TELEFONE], ids=["desktop", "telefone"])
def test_zero_emoji_no_filtro(base, viewport):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre(page, base, viewport=viewport)
        txt = page.inner_text("#historyOpFilter")
        assert not re.search(r"[\U0001F300-\U0001FAFF☀-➿]", txt), txt
        browser.close()

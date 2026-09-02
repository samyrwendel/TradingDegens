"""PnL de paper no Track record (DA-154).

Proposta do Samyr: *"pensei em fazer o cálculo como se em cada operação abrisse
com 100 dólares... como se fosse paper"*. O painel de Track record (`#scanTrack`,
`/api/scan/verdicts`) já media taxa de acerto e expectativa em R — abstrato, não
soma. Aqui ele ganha o PnL em dólares por posição FIXA (não risco fixo — a
perda por trade varia com a distância do stop, e isso tem de estar dito na
tela), separado por Setup123/Storm123, com a curva de equity e o gate de N que
o índice de confiabilidade já usa.
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


def _bloco(n, total=None, pct=None, medio=None, melhor=None, pior=None, curva=None):
    nivel = "insuficiente" if n < 5 else ("preliminar" if n < 20 else "operavel")
    return {"n": n, "nivel": nivel, "banca_por_trade": 100.0,
            "pnl_total_usd": total, "pnl_total_pct": pct, "pnl_medio_usd": medio,
            "melhor_trade": melhor, "pior_trade": pior, "curva_equity": curva or []}


_CURVA = [{"ts": "2026-08-20", "ticker": "AAA", "pnl_usd": -5.0, "equity_usd": -5.0},
          {"ts": "2026-08-25", "ticker": "BBB", "pnl_usd": 15.0, "equity_usd": 10.0}]

_VERDICTS = {
    "verdicts": [], "n_fechados": 8, "taxa_acerto": 0.5,
    "rr_medio": 1.2, "expectativa_r": 0.1, "acerto_equilibrio": 0.45,
    "n_com_rr": 8, "acerto_com_rr": 0.5,
    "por_setup": {"123": {"n": 8, "n_fechados": 8, "taxa_acerto": 0.5},
                  "storm": {"n": 0, "n_fechados": 0, "taxa_acerto": None}},
    "paper": {
        "banca_por_trade": 100.0,
        "premissa": ("paper: posição FIXA em dólares por operação (não risco fixo — "
                     "a perda varia com a distância do stop) · sem custos · sem slippage"),
        "agregado": _bloco(8, total=10.0, pct=1.25, medio=1.25,
                           melhor={"ticker": "BBB", "pnl_usd": 15.0, "pnl_pct": 15.0},
                           pior={"ticker": "AAA", "pnl_usd": -5.0, "pnl_pct": -5.0},
                           curva=_CURVA),
        "por_setup": {
            "123": _bloco(8, total=10.0, pct=1.25, medio=1.25,
                         melhor={"ticker": "BBB", "pnl_usd": 15.0, "pnl_pct": 15.0},
                         pior={"ticker": "AAA", "pnl_usd": -5.0, "pnl_pct": -5.0},
                         curva=_CURVA),
            "storm": _bloco(2),   # abaixo do gate de N — sem PnL, só a ressalva
        },
        "por_frame": {"1d": _bloco(8, total=10.0, pct=1.25, medio=1.25, curva=_CURVA)},
    },
}


def _abre(page, base_url, verdicts=None, capturados=None):
    payload = verdicts if verdicts is not None else _VERDICTS

    def handler(route):
        u = route.request.url
        if "/api/scan/verdicts" in u:
            if capturados is not None:
                capturados.append(u)
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(payload))
        else:
            route.continue_()

    page.route(re.compile(r"/api/scan/verdicts"), handler)
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_function("() => typeof showScanTrack === 'function'")
    page.evaluate("() => { document.getElementById('scanPanel').classList.remove('hidden'); }")
    page.evaluate("async () => { await showScanTrack(); }")
    page.wait_for_selector("#scanTrackBanca", state="attached", timeout=10000)


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_premissa_declarada_e_banca_visivel(base):
    """A regra que decide se o número presta: posição FIXA (não risco fixo), sem
    custos, sem slippage — tudo em palavras na tela, não implícito."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        txt = page.inner_text("#scanTrack")
        assert "posição FIXA" in txt or "posição fixa" in txt.lower(), txt
        assert "não risco fixo" in txt.lower() or "não é risco fixo" in txt.lower(), txt
        assert "sem custos" in txt.lower(), txt
        assert "sem slippage" in txt.lower(), txt
        banca = page.eval_on_selector("#scanTrackBanca", "el => el.value")
        assert banca == "100", banca
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_por_setup_com_numeros_e_gate_de_amostra(base):
    """Setup123 (n=8, acima do mínimo) mostra PnL; Storm123 (n=2, abaixo do
    gate de N=5) declara amostra insuficiente — NUNCA um número que o
    intervalo desmentiria."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        txt = page.inner_text("#scanTrack")
        assert "Setup123" in txt and "Storm123" in txt, txt
        assert "$10" in txt.replace(",", ".") or "10,00" in txt, txt   # total do 123
        # Storm123 (n=2) não pode mostrar total nenhum — só a ressalva. A linha
        # dele é uma só (cada bloco é um <div>); pega ela, não o resto da tela.
        linha_storm = next(li for li in txt.splitlines() if "Storm123" in li)
        assert "insuficiente" in linha_storm.lower(), linha_storm
        assert "$" not in linha_storm, linha_storm
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_curva_de_equity_e_um_desenho(base):
    """"É o gráfico que responde 'isso dá dinheiro?' de relance" — tem de haver
    um <svg><path> desenhado, não só números."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        n = page.eval_on_selector_all("#scanTrack svg path", "els => els.length")
        assert n >= 1, "nenhuma curva de equity desenhada"
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_melhor_e_pior_trade_aparecem(base):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        txt = page.inner_text("#scanTrack")
        assert "BBB" in txt and "AAA" in txt, txt   # melhor e pior, por ticker
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_trocar_a_banca_reconsulta_com_o_novo_valor(base):
    """A banca é CONFIGURÁVEL (100 é só o padrão) — mudar o campo tem de refazer
    a consulta com o parâmetro novo, não só recalcular no cliente com o dado
    velho (o backend é quem sabe reconstruir a curva pra outra banca)."""
    capturados = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, capturados=capturados)
        assert len(capturados) == 1 and "banca=" not in capturados[0], capturados
        page.fill("#scanTrackBanca", "250")
        page.keyboard.press("Tab")   # blur real: dispara o 'change' nativo, uma vez
        for _ in range(50):
            if len(capturados) >= 2:
                break
            page.wait_for_timeout(50)
        assert len(capturados) == 2, capturados
        assert "banca=250" in capturados[1], capturados[1]
        browser.close()

"""PnL de paper no Track record (DA-154) + a carteira virtual (DA-155).

Proposta do Samyr: *"pensei em fazer o cálculo como se em cada operação abrisse
com 100 dólares... como se fosse paper"*. O painel de Track record (`#scanTrack`,
`/api/scan/verdicts`) já media taxa de acerto e expectativa em R — abstrato, não
soma. Aqui ele ganha o PnL em dólares por posição FIXA (não risco fixo — a
perda por trade varia com a distância do stop, e isso tem de estar dito na
tela), separado por Setup123/Storm123, com a curva de equity e o gate de N que
o índice de confiabilidade já usa.

DA-155 nomeou o que estava sendo construído — PAPER TRADING, não "estatística
do scan" — e cresceu o escopo: a CARTEIRA VIRTUAL acompanha a simulação
enquanto ela VIVE (posições abertas com PnL não realizado, saldo evoluindo),
com o vocabulário e o aviso ("nenhuma ordem real") que o nome exige, e sem
NUNCA se confundir com a carteira REAL do Erick (outro painel, `#erickPanel`).
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
        "carteira": {
            "marco": "2026-08-15T00:00:00+00:00", "banca_por_trade": 100.0,
            "nivel": "preliminar", "n_fechadas": 8, "n_abertas": 2,
            "realizado_usd": 10.0, "nao_realizado_usd": 6.5, "saldo_usd": 16.5,
            "abertas": [
                {"ticker": "AAPL", "setup": "123", "frame": "1d", "direction": "compra",
                 "veredito": "andamento_lucro", "entrada": 220.0, "preco_agora": 228.6,
                 "pnl_pct": 3.91, "pnl_usd": 3.91},
                {"ticker": "SOL-USD", "setup": "storm", "frame": "4h", "direction": "venda",
                 "veredito": "andamento_prejuizo", "entrada": 140.0, "preco_agora": 142.6,
                 "pnl_pct": -1.86, "pnl_usd": -1.86},
            ],
            "curva_equity": _CURVA,
        },
    },
}


def _abre(page, base_url, verdicts=None, capturados=None, owner=False):
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
    if owner:
        # simula a sessão de dono só pro FRONT decidir mostrar o botão — o
        # gate de verdade é o servidor (403 sem cookie real, testado à parte
        # em test_webui_server.py::test_paper_reset_publico_e_403...).
        page.evaluate("() => { _isOwner = true; }")
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
        # dele é uma só (cada bloco é um <div>); "Storm123:" (com dois-pontos) a
        # distingue da linha de POSIÇÃO ABERTA (que também cita "Storm123", como
        # o método da posição, sem dois-pontos depois do nome).
        linha_storm = next(li for li in txt.splitlines() if "Storm123:" in li)
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


# ══════════ A CARTEIRA VIRTUAL (DA-155) ═══════════════════════════════════════

@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_nomenclatura_de_paper_trading_e_o_aviso_de_ordem_simulada(base):
    """O nome importa (o Samyr nomeou): PAPER TRADING / simulação, com o aviso
    de que nenhuma ordem real é enviada — sem isso um leitor apressado pode
    achar que é dinheiro de verdade."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        txt = page.inner_text("#scanTrack").lower()
        assert "paper trading" in txt or "paper" in txt, txt
        assert "simula" in txt, txt
        assert "nenhuma ordem real" in txt, txt
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_nunca_confunde_com_a_carteira_do_erick(base):
    """As duas convivem na TELA (painéis diferentes), mas NUNCA no mesmo
    painel nem somando saldo — a carteira virtual não pode citar "Erick" como
    se fosse a fonte do saldo, só como esclarecimento de que são coisas
    diferentes, e o texto do Erick tem de morar no painel DELE."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        assert page.query_selector("#erickPanel") is not None, "painel do Erick some do DOM"
        # os dois painéis são elementos DIFERENTES — nunca o mesmo container
        same = page.evaluate(
            "() => document.getElementById('erickPanel') === document.getElementById('scanTrack')")
        assert same is False
        # #erickPanel não tem NADA da carteira virtual dentro dele
        erick_txt = page.inner_text("#erickPanel")
        assert "saldo simulado" not in erick_txt.lower()
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_saldo_e_posicoes_abertas_com_pnl_nao_realizado(base):
    """Posições ABERTAS (reusando andamento_lucro/andamento_prejuizo) aparecem
    com PnL NÃO REALIZADO, e o saldo soma realizado + não realizado."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        txt = page.inner_text("#scanTrack")
        assert "saldo simulado" in txt.lower()
        assert "16,5" in txt.replace(".", ",") or "16,50" in txt, txt
        assert "AAPL" in txt and "SOL-USD" in txt   # as duas posições abertas
        assert "2 posiç" in txt, txt   # "2 posição(ões) aberta(s)"
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_botao_de_reset_so_aparece_pro_DONO(base):
    """Público não vê o botão de reiniciar (o servidor também barra com 403 —
    ver test_webui_server.py — mas escondê-lo pro público evita prometer uma
    ação que não vai funcionar)."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, owner=False)
        assert page.query_selector("#scanPaperResetBtn") is None
        browser.close()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, owner=True)
        assert page.query_selector("#scanPaperResetBtn") is not None
        browser.close()


# ══════════ A GRADE MÉTODO × FRAME (task 20260903-013) ════════════════════════

_METODO_FRAME = dict(_VERDICTS)
_METODO_FRAME["paper"] = dict(_VERDICTS["paper"])
_METODO_FRAME["paper"]["metodo_frame"] = {
    "marco": "2026-09-03T00:00:00+00:00",
    "banca_por_trade": 100.0,
    # DESDE o marco: o que conta pro gate — Setup123 1d com acerto ALTO e E[R] NEGATIVO
    "desde_marco": {
        "123": {"1d": {"n": 6, "nivel": "preliminar", "taxa_acerto": 0.75,
                       "expectativa_r": -0.07, "acerto_equilibrio": 0.83, "rr_medio": 0.2,
                       "n_com_rr": 6, "banca_por_trade": 100.0, "pnl_fixo_usd": 13.57,
                       "pnl_medio_usd": 2.26, "pnl_risco_fixo_usd": -183.0, "curva_equity": []}},
    },
    # ANTES da régua: o histórico do BTC storm 1d, DEDUPLICADO a 1
    "antes_da_regua": {
        "storm": {"1d": {"n": 1, "nivel": "insuficiente", "taxa_acerto": 0.0,
                         "expectativa_r": -1.0, "acerto_equilibrio": 0.33, "rr_medio": 2.0,
                         "n_com_rr": 1, "banca_por_trade": 100.0, "pnl_fixo_usd": None,
                         "pnl_medio_usd": None, "pnl_risco_fixo_usd": None, "curva_equity": []}},
    },
}


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_metodo_frame_mostra_acerto_er_e_as_duas_leituras_de_pnl(base):
    """A grade método×frame: acerto ALTO com E[R] NEGATIVO lado a lado, e as duas
    leituras de PnL (posição fixa E risco fixo) — pra "acerto alto = ganhou" não colar."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, verdicts=_METODO_FRAME)
        txt = page.inner_text("#scanTrack")
        assert "método" in txt.lower() and "frame" in txt.lower(), txt
        assert "75%" in txt, txt                       # acerto alto
        assert "E[R]" in txt and "-0" in txt, txt      # E[R] negativo ao lado
        assert "risco fixo" in txt.lower() and "posição fixa" in txt.lower(), txt
        assert "-183" in txt.replace(",", "."), txt    # a leitura risco-fixo do 123
        # o histórico anterior fica VISÍVEL, rotulado "antes da régua"
        assert "antes da régua" in txt.lower(), txt
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_reset_pede_confirmacao_e_chama_o_endpoint_certo(base):
    """O clique PEDE confirmação (é uma ação que zera o saldo simulado que a
    tela mostra) e, confirmado, chama POST /api/scan/paper/reset — nunca
    mexe no ledger direto do cliente."""
    chamadas = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        page.on("dialog", lambda d: d.accept())

        def reset_handler(route):
            chamadas.append(route.request.method)
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True, "marco": "2026-09-01T00:00:00+00:00"}))

        page.route(re.compile(r"/api/scan/paper/reset"), reset_handler)
        _abre(page, base, owner=True)
        page.click("#scanPaperResetBtn")
        page.wait_for_timeout(300)
        assert chamadas == ["POST"], chamadas
        browser.close()

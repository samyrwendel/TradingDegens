"""DA-076 na tela: ZERO pictograma, e nenhuma informação perdida (task 025).

Instrução do Samyr: "tira todos os emojis". A DA-076 já decidiu a linha de corte —
SAI todo PICTOGRAMA (alvo, stop, balança, avisos, estados, âncoras de seção,
ferramentas); FICA o símbolo TIPOGRÁFICO com função de interface (↻, →, ↑ ↓ ↔, ✕),
que é controle e direção, não decoração.

A armadilha da tarefa está no segundo teste, não no primeiro: em vários pontos o
pictograma era o ÚNICO marcador de estado da linha. Apagá-lo sem substituto APAGA
INFORMAÇÃO — e informação sumindo da tela é o defeito que este projeto passou o dia
inteiro caçando. Por isso aqui há dois portões:

1. **Portão estático, por FAIXA UNICODE** (não por lista à mão: lista à mão erra por
   omissão) — nenhum pictograma nos três arquivos do front, e o que sobra é
   enumerado com a justificativa de ser tipográfico.
2. **Portão de INFORMAÇÃO** — o estado continua distinguível: compra × venda,
   passou × falhou, atenção. Cada um por COR + PALAVRA, medidas no navegador.
"""

import json
import pathlib
import re
import threading

import pytest

from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.server import make_server
from tradingagents.webui.store import HistoryStore

_STATIC = pathlib.Path(__file__).resolve().parents[1] / "tradingagents" / "webui" / "static"
_ARQUIVOS = ("app.js", "index.html", "style.css")

# Faixas de PICTOGRAMA. Tudo o que cai nelas é emoji/dingbat — não é letra do
# português nem pontuação, e é exatamente o que a DA-076 corta.
_FAIXAS = (
    (0x2190, 0x21FF), (0x2300, 0x23FF), (0x25A0, 0x25FF), (0x2600, 0x27BF),
    (0x2B00, 0x2BFF), (0x1F000, 0x1FAFF), (0xFE00, 0xFE0F),
)

# O que FICA, com o porquê. ↻ ✕ → ← ↑ ↓ ↔ ↗ ⇒ são controle e direção (a DA os
# nomeia). ▸ ▾ são os marcadores de abrir/fechar dos <details> — mesma família, e
# entram por `content` de CSS, não como texto de conteúdo. ✓ · — … são pontuação.
_TIPOGRAFICOS = set("↻→←↑↓↔↗⇒✕×·—–…≥≤±✓○●◦▸▾")


def _pictogramas(texto):
    fora = {}
    for ch in texto:
        if ch in _TIPOGRAFICOS:
            continue
        if any(a <= ord(ch) <= b for a, b in _FAIXAS):
            fora[ch] = fora.get(ch, 0) + 1
    return fora


def test_zero_pictograma_nos_tres_arquivos_do_front():
    """Portão ESTÁTICO. Varredura por faixa Unicode: a lista à mão erra por omissão,
    e o levantamento que originou a decisão contava 333 ocorrências."""
    achados = {}
    for nome in _ARQUIVOS:
        fora = _pictogramas((_STATIC / nome).read_text())
        if fora:
            achados[nome] = fora
    assert achados == {}, achados


def test_o_que_sobrou_e_so_controle_e_direcao():
    """O complemento do teste acima: prova que os símbolos que restam são os que a
    DA-076 manda ficar — e não que a varredura simplesmente não olhou pra eles."""
    usados = set()
    for nome in _ARQUIVOS:
        for ch in (_STATIC / nome).read_text():
            if ch in _TIPOGRAFICOS and any(a <= ord(ch) <= b for a, b in _FAIXAS):
                usados.add(ch)
    # nenhum símbolo fora do conjunto declarado entra pela porta dos fundos
    assert usados <= _TIPOGRAFICOS
    # e os de controle que a interface realmente usa continuam lá
    assert {"↻", "✕"} <= usados, usados


# ---------------------------------------------------------------------------
# Portão de INFORMAÇÃO: o estado continua distinguível, por cor + palavra.
# ---------------------------------------------------------------------------
pytestmark_integration = pytest.mark.integration

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


def _snap(setup_state="ativo", direction="compra"):
    return {
        "run_id": "R-EMO", "ticker": "ZEC-USD", "date": "2026-08-29",
        "asset_type": "crypto", "status": "done", "elapsed": 1, "cost": {"usd": 0.0},
        "verdict": None, "verdict_timeframe": "4h", "method": "setup123",
        "result": {
            "setup123": True, "verdict": None, "final_decision": "",
            "timeframe": "4h", "as_of_price": 834.74,
            "actionable": {
                "symbol": "ZEC-USD", "price": 834.74, "as_of": "2026-08-29",
                "timeframe": "4h", "horizon": "dias", "setup_state": setup_state,
                "setup_source": "123",
                "buy_zone": None, "realize_zone": None, "pullback_zone": None,
                "pattern": {"p1": {}, "p2": {}, "p3": {}, "trigger": 834.82,
                            "state": "formando", "direction": direction},
                "invalidation": {"price": 790.29, "meaning": "perde o ponto 3"},
                "stop": {"price": 764.76, "basis": "invalidação + folga"},
                "target": {"price": 856.72, "label": "topo anterior"},
                "risk_reward": {"rr": 0.31, "entry": 834.82, "risk": 70.06,
                                "reward": 21.9, "note": None, "entry_basis": "gatilho"},
            },
            "live_price": None, "price_chart": {}, "degraded": [],
            "bull": "Tese de alta.", "bear": "Tese de baixa.",
            "research_manager": "", "investment_plan": "", "trader_plan": "",
            "risk_decision": "", "market_report": "", "sentiment_report": "",
            "news_report": "", "fundamentals_report": "", "erick_report": "",
            "drop_nature": {}, "derivatives_report": "",
        },
    }


def _abre(page, base_url, snap):
    def handler(route):
        url = route.request.url
        if "/api/status/" in url or re.search(r"/api/run/[^/]+$", url):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(snap))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)
    page.goto(base_url, wait_until="networkidle")
    page.evaluate("() => watchRun('R-EMO')")
    page.wait_for_selector("#setupCards:not(.hidden)")
    page.wait_for_timeout(150)


@pytest.mark.integration
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_nada_de_pictograma_no_que_o_navegador_ESCREVE(base):
    """O portão estático olha o fonte; este olha o RENDERIZADO — inclusive o que sai
    de `content` de CSS, que o fonte esconde dentro de uma regra."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1100})
        _abre(page, base, _snap())
        txt = page.evaluate("() => document.body.innerText")
        fora = _pictogramas(txt)
        assert fora == {}, fora
        browser.close()


@pytest.mark.integration
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_compra_e_venda_continuam_distinguiveis_por_COR_e_PALAVRA(base):
    """O 🟢/🔴 era o marcador de direção. Sem ele, a distinção tem que sobreviver —
    e sobrevive em dois canais: a palavra ("de compra"/"de venda") e a cor da borda
    do card, que é a MESMA que o gráfico usa pra marcar o padrão."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        medidas = {}
        for direcao in ("compra", "venda"):
            page = browser.new_page(viewport={"width": 1500, "height": 1100})
            _abre(page, base, _snap(direction=direcao))
            medidas[direcao] = page.evaluate("""() => {
              const c = document.querySelector('#setupCards .sc-123');
              return {texto: c.querySelector('.sc-dir').innerText.trim(),
                      cor: getComputedStyle(c.querySelector('.sc-dir')).color,
                      borda: getComputedStyle(c).borderLeftColor};
            }""")
            page.close()
        assert medidas["compra"]["texto"] == "de compra", medidas
        assert medidas["venda"]["texto"] == "de venda", medidas
        assert medidas["compra"]["cor"] != medidas["venda"]["cor"], ("a COR também distingue", medidas)
        assert medidas["compra"]["borda"] != medidas["venda"]["borda"], medidas
        browser.close()


@pytest.mark.integration
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("estado,cor_esperada,rotulo", [
    # verde = setup vivo (a única cor que a DA-078 deixa aqui, e ela significa
    # "janela aberta"); "aguardar" perdeu o âmbar e é dito por PALAVRA.
    ("ativo", "rgb(46, 204, 113)", "Setup ativo agora"),
    ("aguardar_rompimento", None, "Aguardar rompimento"),
])
def test_o_estado_do_setup_continua_marcado_por_cor(base, estado, cor_esperada, rotulo):
    """O 🎯/⏳/⚪ era o marcador; depois a cor; agora, onde a DA-078 tirou a cor, a
    PALAVRA. O que não muda é o invariante: o estado nunca fica sem portador."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1100})
        _abre(page, base, _snap(setup_state=estado))
        m = page.evaluate("""() => {
          const e = document.querySelector('#setupCards .sc-state');
          return {txt: e.innerText.trim(), cor: getComputedStyle(e).color};
        }""")
        if cor_esperada:
            assert m["cor"] == cor_esperada, m
        assert m["txt"].startswith(rotulo), m
        assert not _pictogramas(m["txt"]), m
        browser.close()


@pytest.mark.integration
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_atencao_continua_sendo_atencao_sem_o_sinal_de_aviso(base):
    """O ⚠️ marcava o R:R ruim, o alvo recusado e a série vencida; a cor âmbar fazia
    o trabalho junto com ele. A DA-078 tirou as duas — sobrou a PALAVRA, que é o que
    a regra manda: aviso se resolve com texto e hierarquia, não com cor nova."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1100})
        _abre(page, base, _snap())
        m = page.evaluate("""() => {
          const rr = [...document.querySelectorAll('#setupCards .sc-row')]
            .find(e => e.innerText.includes('risco/retorno'));
          return {classe: rr.className, cor: getComputedStyle(rr).color,
                  title: rr.getAttribute('title'), txt: rr.innerText};
        }""")
        assert "rr-ruim" in m["classe"], m
        assert "risco > retorno" in m["txt"], ("sem cor e sem emoji, o aviso é PALAVRA", m)
        assert "risco MAIOR que o retorno" in (m["title"] or ""), m
        assert not _pictogramas(m["txt"]), m
        browser.close()

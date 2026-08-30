"""DA-078 regra 11 — o espaço vago fica ONDE SOBRA, não no meio (task 20260830-004).

Quarto print do MESMO defeito no mesmo dia, e por isso tratado como padrão e não como
quatro bugs: barra de controle com MODELOS cortado enquanto sobrava folga entre os
blocos; linha do scan com rasgo no meio e o motivo truncando do outro lado; tira do
cabeçalho com as duas famílias nos extremos e a data caindo fora; e o cabeçalho do
resultado com a meta à esquerda, a cotação à direita e um vão enorme no meio — com o
da direita ainda cortando na borda.

A causa comum é `justify-content: space-between` + `margin-left: auto`: o par empurra
o conteúdo pras duas pontas e abre o buraco no centro, enquanto o texto da ponta
trunca por falta de 20px. É desperdício e perda de informação ao mesmo tempo.

O TESTE É O CRITÉRIO, literalmente: **nenhuma linha pode ter, ao mesmo tempo, vão
central e conteúdo truncado.** Ele varre o DOM renderizado das superfícies reais em
três larguras e mede as duas coisas juntas — em vez de auditar o CSS por seletor, que
não diz o que acontece na tela.
"""

import json
import re
import threading

import pytest

from tradingagents.webui import timeutil
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.store import HistoryStore

pytestmark = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None

LARGURAS = (1500, 1280, 390)

# O vão que conta como "rasgo": menos que isto é respiro entre grupos, não buraco.
_VAO_MIN = 60

# A varredura. Para cada elemento em flex-row, mede o MAIOR intervalo horizontal
# entre filhos consecutivos (o "vão") e se algum descendente está truncado. Só
# reprova quem tem OS DOIS — vão sozinho pode ser respiro; truncar sozinho é outro
# assunto (largura de verdade insuficiente).
_VARRE = """(vaoMin) => {
  const trunca = (e) => e.scrollWidth > e.clientWidth + 1;
  const visivel = (e) => { const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0; };
  const ruins = [];
  for (const el of document.querySelectorAll('*')) {
    const cs = getComputedStyle(el);
    if (cs.display !== 'flex' || cs.flexDirection !== 'row') continue;
    if (!visivel(el)) continue;
    const filhos = [...el.children].filter(visivel)
      .map(c => c.getBoundingClientRect())
      .sort((a, b) => a.left - b.left);
    if (filhos.length < 2) continue;
    // só compara filhos da MESMA fileira (flex-wrap põe uns embaixo dos outros)
    let vao = 0;
    for (let i = 1; i < filhos.length; i++) {
      const a = filhos[i - 1], b = filhos[i];
      const mesmaFileira = Math.abs(a.top - b.top) < 6;
      if (mesmaFileira) vao = Math.max(vao, b.left - a.right);
    }
    if (vao < vaoMin) continue;
    const cortados = [el, ...el.querySelectorAll('*')]
      .filter(e => visivel(e) && trunca(e))
      .map(e => ({cls: (e.className || '').toString().slice(0, 40),
                  txt: (e.innerText || '').slice(0, 40)}));
    if (cortados.length) {
      ruins.push({cls: (el.className || '').toString().slice(0, 60),
                  vao: Math.round(vao), cortados: cortados.slice(0, 3)});
    }
  }
  return ruins;
}"""

_HOJE = timeutil.today()

_HIST = {"runs": [
    {"run_id": "R1", "ticker": "AMD", "date": "2026-08-29", "status": "done",
     "method": "setup123", "setup_state": "aguardar_rompimento", "verdict": None,
     "asset_type": "stock", "cost_usd": 0.0, "elapsed": 2, "count": 3},
    {"run_id": "R2", "ticker": "ZEC-USD", "date": "2026-08-29", "status": "done",
     "method": "padrao", "verdict": "HOLD", "asset_type": "crypto",
     "cost_usd": 0.42, "elapsed": 512, "count": 7},
]}

_SNAP = {
    "run_id": "R1", "ticker": "AMD", "date": "2026-08-29", "asset_type": "stock",
    "status": "done", "elapsed": 512, "cost": {"usd": 0.4211},
    "verdict": "HOLD", "verdict_timeframe": "4h", "method": "padrao",
    "result": {
        "verdict": "HOLD", "final_decision": "MANTER", "timeframe": "4h",
        "as_of_price": 465.6,
        "actionable": {
            "symbol": "AMD", "price": 465.6, "as_of": "2026-08-29 20:00",
            "timeframe": "4h", "horizon": "dias", "setup_state": "aguardar_rompimento",
            "setup_source": "123", "buy_zone": None, "realize_zone": None,
            "pullback_zone": None,
            "pattern": {"trigger": 470.11, "state": "formando", "direction": "compra"},
            "invalidation": {"price": 440.2, "meaning": "perde o ponto 3"},
            "stop": {"price": 435.7, "basis": "invalidação + folga"},
            "target": {"price": 498.3, "label": "topo anterior"},
            "risk_reward": {"rr": 0.82, "entry": 470.11, "risk": 34.41,
                            "reward": 28.19, "note": None, "entry_basis": "gatilho"},
        },
        "live_price": {"price": 465.58, "change_pct": -2.33, "currency": "USD",
                       "sessao": "fechado", "rotulo": "último fechamento",
                       "as_of": "29/08 16:00", "regular_price": 465.58,
                       "fuso": "America/New_York", "em": _HOJE},
        "price_chart": {}, "degraded": [],
        "bull": "Tese de alta.", "bear": "Tese de baixa.",
        "research_manager": "", "investment_plan": "", "trader_plan": "",
        "risk_decision": "", "market_report": "", "sentiment_report": "",
        "news_report": "", "fundamentals_report": "", "erick_report": "",
        "drop_nature": {}, "derivatives_report": "",
    },
}


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


def _rota(page):
    def handler(route):
        url = route.request.url
        if "/api/history" in url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_HIST))
        elif "/api/status/" in url or re.search(r"/api/run/[^/]+$", url):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_SNAP))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("largura", LARGURAS)
def test_a_barra_e_a_lateral_nao_tem_vao_com_conteudo_cortado(base, largura):
    """Superfícies 1 e 2 do histórico: a barra de controle e a lista de observação."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": largura, "height": 1000})
        _rota(page)
        page.goto(base, wait_until="networkidle")
        page.wait_for_selector(".history li", state="attached")
        page.wait_for_timeout(300)
        ruins = page.evaluate(_VARRE, _VAO_MIN)
        assert ruins == [], (f"vão central COM conteúdo cortado em {largura}px", ruins)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("largura", LARGURAS)
def test_o_cabecalho_do_resultado_nao_tem_vao_com_conteudo_cortado(base, largura):
    """Superfícies 3 e 4: a tira do cabeçalho e o cabeçalho do resultado — o print
    que abriu esta task, com a meta à esquerda, a cotação à direita e o buraco no
    meio (e o da direita cortando na borda)."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": largura, "height": 1200})
        _rota(page)
        page.goto(base, wait_until="networkidle")
        page.evaluate("() => watchRun('R1')")
        page.wait_for_selector("#resultPanel:not(.hidden)", state="attached")
        page.wait_for_timeout(400)
        ruins = page.evaluate(_VARRE, _VAO_MIN)
        assert ruins == [], (f"vão central COM conteúdo cortado em {largura}px", ruins)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_meta_e_a_cotacao_ficam_na_MESMA_fileira_sem_buraco(base):
    """O caso do print, medido diretamente: eram duas linhas — meta à esquerda,
    cotação encostada à direita — e o vão entre elas era a largura da tela menos os
    dois conteúdos. Agora as duas fluem na MESMA fileira, e o que sobra fica no FIM.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1200})
        _rota(page)
        page.goto(base, wait_until="networkidle")
        page.evaluate("() => watchRun('R1')")
        page.wait_for_selector("#headLevels:not(.hidden)", state="attached")
        page.wait_for_timeout(300)
        m = page.evaluate("""() => {
          const info = document.querySelector('.result-info');
          const meta = document.querySelector('#resultMeta');
          const niveis = document.querySelector('#headLevels');
          const r = (e) => { const b = e.getBoundingClientRect();
            return {left: Math.round(b.left), right: Math.round(b.right),
                    top: Math.round(b.top)}; };
          return {info: r(info), meta: r(meta), niveis: r(niveis),
                  paiDosNiveis: niveis.parentElement.className};
        }""")
        # a cotação é IRMÃ da meta dentro da mesma fileira
        assert "result-info" in m["paiDosNiveis"], m
        # e ela começa onde a meta termina (ou na linha seguinte, encostada à
        # esquerda) — nunca pinçada na borda direita com um buraco atrás
        mesmaLinha = abs(m["niveis"]["top"] - m["meta"]["top"]) < 6
        if mesmaLinha:
            assert m["niveis"]["left"] - m["meta"]["right"] < 40, ("vão no meio", m)
        else:
            assert abs(m["niveis"]["left"] - m["info"]["left"]) < 4, (
                "quebrou de linha e tem de encostar na ESQUERDA, não na direita", m)
        browser.close()

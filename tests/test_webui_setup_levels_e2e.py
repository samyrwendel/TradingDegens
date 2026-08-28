"""E2E (Playwright): o 1-2-3 na tela agora diz onde INVALIDA, o SL, o TP e o R:R.

Queixa do Samyr (28/08): "vejo muito o 1-2-3 de compra e venda, mas não mostra onde
invalida e onde é o TP e o SL". O gráfico marcava os três pontos e a linha do gatilho
— e parava aí, o que não dá para operar.

Aqui provamos NA TELA:
  1. compra — legenda ganha invalidação / stop (SL) / alvo (TP), a nota escreve a
     FRASE da invalidação ("o setup morre se perder X") e a faixa do setup mostra o R:R;
  2. reconciliação — quando o alvo É a região de realização, sai UMA faixa só dizendo
     "realização = alvo (TP)"; quando a realização é o próprio gatilho, ela não é
     desenhada de novo (a linha do 1-2-3 já está lá);
  3. venda — o short não herda esqueleto de long: a realização vira "topo anterior
     (resistência)" e o alvo é o fundo abaixo;
  4. sem base — alvo nulo rende "sem nível definido" e R:R "sem base", nunca um número.

Screenshots (antes/depois, DA-062) em /tmp/devbot-td-levels. Pulado sem Playwright.
"""
import os
import threading

import pytest

from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.server import make_server
from tradingagents.webui.store import HistoryStore

pytestmark = pytest.mark.integration

sync_playwright = None
try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None

_SHOTS = "/tmp/devbot-td-levels"


def _shot(page, name):
    try:
        os.makedirs(_SHOTS, exist_ok=True)
        page.screenshot(path=os.path.join(_SHOTS, name), full_page=False)
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture
def live_server(tmp_path):
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path),
                                         "llm_provider": "openai",
                                         "deep_think_llm": "gpt-5.5",
                                         "quick_think_llm": "gpt-5.4-mini"},
                            store=HistoryStore(tmp_path))
    httpd = make_server("127.0.0.1", 0, runner=runner)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


# Candles sintéticos + marcadores do 1-2-3; o plano vem por argumento pra cada
# cenário (compra/venda/sem base) usar a MESMA série e só trocar os níveis.
_SEED_JS = r"""
(plan) => {
  document.getElementById('resultPanel').classList.remove('hidden');
  const candles = [];
  let p = 120;
  for (let i = 0; i < 120; i++) {
    const o = p;
    const c = p + Math.sin(i / 9) * 4 + ((i % 4) - 1.5) * 0.6;
    const h = Math.max(o, c) + 2;
    const l = Math.min(o, c) - 2;
    const dd = String((i % 28) + 1).padStart(2, '0');
    const mm = String((Math.floor(i / 28) % 12) + 1).padStart(2, '0');
    candles.push({ o, h, l, c, d: `2026-${mm}-${dd}` });
    p = c;
  }
  const chart = {
    symbol: 'SYN', timeframe: '1d', candles,
    ma: {}, ema: {}, ma_windows: [], ema_windows: [],
    markers: { buy_regions: [], active_region: null, pattern_123: plan.pattern },
  };
  renderHeadPrice(plan);
  renderActionable(plan);
  renderChartCard(chart, 'SYN', plan);
  return {
    legend: document.getElementById('chartLegend').textContent,
    note: document.getElementById('chartNote').textContent,
    strip: document.getElementById('actionable').textContent,
    zones: planZones(plan).map((z) => z.tag),
  };
}
"""

_P1 = {"date": "2026-01-05", "price": 118.0}
_P2 = {"date": "2026-02-10", "price": 152.0}
_P3 = {"date": "2026-03-08", "price": 131.0}


def _plan(**over):
    """Plano de COMPRA completo: 1-2-3 formando, alvo distinto da realização."""
    base = {
        "symbol": "SYN", "as_of": "2026-04-28", "price": 140.0,
        "timeframe": "diário (referência)", "horizon": "dias a semanas",
        "setup_state": "aguardar_rompimento",
        "buy_zone": None, "pullback_zone": None,
        "realize_zone": {"label": "topo anterior 2026-02-10", "price": 152.0,
                         "low": 150.0, "high": 154.0, "band_basis": "±0.5·ATR14",
                         "role": "gatilho", "role_label": "realização = gatilho do 1-2-3"},
        "pattern": {"p1": _P1, "p2": _P2, "p3": _P3, "trigger": 152.0,
                    "state": "formando", "direction": "compra"},
        "invalidation": {"label": "perda do ponto 3 (2026-03-08)", "price": 131.0,
                         "meaning": "o setup morre se perder 131.00 — abaixo do ponto 3 "
                                    "o fundo ascendente deixa de ser ascendente e o "
                                    "1-2-3 de compra não existe mais."},
        "stop": {"label": "stop (SL)", "price": 128.5, "anchor": 131.0, "atr": 5.0,
                 "basis": "invalidação + folga de 0.5·ATR14"},
        "target": {"label": "topo anterior 2025-11-20", "price": 176.0,
                   "low": 173.5, "high": 178.5, "band_basis": "±0.5·ATR14",
                   "same_as_realize": False},
        "risk_reward": {"entry": 152.0, "entry_basis": "gatilho — rompimento da máxima do ponto 2",
                        "risk": 23.5, "reward": 24.0, "rr": 1.02, "note": None},
    }
    base.update(over)
    return base


def _seed(page, plan):
    return page.evaluate(_SEED_JS, plan)


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_compra_mostra_invalidacao_stop_alvo_e_rr(live_server):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        try:
            page.goto(live_server)
            page.wait_for_selector("#priceChart", state="attached")

            # ANTES: o mesmo 1-2-3 como era — pontos e gatilho, sem SL/TP/invalidação
            antes = _plan(invalidation=None, stop=None, target=None, risk_reward=None,
                          realize_zone=None)
            out0 = _seed(page, antes)
            _shot(page, "antes-compra.png")
            assert "invalidação" not in out0["legend"]
            assert "stop (SL)" not in out0["legend"]

            # DEPOIS
            out = _seed(page, _plan())
            _shot(page, "depois-compra.png")

            assert "invalidação" in out["legend"]
            assert "stop (SL)" in out["legend"]
            assert "alvo (TP)" in out["legend"]
            # a FRASE da invalidação, não só o número
            assert "morre se perder" in out["note"]
            assert "131" in out["note"]
            # o stop declara a folga de ATR (não é percentual chutado)
            assert "0.5·ATR14" in out["note"]
            # R:R na faixa do setup
            assert "Risco/retorno" in out["strip"] and "1,02:1" in out["strip"]
            # a realização que É o gatilho não vira uma segunda faixa
            assert "realização (alvo)" not in out["zones"]
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_alvo_igual_a_realizacao_desenha_uma_faixa_so(live_server):
    """Mesmo nível → UMA faixa dizendo que são o mesmo; nunca duas concorrendo."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        try:
            page.goto(live_server)
            page.wait_for_selector("#priceChart", state="attached")
            plan = _plan(
                setup_state="ativo",
                pattern={"p1": _P1, "p2": _P2, "p3": _P3, "trigger": 152.0,
                         "state": "acionado", "direction": "compra"},
                realize_zone={"label": "topo anterior 2025-11-20", "price": 176.0,
                              "low": 173.5, "high": 178.5, "band_basis": "±0.5·ATR14",
                              "role": "alvo", "role_label": "realização (alvo)"},
                target={"label": "topo anterior 2025-11-20", "price": 176.0,
                        "low": 173.5, "high": 178.5, "band_basis": "±0.5·ATR14",
                        "same_as_realize": True},
            )
            out = _seed(page, plan)
            _shot(page, "depois-alvo-igual-realizacao.png")
            tags = out["zones"]
            assert tags.count("realização = alvo (TP)") == 1
            assert "alvo (TP)" not in tags          # não há uma SEGUNDA faixa de alvo
            assert "realização (alvo)" not in tags  # nem a etiqueta antiga sozinha
            assert "mesmo nível da região de realização" in out["note"]
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_venda_nao_herda_esqueleto_de_long(live_server):
    """No short: realização vira RESISTÊNCIA acima e o alvo é o fundo abaixo."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        try:
            page.goto(live_server)
            page.wait_for_selector("#priceChart", state="attached")
            plan = _plan(
                pattern={"p1": {"date": "2026-01-05", "price": 165.0},
                         "p2": {"date": "2026-02-10", "price": 128.0},
                         "p3": {"date": "2026-03-08", "price": 152.0},
                         "trigger": 128.0, "state": "acionado", "direction": "venda"},
                invalidation={"label": "retomada do ponto 3 (2026-03-08)", "price": 152.0,
                              "meaning": "o setup morre se voltar acima de 152.00 — acima do "
                                         "ponto 3 o topo descendente deixa de ser descendente "
                                         "e o 1-2-3 de venda não existe mais."},
                stop={"label": "stop (SL)", "price": 154.5, "anchor": 152.0, "atr": 5.0,
                      "basis": "invalidação + folga de 0.5·ATR14"},
                realize_zone={"label": "topo anterior 2026-03-08", "price": 152.0,
                              "low": 149.5, "high": 154.5, "band_basis": "±0.5·ATR14",
                              "role": "resistencia", "role_label": "topo anterior (resistência)"},
                target={"label": "fundo anterior 2025-10-14", "price": 112.0,
                        "low": 109.5, "high": 114.5, "band_basis": "±0.5·ATR14",
                        "same_as_realize": False},
                risk_reward={"entry": 140.0, "entry_basis": "preço atual (padrão já acionado)",
                             "risk": 14.5, "reward": 28.0, "rr": 1.93, "note": None},
            )
            out = _seed(page, plan)
            _shot(page, "depois-venda.png")
            assert "topo anterior (resistência)" in out["zones"]
            assert "realização (alvo)" not in out["zones"]   # topo NÃO é alvo de short
            assert "alvo (TP)" in out["zones"]
            assert "voltar acima" in out["note"]
            assert "fundo anterior" in out["note"]
            assert "1,93:1" in out["strip"]
        finally:
            browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="playwright/chromium indisponível")
def test_sem_base_diz_sem_nivel_definido(live_server):
    """Sem alvo e sem R:R a tela DECLARA a ausência — nunca preenche com número."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        try:
            page.goto(live_server)
            page.wait_for_selector("#priceChart", state="attached")
            plan = _plan(target=None, risk_reward=None, realize_zone=None)
            out = _seed(page, plan)
            _shot(page, "depois-sem-nivel.png")
            assert "Alvo (TP): sem nível definido" in out["note"]
            assert "Risco/retorno: sem base" in out["note"]
            # invalidação e stop continuam com número real
            assert "morre se perder" in out["note"]
            assert "128,5" in out["note"]
            assert "alvo (TP)" not in out["zones"]
        finally:
            browser.close()

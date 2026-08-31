"""E2E: A ESCADA na tela — os cinco frames de uma vez, com UM veredito.

*"Preciso que a análise do Storm123 e Setup123 seja mais ampla e na análise inicial
já faça os timeframes de 15m, 1h, 4h, D e S."* (task 20260831-012)

O risco desta entrega não é o custo (são $0 de LLM e ~2,6s a frio); é a
APRESENTAÇÃO. Cinco leituras com o mesmo peso na tela viram ruído — e pior, viram
cinco vereditos onde há um. Os invariantes que este arquivo trava saem direto da
spec de apresentação (``~/brain/trading-ops/tradingdegens-spec-apresentacao.md``):

* **invariante 3** — o veredito é distinguível de relance. UMA linha o carrega, com
  a palavra escrita (não só cor: a paleta é semântica de PREÇO, DA-078) e contraste
  maior que o das exploratórias, MEDIDO.
* **invariante 2** — nunca uma tupla inconsistente. O degrau ABERTO (o que o gráfico
  desenha) é marca PRÓPRIA e separada da do veredito: "o que estou vendo" e "o que
  decidiu" são duas coisas, e trocar de frame move só a primeira.
* **invariante 8** — estado ausente é declarado. Frame sem candle mantém a linha,
  diz que não há candle e não publica número nenhum.
* **DA-101** — no telefone se ENCOLHE pra caber: os cinco degraus continuam na tela,
  nada atrás de menu e nada transbordando na horizontal.
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


# O caso REAL que motivou a task (DOT-USD, 31/08): os frames DISCORDAM — venda no
# semanal, invalidado no diário, venda no 4h. É exatamente por discordarem que ver
# os cinco de uma vez vale alguma coisa; um fixture onde todos concordam não prova
# nada sobre a tela.
_FRAMES = [
    {"frame": "1w", "estado": "em_movimento", "direction": "venda", "pattern_state": "acionado",
     "trigger": 1.13, "price": 0.84, "dist_pct": 0.256, "sl": 1.19, "tp": 0.71,
     "rr": 0.72, "andado_pct": 68.0, "invalidacao": 1.19},
    {"frame": "1d", "estado": "invalidou", "direction": "venda", "pattern_state": "acionado",
     "trigger": 0.75, "price": 0.84, "dist_pct": 0.12, "sl": 0.79, "tp": 0.66,
     "rr": 0.13, "invalidacao": 0.79},
    {"frame": "4h", "estado": "em_gatilho", "direction": "venda", "pattern_state": "acionado",
     "trigger": 0.838, "price": 0.84, "dist_pct": 0.002, "sl": 0.862, "tp": 0.795,
     "rr": 1.79, "invalidacao": 0.862},
    {"frame": "1h", "estado": "formando", "direction": "compra", "pattern_state": "formando",
     "trigger": 0.851, "price": 0.84, "dist_pct": 0.013, "sl": 0.822, "tp": 0.88,
     "rr": 1.6},
    # O quinto NÃO tem candle: a fonte não cobre 15m deste símbolo nesta janela.
    {"frame": "15m", "estado": "sem_dado", "motivo": "fonte: intradiario_indisponivel",
     "price": 0.84},
]

_MULTIFRAME = {"veredito": "4h", "metodo": "setup123", "frames": _FRAMES, "ms": 2580}

_ACT = {
    "symbol": "DOT-USD", "price": 0.84, "as_of": "2026-08-31 04:00", "timeframe": "4h",
    "horizon": "dias", "setup_state": "ativo", "setup_source": "123",
    "buy_zone": None, "realize_zone": None, "pullback_zone": None,
    "pattern": {"p1": {"date": "2026-08-25", "price": 0.90},
                "p2": {"date": "2026-08-27", "price": 0.838},
                "p3": {"date": "2026-08-29", "price": 0.862},
                "trigger": 0.838, "state": "acionado", "direction": "venda"},
    "invalidation": {"label": "perda do ponto 3", "price": 0.862, "meaning": "…"},
    "stop": {"label": "stop (SL)", "price": 0.862, "anchor": 0.862, "atr": 0.01,
             "basis": "invalidação + folga de 0.5·ATR14"},
    "target": {"label": "fundo anterior", "price": 0.795, "same_as_realize": False},
    "risk_reward": {"entry": 0.838, "entry_basis": "gatilho", "risk": 0.024,
                    "reward": 0.043, "rr": 1.79, "note": None},
}


def _snap(multiframe=_MULTIFRAME, metodo="setup123"):
    return {
        "run_id": "R-012", "ticker": "DOT-USD", "date": "2026-08-31",
        "asset_type": "crypto", "status": "done", "elapsed": 3, "cost": {"usd": 0.0},
        "verdict": None, "verdict_timeframe": "4h",
        "result": {
            "setup123": metodo == "setup123", "storm123": metodo == "storm123",
            "verdict": None, "final_decision": "", "timeframe": "4h",
            "as_of_price": 0.84, "actionable": _ACT, "multiframe": multiframe,
            "live_price": None, "price_chart": {}, "degraded": [],
            "bull": "", "bear": "", "research_manager": "", "investment_plan": "",
            "trader_plan": "", "risk_decision": "", "market_report": "",
            "sentiment_report": "", "news_report": "", "fundamentals_report": "",
            "erick_report": "", "drop_nature": {}, "derivatives_report": "",
            "timeframes": ["1w", "1d", "4h", "1h", "15m"],
        },
    }


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


def _abre(page, base_url, snap=None, chart_tf=None):
    snap = snap or _snap()

    def handler(route):
        url = route.request.url
        if "/api/status/" in url or re.search(r"/api/run/[^/]+$", url):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(snap))
        elif "/api/chart" in url:
            tf = (re.search(r"[?&]tf=([^&]+)", url) or [None, "1d"])[1]
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "ticker": "DOT-USD", "date": "2026-08-31", "asset_type": "crypto",
                "timeframe": tf, "requested": tf,
                "timeframes": ["1w", "1d", "4h", "1h", "15m"],
                "degraded": False, "notice": None, "price_chart": {}, "actionable": _ACT,
            }))
        elif "/api/execucao" in url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps({}))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)
    page.goto(base_url, wait_until="networkidle")
    page.evaluate("() => watchRun('R-012')")
    page.wait_for_selector("#escada:not(.hidden)")
    page.wait_for_timeout(150)


def _medida(page):
    return page.evaluate("""() => {
      const rows = [...document.querySelectorAll('#escada .es-row')];
      const op = (e) => parseFloat(getComputedStyle(e).opacity);
      return {
        n: rows.length,
        tfs: rows.map(r => r.dataset.esTf),
        papeis: rows.map(r => (r.querySelector('.es-papel') || {}).innerText || ''),
        classes: rows.map(r => r.className),
        opac: rows.map(op),
        textos: rows.map(r => r.innerText.replace(/\\s+/g, ' ').trim()),
        resumo: (document.querySelector('#escada .es-resumo') || {}).innerText || '',
        nota: (document.querySelector('#escada .es-nota') || {}).innerText || '',
        custo: (document.querySelector('#escada .es-custo') || {}).innerText || '',
        overflow: document.querySelector('#escada').scrollWidth
                  - document.querySelector('#escada').clientWidth,
      };
    }""")


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_analise_inicial_ja_entrega_os_CINCO_frames(base):
    """O pedido, em uma asserção: cinco degraus na tela sem trocar de chip, na ordem
    da escada (maior manda na tese, menor no timing)."""
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1500, "height": 1200})
        _abre(page, base)
        m = _medida(page)
        assert m["n"] == 5, m
        assert m["tfs"] == ["1w", "1d", "4h", "1h", "15m"], m
        # o custo medido fica NA TELA: a decisão de manter cinco é de quem o vê
        assert "2,6s" in m["custo"] and "$0" in m["custo"], m
        b.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_veredito_e_UM_e_se_distingue_das_exploratorias(base):
    """Invariante 3. Um degrau diz "veredito"; os outros dizem "exploratório" — com
    a PALAVRA, porque verde/vermelho nesta tela querem dizer alta/baixa de preço
    (DA-078), não autoridade. E a diferença é de contraste MEDIDO, não de intenção."""
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1500, "height": 1200})
        _abre(page, base)
        m = _medida(page)
        vered = [i for i, t in enumerate(m["papeis"]) if "VEREDITO" in t.upper()]
        assert vered == [2], ("o veredito é UM, e é o 4h da run", m["papeis"])
        assert "es-veredito" in m["classes"][2], m
        explor = [i for i, t in enumerate(m["papeis"]) if "EXPLORAT" in t.upper()]
        assert len(explor) == 4, m["papeis"]
        # contraste: a linha do veredito não pode estar no mesmo peso das outras
        assert m["opac"][2] > max(m["opac"][i] for i in explor), m["opac"]
        # e a tela DIZ, por extenso, que o veredito é de um frame só
        assert "veredito é" in m["nota"] and "4h" in m["nota"], m["nota"]
        b.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_frame_sem_candle_declara_a_ausencia_e_nao_inventa_numero(base):
    """Invariante 8. O degrau do 15m FICA (sumir esconderia o quinto frame), diz que
    não há candle — e nenhum gatilho/SL/TP/R:R aparece nele."""
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1500, "height": 1200})
        _abre(page, base)
        m = _medida(page)
        linha = m["textos"][4]
        assert m["tfs"][4] == "15m", m
        assert "sem candle" in linha.lower(), linha
        assert not re.search(r"\d+[,.]\d", linha), ("nada de número num frame sem candle", linha)
        b.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_clicar_num_degrau_move_o_ABERTO_e_NAO_move_o_veredito(base):
    """Invariante 2, na versão que a escada acrescenta: o frame que o gráfico desenha
    é uma marca PRÓPRIA. Sem essa separação, clicar pra explorar o 1h faria a tela
    parecer dizer que o veredito passou a ser do 1h."""
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1500, "height": 1200})
        _abre(page, base)
        antes = _medida(page)
        assert "NO GRÁFICO" not in " ".join(antes["papeis"]).upper(), \
            "no início o frame aberto É o do veredito: uma marca só"
        page.click("#escada .es-row[data-es-tf='1h']")
        page.wait_for_timeout(400)
        depois = _medida(page)
        assert "VEREDITO" in depois["papeis"][2].upper(), ("o veredito não se move", depois["papeis"])
        assert "NO GRÁFICO" in depois["papeis"][3].upper(), depois["papeis"]
        assert page.evaluate("() => _tf") == "1h"
        # e os cards abaixo passaram a falar do frame aberto
        assert "1h" in page.inner_text("#setupCards .sc-frame-topo")
        b.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_resumo_compara_tese_e_timing_sem_inventar_um_sexto_veredito(base):
    """A única linha que AGREGA. Ela só soma direções já computadas — e diz de
    quantos frames está falando, porque um "2 de 5" sobre um total imaginário seria
    pior que não resumir."""
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1500, "height": 1200})
        _abre(page, base)
        m = _medida(page)
        # innerText vem em caixa alta (text-transform no rótulo do grupo)
        r = m["resumo"].replace("\n", " ").upper()
        assert "TESE" in r and "TIMING" in r, r
        assert "S · D" in r, ("a tese nomeia os frames que a compõem", r)
        # 1w e 1d são os dois de venda; no intra há venda (4h) e compra (1h), e o 15m
        # não vota (sem dado) — o resumo tem que mostrar o conflito, não uma maioria.
        assert "2 DE 2 DE VENDA" in r, r
        assert "1 DE COMPRA × 1 DE VENDA" in r, r
        assert "EM CONFLITO" in r, ("os cinco frames discordam e a tela diz isso", r)
        b.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_numa_run_STORM_a_escada_mostra_a_leitura_do_STORM(base):
    """O gráfico desenha a leitura que dá NOME ao método (DA-088) e a escada segue a
    mesma regra. E o Éden vetando NÃO publica níveis: oferecer gatilho e alvo de um
    trade que a própria regra proíbe é o pior tipo de número correto."""
    frames = [
        {"frame": "1w", "estado": "formando", "storm": {
            "estado": "vetado", "direction": "compra", "opera": False,
            "veto": "sem Éden alinhado — ARMADILHA", "eden_rotulo": "armadilha",
            "trigger": 1.02, "sl": 0.98, "tp": 1.20, "rr": 4.5}},
        {"frame": "1d", "estado": "formando", "storm": {
            "estado": "em_gatilho", "direction": "compra", "opera": True,
            "eden_rotulo": "alinhado", "entrada": "ponto3",
            "trigger": 0.85, "sl": 0.82, "tp": 0.91, "rr": 2.0, "dist_pct": 0.004}},
        {"frame": "4h", "estado": "formando", "storm": {
            "estado": "formando", "direction": "compra", "opera": True,
            "eden_rotulo": "alinhado", "entrada": "ponto2",
            "trigger": 0.87, "sl": 0.83, "tp": 0.95, "rr": 2.1, "dist_pct": 0.03}},
        {"frame": "1h", "estado": "sem_setup", "storm": {"estado": "sem_setup"}},
        {"frame": "15m", "estado": "sem_dado", "motivo": "fonte: intradiario_indisponivel"},
    ]
    snap = _snap({"veredito": "1d", "metodo": "storm123", "frames": frames, "ms": 2100},
                 metodo="storm123")
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1500, "height": 1200})
        _abre(page, base, snap)
        m = _medida(page)
        titulo = page.inner_text("#escada .section-title").upper()
        assert "STORM123" in titulo, titulo
        assert "VEREDITO" in m["papeis"][1].upper(), m["papeis"]
        vetada = m["textos"][0]
        assert "não opera" in vetada.lower() and "éden" in vetada.lower(), vetada
        assert "1,02" not in vetada and "4,5" not in vetada, \
            ("vetado pelo Éden não publica os níveis", vetada)
        # a leitura que OPERA continua com os números todos
        assert "0,85" in m["textos"][1] and "2" in m["textos"][1], m["textos"][1]
        # O nome do Éden vem PRONTO do produtor e às vezes já traz a palavra: prefixar
        # sem olhar saía "Éden Éden de Alta" (visto em BTC-USD storm123, 31/08).
        assert "éden éden" not in vetada.lower(), vetada
        # …e um frame VETADO não vota na direção do resumo: o método recusa operá-lo,
        # então contá-lo faria a tese somar um lado que não se pode tomar. Aqui só o
        # 1d e o 4h operam (compra); o 1w é vetado e o resumo tem que dizer isso.
        r = m["resumo"].replace("\n", " ").upper()
        assert "TESE" in r and "1 DE 2 DE COMPRA" in r, ("o 1w vetado não vota", r)
        b.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_uma_run_com_ERRO_nao_herda_a_escada_da_anterior(base):
    """A escada pertence ao RESULTADO. Deixá-la na tela sob um erro faria a análise
    que falhou parecer ter cinco leituras — o mesmo motivo pelo qual o gráfico e os
    cards já somem ali."""
    erro = _snap()
    erro["status"] = "error"
    erro["error"] = "fonte fora do ar"
    erro["result"] = {"partial": False}
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1500, "height": 1200})
        _abre(page, base)                       # primeiro a run boa: a escada aparece
        assert page.eval_on_selector("#escada", "e => !e.classList.contains('hidden')")
        page.evaluate("(s) => renderResult(s)", erro)
        page.wait_for_timeout(150)
        assert page.eval_on_selector("#escada", "e => e.classList.contains('hidden')"), \
            "erro não pode herdar a escada da análise anterior"
        b.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("w,h", [(390, 844), (360, 800)])
def test_no_telefone_a_escada_ENCOLHE_pra_caber_com_os_cinco_degraus(base, w, h):
    """DA-101: no celular se encolhe pra caber, nunca se esconde dado atrás de menu.
    Os cinco degraus continuam lá, cada um dizendo o nome do frame por extenso (o
    "S" sozinho, sem cabeçalho de coluna, não diz nada) e nada transborda na
    horizontal — barra lateral de rolagem numa tabela de preço é dado amputado."""
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": w, "height": h}, is_mobile=True,
                          has_touch=True, device_scale_factor=2)
        _abre(page, base)
        m = _medida(page)
        assert m["n"] == 5, m
        assert m["overflow"] <= 1, ("a escada não pode rolar na horizontal", m["overflow"])
        assert page.evaluate(
            "() => document.documentElement.scrollWidth <= window.innerWidth + 1"), \
            "a página inteira não pode ganhar rolagem horizontal por causa da escada"
        # o cabeçalho de coluna sai (coluna que não existe não se nomeia) e o nome do
        # frame por extenso entra no lugar
        assert page.evaluate(
            "() => getComputedStyle(document.querySelector('#escada .es-head-row')).display") == "none"
        assert "Semanal" in m["textos"][0], m["textos"][0]
        assert "Diário" in m["textos"][1], m["textos"][1]
        # …e NENHUM número fica órfão: sem cabeçalho de coluna, "0,84 0,86 0,8 1,79"
        # em fila é quatro números que não dizem qual é o gatilho e qual é o stop.
        vered = m["textos"][2].lower()
        for nome in ("dist", "gatilho", "sl", "tp", "r:r"):
            assert nome in vered, (f"a linha quebrada precisa nomear {nome}", vered)
        # o rótulo curto não se repete como se fosse o nome ("4h 4h")
        assert not re.search(r"\b4h\s+4h\b", m["textos"][2]), m["textos"][2]
        b.close()

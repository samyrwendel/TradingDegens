"""E2E: REAVALIAR segue o frame da TELA, e a troca de frame REVALIDA sozinha.

Dois pedidos do Samyr (task 20260831-013), e o primeiro é um bug:

**"Sempre mando revalidar em um timeframe ele tá voltando para o Diário."** Havia
DOIS chamadores de ``reevaluate()`` com semânticas opostas: o botão do banner de
fonte degradada rodava no frame do VEREDITO (tipicamente o Diário) e o do gráfico no
frame ATUAL. Mesma palavra, dois comportamentos, e o usuário só descobria qual era
pelo resultado — que é a definição de armadilha. Agora os dois seguem o frame da
tela e AMBOS o nomeiam no rótulo.

**"Se durante a mudança do tempo gráfico houver atualização de preço, quero a
revalidação automática."** O defeito concreto: ``switchTimeframe`` recalculava os
NÍVEIS mas reusava ``_openLive`` — a cotação congelada no instante em que a run foi
desenhada, porque o ``/api/chart`` não devolve preço live. A tira do cabeçalho
mostrava "cotação agora" de dez minutos atrás e calculava a DISTÂNCIA até o preço da
análise contra esse número velho. Agora a troca busca a cotação fresca em paralelo e,
quando o preço andou ≥0,5% desde a análise, a tela DIZ que revalidou e de quando é.

O que este arquivo trava:
  (a) o reavaliar do banner posta o frame DA TELA, e o rótulo o nomeia;
  (b) trocar de frame atualiza a cotação e a distância;
  (c) acima do limiar a revalidação é ANUNCIADA (com frame e hora);
  (d) abaixo do limiar não anuncia nada — aviso em toda troca é aviso que ninguém lê;
  (e) trocar cinco vezes seguidas NÃO vira cinco buscas de cotação (sem cascata);
  (f) desligado, volta ao comportamento antigo — e o botão diz que está desligado;
  (g) run de data PASSADA não busca cotação nenhuma (DA-073).
"""

import json
import re
import threading
import time

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

_HOJE = timeutil.today()

# Preço da ANÁLISE: 100,00. É contra ele que a distância e o limiar se medem.
_ACT = {
    "symbol": "XYZ-USD", "price": 100.0, "as_of": f"{_HOJE} 04:00", "timeframe": "1d",
    "horizon": "dias", "setup_state": "ativo", "setup_source": "123",
    "buy_zone": None, "realize_zone": None, "pullback_zone": None,
    "pattern": {"p1": {"date": _HOJE, "price": 90.0}, "p2": {"date": _HOJE, "price": 105.0},
                "p3": {"date": _HOJE, "price": 95.0}, "trigger": 105.0,
                "state": "formando", "direction": "compra"},
    "invalidation": {"label": "perda do ponto 3", "price": 95.0, "meaning": "…"},
    "stop": {"label": "stop (SL)", "price": 94.0, "anchor": 95.0, "atr": 1.0,
             "basis": "invalidação + folga"},
    "target": {"label": "topo anterior", "price": 115.0, "same_as_realize": False},
    "risk_reward": {"entry": 105.0, "entry_basis": "gatilho", "risk": 11.0,
                    "reward": 10.0, "rr": 0.9, "note": None},
}

# O gráfico precisa EXISTIR: o seletor de timeframe mora dentro do `.chart-card`,
# que fica escondido quando não há candle — e um botão invisível não se clica.
_CANDLES = [{"d": f"2026-08-{d:02d}", "o": 99.0, "h": 101.0, "l": 98.0, "c": 100.0}
            for d in range(1, 29)]
_CHART = {"symbol": "XYZ-USD", "candles": _CANDLES, "ma": {}, "ema": {},
          "ma_windows": [], "ema_windows": [],
          "markers": {"buy_regions": [], "active_region": None, "pattern_123": None}}

# A cotação DA RUN (congelada): igual ao preço da análise, distância zero.
_LIVE_RUN = {"price": 100.0, "change_pct": 0.0, "currency": "USD", "sessao": "24h",
             "rotulo": "cotação agora · 24h", "as_of": "04:05", "regular_price": 100.0,
             "fuso": "UTC", "em": _HOJE}


def _snap(date=None, degraded=None):
    return {
        "run_id": "R-013", "ticker": "XYZ-USD", "date": date or _HOJE,
        "asset_type": "crypto", "status": "done", "elapsed": 2, "cost": {"usd": 0.0},
        "verdict": None, "verdict_timeframe": "1d",
        "result": {
            "setup123": True, "verdict": None, "final_decision": "", "timeframe": "1d",
            "as_of_price": 100.0, "actionable": _ACT, "live_price": _LIVE_RUN,
            "price_chart": _CHART, "degraded": degraded or [], "multiframe": {},
            "bull": "", "bear": "", "research_manager": "", "investment_plan": "",
            "trader_plan": "", "risk_decision": "", "market_report": "",
            "sentiment_report": "", "news_report": "", "fundamentals_report": "",
            "erick_report": "", "drop_nature": {}, "derivatives_report": "",
            "timeframes": ["1w", "1d", "4h", "1h", "15m"],
        },
    }


_FONTE_CAIU = [{"label": "Finnhub", "report_key": "news_report",
                "reason": "timeout após nova tentativa", "kind": "missing"}]


@pytest.fixture
def servidor_lento(tmp_path):
    """Servidor cuja cotação demora 1,2s — DE VERDADE, não por sleep no route.

    O ``ThreadingHTTPServer`` atende em paralelo, então a lentidão fica só onde
    deveria: no /api/prices. É o único jeito honesto de medir se a troca de frame
    ficou pendurada nela.
    """
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "openai",
                     "deep_think_llm": "x", "quick_think_llm": "y"},
        store=HistoryStore(tmp_path))

    def lenta(tickers):
        time.sleep(1.2)
        runner.atendeu = True
        return {"XYZ-USD": {"price": 103.5, "change_pct": 1.0, "currency": "USD",
                            "sessao": "24h", "rotulo": "cotação agora · 24h",
                            "as_of": "10:31", "regular_price": 103.5, "fuso": "UTC"}}

    runner.atendeu = False
    runner.live_prices = lenta
    httpd = make_server("127.0.0.1", 0, runner=runner)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", runner
    finally:
        httpd.shutdown()


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


class _Espia:
    """Registra o que o front pediu e o que ele POSTOU — é onde o bug se prova."""

    def __init__(self, preco_live=100.0, atraso_precos=0.0, precos_no_servidor=False):
        self.precos = []        # cada GET /api/prices
        self.analises = []      # cada POST /api/analyze (corpo)
        self.charts = []
        self.preco_live = preco_live
        # Segura a resposta da cotação pra que a JANELA em que ela está em voo seja
        # real — é só dentro dela que a cascata de toques rápidos consegue existir.
        self.atraso_precos = atraso_precos
        # Deixa a cotação ir ao SERVIDOR (que é threaded) em vez de ser forjada aqui.
        # O ``page.route`` do Playwright síncrono atende UMA rota por vez: um sleep
        # dentro dele trava também o /api/chart, e aí o atraso que se quer medir na
        # cotação apareceria na troca de frame por artefato do teste, não do código.
        self.precos_no_servidor = precos_no_servidor


def _abre(page, base_url, espia, snap=None):
    snap = snap or _snap()

    def handler(route):
        req = route.request
        url = req.url
        if "/api/prices" in url:
            espia.precos.append(url)
            if espia.precos_no_servidor:
                route.continue_()
                return
            if espia.atraso_precos:
                time.sleep(espia.atraso_precos)
            corpo = {"prices": {"XYZ-USD": {
                "price": espia.preco_live, "change_pct": 1.0, "currency": "USD",
                "sessao": "24h", "rotulo": "cotação agora · 24h", "as_of": "10:31",
                "regular_price": espia.preco_live, "fuso": "UTC"}}}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(corpo))
        elif "/api/analyze" in url and req.method == "POST":
            espia.analises.append(json.loads(req.post_data or "{}"))
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"run_id": "R-NOVA", "run_token": "t"}))
        elif "/api/chart" in url:
            tf = (re.search(r"[?&]tf=([^&]+)", url) or [None, "1d"])[1]
            espia.charts.append(tf)
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "ticker": "XYZ-USD", "date": snap["date"], "asset_type": "crypto",
                "timeframe": tf, "requested": tf,
                "timeframes": ["1w", "1d", "4h", "1h", "15m"],
                "degraded": False, "notice": None,
                "price_chart": {**_CHART, "timeframe": tf}, "actionable": _ACT}))
        elif "/api/status/" in url or re.search(r"/api/run/[^/]+$", url):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(snap))
        elif "/api/execucao" in url:
            route.fulfill(status=200, content_type="application/json", body="{}")
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)
    page.goto(base_url, wait_until="networkidle")
    page.evaluate("() => { try { localStorage.removeItem('td_reval_auto'); } catch (e) {} }")
    page.evaluate("() => watchRun('R-013')")
    page.wait_for_selector("#setupCards:not(.hidden)")
    page.wait_for_timeout(150)


def _troca(page, tf):
    page.click(f"#tfSelector .tf-btn[data-tf='{tf}']")
    page.wait_for_function(f"() => _tf === '{tf}'", timeout=5000)
    page.wait_for_timeout(150)


# ------------------------------------------------------- (a) o BUG do frame ----
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_reavaliar_com_a_fonte_roda_no_frame_da_TELA_e_nao_no_do_veredito(base):
    """O bug em uma asserção. Veredito no Diário, tela no 4h: o POST tem que levar
    ``timeframe: "4h"``. Antes levava "1d" e o usuário voltava pro Diário sem pedir."""
    espia = _Espia()
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1500, "height": 1200})
        _abre(page, base, espia, _snap(degraded=_FONTE_CAIU))
        assert page.eval_on_selector("#degradedBanner", "e => !e.classList.contains('hidden')")
        _troca(page, "4h")
        assert page.evaluate("() => _verdictTf") == "1d", "o veredito continua no Diário"
        page.click("#reevalSourcesBtn")
        page.wait_for_timeout(300)
        assert espia.analises, "o clique tem que postar uma análise"
        assert espia.analises[-1]["timeframe"] == "4h", espia.analises[-1]
        b.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_rotulo_do_reavaliar_NOMEIA_o_frame_e_acompanha_a_troca(base):
    """Se o botão decide o frame, ele tem que DIZER qual — descobrir pelo resultado
    é o que fez o Samyr reportar isto como bug. E o rótulo segue a troca: parado no
    frame anterior ele volta a prometer o que não vai fazer."""
    espia = _Espia()
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1500, "height": 1200})
        _abre(page, base, espia, _snap(degraded=_FONTE_CAIU))
        assert "Diário" in page.inner_text("#reevalSourcesBtn"), page.inner_text("#reevalSourcesBtn")
        _troca(page, "15m")
        assert "15m" in page.inner_text("#reevalSourcesBtn"), page.inner_text("#reevalSourcesBtn")
        # e o botão do gráfico, que já seguia a tela, continua concordando com ele
        assert "15m" in page.inner_text("#reevalBtn"), page.inner_text("#reevalBtn")
        b.close()


# ------------------------------------- (b)(c) a revalidação ao trocar de frame --
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_trocar_de_frame_atualiza_a_cotacao_e_a_distancia(base):
    """O defeito concreto: a cotação ficava congelada na da run e a DISTÂNCIA até o
    preço da análise era calculada contra ela. Preço da análise 100,00; a run trouxe
    100,00 (distância zero) e agora o ativo vale 103,50."""
    espia = _Espia(preco_live=103.5)
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1500, "height": 1200})
        _abre(page, base, espia, _snap())
        antes = page.inner_text("#headPrice")
        assert "100,00" in antes and "3,50" not in antes, antes
        _troca(page, "4h")
        depois = page.inner_text("#headPrice")
        assert "103,50" in depois, ("a cotação tem que ser a de agora", depois)
        assert "3,50" in depois and "3,5%" in depois, \
            ("a distância se mede contra a cotação NOVA", depois)
        assert espia.precos, "a troca de frame busca a cotação"
        b.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_acima_do_limiar_a_revalidacao_e_ANUNCIADA_com_frame_e_hora(base):
    """O usuário tem que PERCEBER que revalidou — silêncio faz ele achar que a tela
    travou, e faz um número novo parecer o mesmo de antes. 3,5% > 0,5%."""
    espia = _Espia(preco_live=103.5)
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1500, "height": 1200})
        _abre(page, base, espia, _snap())
        assert page.eval_on_selector_all("#revalLinha .rv-nota", "e => e.length") == 0, \
            "sem troca de frame não houve revalidação a anunciar"
        _troca(page, "4h")
        nota = page.inner_text("#revalLinha .rv-nota")
        assert "revalidado" in nota.lower(), nota
        assert "4h" in nota, ("a nota diz em QUAL frame revalidou", nota)
        assert re.search(r"\d{2}:\d{2}", nota), ("…e de quando é a leitura", nota)
        assert "3,5" in nota and "+" in nota, ("…e o quanto o preço andou", nota)
        b.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_abaixo_do_limiar_nao_anuncia_nada(base):
    """0,2% < 0,5%: a cotação se atualiza (isso é verdade, não aviso), mas não há
    anúncio. Aviso que aparece em toda troca é aviso que ninguém lê."""
    espia = _Espia(preco_live=100.2)
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1500, "height": 1200})
        _abre(page, base, espia, _snap())
        _troca(page, "4h")
        assert "100,20" in page.inner_text("#headPrice"), page.inner_text("#headPrice")
        assert page.eval_on_selector_all("#revalLinha .rv-nota", "e => e.length") == 0, \
            page.inner_text("#revalLinha")
        b.close()


# ------------------------------------------------------ (e) sem cascata --------
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_trocar_cinco_vezes_seguidas_NAO_vira_cinco_buscas_de_cotacao(base):
    """Sem cascata. A cotação vale ~45s (o mesmo TTL do servidor), então cinco
    trocas dentro da janela reusam a mesma — e o selo `_tfSeq` já descarta a resposta
    de uma troca superada, então nada dispara por troca que não vingou."""
    espia = _Espia(preco_live=103.5)
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1500, "height": 1200})
        _abre(page, base, espia, _snap())
        for tf in ("4h", "1h", "15m", "1w", "1d"):
            _troca(page, tf)
        assert len(espia.precos) == 1, ("cinco trocas, uma cotação", espia.precos)
        assert len(espia.charts) == 5, ("os NÍVEIS, esses sim, são de cada frame", espia.charts)
        b.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_cotacao_NAO_segura_a_troca_de_frame(servidor_lento):
    """Regressão MEDIDA nesta entrega. A primeira versão pedia níveis e cotação num
    ``Promise.all`` — e com isso a troca de frame, que é a ação primária, passou a
    esperar o enriquecimento dela: o teste da escada quebrou porque o frame demorava
    a trocar. A troca desenha com o que tem; o preço entra por cima quando chega.

    Aqui a cotação demora 1,2s e a troca tem de acontecer MUITO antes disso — e a
    revalidação ainda assim aparece depois, sem ter atrasado nada."""
    base_url, runner = servidor_lento
    espia = _Espia(precos_no_servidor=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1500, "height": 1200})
        _abre(page, base_url, espia, _snap())
        t0 = time.monotonic()
        page.click("#tfSelector .tf-btn[data-tf='4h']")
        page.wait_for_function("() => _tf === '4h'", timeout=8000)
        gasto = time.monotonic() - t0
        assert gasto < 0.6, ("a troca não espera a cotação", gasto)
        # …e a revalidação chega DEPOIS, por cima, sem ter segurado a troca
        page.wait_for_selector("#revalLinha .rv-nota", timeout=8000)
        assert "103,50" in page.inner_text("#headPrice"), page.inner_text("#headPrice")
        assert runner.atendeu, "a cotação veio do servidor (lento), não do route"
        b.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_tres_toques_ANTES_da_primeira_resposta_ainda_sao_uma_busca_so(base):
    """A cascata que o TTL sozinho NÃO pega. O memo de 45s só existe depois que a
    resposta chega; três toques em meio segundo acontecem todos ANTES disso — e cada
    um abriria a sua busca para a mesma pergunta. Quem chega com uma busca em voo
    entra nela, então continua sendo uma."""
    espia = _Espia(preco_live=103.5, atraso_precos=0.5)
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1500, "height": 1200})
        _abre(page, base, espia, _snap())
        # sem esperar cada troca terminar: é isso que o dedo do usuário faz
        for tf in ("4h", "1h", "15m"):
            page.click(f"#tfSelector .tf-btn[data-tf='{tf}']")
        page.wait_for_function("() => _tf === '15m'", timeout=8000)
        page.wait_for_timeout(900)
        assert len(espia.precos) == 1, ("três toques, uma cotação", espia.precos)
        # e o que ficou na tela é do frame que VINGOU, não do primeiro toque
        assert "15m" in page.inner_text("#revalLinha"), page.inner_text("#revalLinha")
        b.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_cotacao_fora_do_ar_NAO_quebra_a_troca_de_frame(base):
    """Fail-open. A revalidação é enriquecimento: com a fonte de cotação fora do ar a
    troca de frame acontece igual, a tira volta pra cotação da run e NADA é anunciado —
    anunciar "revalidado" sem ter revalidado seria a pior das saídas."""
    espia = _Espia()
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1500, "height": 1200})

        snap = _snap()

        def handler(route):
            url = route.request.url
            if "/api/prices" in url:
                espia.precos.append(url)
                route.fulfill(status=500, content_type="application/json",
                              body=json.dumps({"error": "fonte fora do ar"}))
            elif "/api/chart" in url:
                tf = (re.search(r"[?&]tf=([^&]+)", url) or [None, "1d"])[1]
                espia.charts.append(tf)
                route.fulfill(status=200, content_type="application/json", body=json.dumps({
                    "ticker": "XYZ-USD", "date": snap["date"], "asset_type": "crypto",
                    "timeframe": tf, "requested": tf,
                    "timeframes": ["1w", "1d", "4h", "1h", "15m"],
                    "degraded": False, "notice": None,
                    "price_chart": {**_CHART, "timeframe": tf}, "actionable": _ACT}))
            elif "/api/status/" in url or re.search(r"/api/run/[^/]+$", url):
                route.fulfill(status=200, content_type="application/json", body=json.dumps(snap))
            elif "/api/execucao" in url:
                route.fulfill(status=200, content_type="application/json", body="{}")
            else:
                route.continue_()
        page.route(re.compile(r"/api/"), handler)
        page.goto(base, wait_until="networkidle")
        page.evaluate("() => { try { localStorage.removeItem('td_reval_auto'); } catch (e) {} }")
        page.evaluate("() => watchRun('R-013')")
        page.wait_for_selector("#setupCards:not(.hidden)")
        page.wait_for_timeout(150)
        _troca(page, "4h")
        assert page.evaluate("() => _tf") == "4h", "a troca acontece mesmo sem cotação"
        assert "100,00" in page.inner_text("#headPrice"), page.inner_text("#headPrice")
        assert page.eval_on_selector_all("#revalLinha .rv-nota", "e => e.length") == 0, \
            "sem cotação nova não há revalidação a anunciar"
        b.close()


# ------------------------------------------------------ (f) o desligar ---------
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_desligada_a_revalidacao_volta_ao_comportamento_antigo_e_diz_isso(base):
    """"Se atrapalhar, ele precisa poder desligar." Desligado: nenhuma busca de
    cotação, a tira volta a mostrar a da run, e o botão escreve que está desligado
    (a palavra, não uma cor — DA-078 regra 9)."""
    espia = _Espia(preco_live=103.5)
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1500, "height": 1200})
        _abre(page, base, espia, _snap())
        assert "ligado" in page.inner_text("#revalToggle").lower()
        page.click("#revalToggle")
        page.wait_for_timeout(100)
        assert "desligado" in page.inner_text("#revalToggle").lower(), page.inner_text("#revalToggle")
        _troca(page, "4h")
        assert espia.precos == [], ("desligada não busca cotação", espia.precos)
        assert "100,00" in page.inner_text("#headPrice"), page.inner_text("#headPrice")
        assert page.eval_on_selector_all("#revalLinha .rv-nota", "e => e.length") == 0
        b.close()


# ------------------------------------------------------ (g) data passada -------
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_run_de_data_PASSADA_nao_busca_cotacao_nenhuma(base):
    """DA-073: numa análise de ontem o preço de HOJE não pertence àquela leitura.
    Revalidar ali seria carimbar de novo um número que não é dali."""
    espia = _Espia(preco_live=103.5)
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1500, "height": 1200})
        _abre(page, base, espia, _snap(date="2026-08-20"))
        _troca(page, "4h")
        assert espia.precos == [], ("run de data passada não busca cotação", espia.precos)
        assert page.eval_on_selector_all("#revalLinha .rv-nota", "e => e.length") == 0
        b.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("w,h", [(390, 844), (360, 800)])
def test_no_telefone_o_controle_e_a_nota_cabem_e_ficam_junto_do_grafico(base, w, h):
    """DA-101 + a armadilha do `order`: sem ordem declarada o bloco iria pro topo do
    card no telefone, longe do controle a que pertence. Ele fica DEPOIS do canvas,
    junto do reavaliar, e nada transborda na horizontal."""
    espia = _Espia(preco_live=103.5)
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": w, "height": h}, is_mobile=True,
                          has_touch=True, device_scale_factor=2)
        _abre(page, base, espia, _snap())
        _troca(page, "4h")
        m = page.evaluate("""() => {
          const l = document.getElementById('revalLinha');
          const cv = document.querySelector('.chart-wrap');
          const bt = document.getElementById('reevalBtn');
          const r = l.getBoundingClientRect();
          return {topo: r.top, canvas: cv.getBoundingClientRect().bottom,
                  reeval: bt.getBoundingClientRect().top,
                  overflow: l.scrollWidth - l.clientWidth,
                  texto: l.innerText.replace(/\\s+/g, ' ')};
        }""")
        assert m["topo"] > m["canvas"], ("no telefone o bloco fica ABAIXO do gráfico", m)
        assert m["topo"] >= m["reeval"], ("…e depois do reavaliar", m)
        assert m["overflow"] <= 1, ("nada de rolagem horizontal", m)
        assert "revalidado" in m["texto"].lower() and "4h" in m["texto"], m["texto"]
        assert page.evaluate(
            "() => document.documentElement.scrollWidth <= window.innerWidth + 1")
        b.close()

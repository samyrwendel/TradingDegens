"""O método STORM na webui (task 022): chip próprio, rota própria, $0 — e o VETO.

Três coisas que este arquivo trava:

1. **Método SEPARADO, não flag do 1-2-3.** ``storm123`` tem chip próprio na barra,
   sobe pela rota estrutural (sem LLM) e grava o SEU marcador no resultado. Um run
   Storm nunca é lido como run 1-2-3 (nem vira coluna de confronto).
2. **A isenção de portão entra pela LISTA da task 007**, não por um segundo ``if``
   paralelo — era o segundo caminho de isenção que reabriria o buraco daquela task.
   POST anônimo de ``storm123`` puro passa; com qualquer chave desconhecida no corpo
   (``compare``, ``x``) o portão volta.
3. **O Éden é VETO declarado.** Sem alinhamento a tela diz NÃO OPERA e o motivo —
   nunca um setup silenciosamente rebaixado —, e um setup vetado não ganha traço no
   gráfico (o gráfico é a figura operável; o card mantém cada número).
"""

import json
import re
import threading
import urllib.error
import urllib.request

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


# ------------------------------------------------------- portão de custo -------
def _post(url, corpo):
    req = urllib.request.Request(
        url, data=json.dumps(corpo).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def test_storm_anonimo_passa_pelo_portao_porque_nao_custa_nada(base, monkeypatch):
    """$0 de LLM: o portão protegeria um custo que não existe — a mesma razão que
    já valia pro 1-2-3."""
    st, body = _post(base + "/api/analyze",
                     {"ticker": "MSFT", "date": "2026-08-29", "method": "storm123",
                      "timeframe": "1d", "compare": False})
    assert st == 200, body
    assert body.get("run_id")


def test_a_isencao_do_storm_nao_reabre_o_buraco_da_task_007(base):
    """Chave desconhecida no corpo = rota que ninguém auditou. O portão volta —
    inclusive (e principalmente) com ``compare``, que foi exatamente o furo."""
    st, body = _post(base + "/api/analyze",
                     {"ticker": "MSFT", "date": "2026-08-29", "method": "storm123",
                      "compare": True})
    assert st == 403, (st, body)
    st2, body2 = _post(base + "/api/analyze",
                       {"ticker": "MSFT", "date": "2026-08-29", "method": "storm123",
                        "rota_nova": True})
    assert st2 == 403, (st2, body2)


def test_metodo_desconhecido_nao_herda_a_isencao(base):
    st, _ = _post(base + "/api/analyze",
                  {"ticker": "MSFT", "date": "2026-08-29", "method": "storm999"})
    assert st == 403


# ----------------------------------------------------------- marcador/método ---
def test_o_resultado_do_storm_se_identifica_pelo_SEU_marcador():
    from tradingagents.webui.compare import detect_method
    assert detect_method({"result": {"storm123": True, "setup123": False}}) == "storm123"
    assert detect_method({"result": {"setup123": True, "storm123": False}}) == "setup123"
    # e uma leitura estrutural nunca vira a coluna de um confronto
    assert detect_method({"result": {"erick_report": "x"}}) == "erick"


# ------------------------------------------------------------------- a tela ---
_ENTRADAS = [
    {"entrada": "ponto2", "label": "rompimento da máxima do ponto 2", "trigger": 108.0,
     "state": "formando", "ordem": "confirmada",
     "ordem_label": "espera a confirmação — gatilho mais longe, risco maior, menos sinal falso",
     "state_label": "em formação — o gatilho ainda não foi rompido"},
    {"entrada": "ponto3", "label": "rompimento da máxima do ponto 3", "trigger": 105.0,
     "state": "formando", "ordem": "antecipada",
     "ordem_label": "entra antes — gatilho mais próximo, risco menor, mais sinal falso",
     "state_label": "em formação — o gatilho ainda não foi rompido"},
]
_PAT = {
    "p1": {"date": "2026-08-20", "price": 110.0, "open": 100.0, "high": 110.0,
           "low": 99.0, "close": 108.0},
    "p2": {"date": "2026-08-21", "price": 90.0, "open": 107.0, "high": 108.0,
           "low": 90.0, "close": 92.0},
    "p3": {"date": "2026-08-22", "price": 105.0, "open": 93.0, "high": 105.0,
           "low": 92.0, "close": 104.0},
    "direction": "compra", "amplitude": 20.0, "entradas": _ENTRADAS,
}


def _leitura(entrada, trigger, alvo, rr):
    base = next(e for e in _ENTRADAS if e["entrada"] == entrada)
    return {
        **base,
        "target": {"label": ("projeção da amplitude dos 3 candles (20,00) a partir do "
                             f"gatilho do {entrada.replace('ponto', 'ponto ')}"),
                   "price": alvo, "amplitude": 20.0, "low": None, "high": None,
                   "band_basis": None, "same_as_realize": False},
        "risk_reward": {"entry": trigger, "entry_basis": f"gatilho — {base['label']}",
                        "risk": round(trigger - 90.0, 2), "reward": round(alvo - trigger, 2),
                        "rr": rr, "note": None},
    }


def _storm(opera=True, **over):
    plano = {
        "symbol": "MSFT", "as_of": "2026-08-29", "price": 104.0,
        "timeframe": "diário (referência)",
        "eden": {"disponivel": True, "alinhado": True, "direcao": "compra",
                 "armadilha": False, "ema_rapida": 101.2, "ema_lenta": 88.4,
                 "preco": 104.0, "motivo": "MME 8 acima da MME 80 e preço acima das duas"},
        "pattern": dict(_PAT),
        "ema_lenta_no_p3": 88.4,
        "invalidation": {"label": "perda do ponto 2 (2026-08-21)", "price": 90.0,
                         "meaning": "o setup morre se perder o ponto 2 — é o fundo que a reversão declarou"},
        "stop": {"label": "stop (SL)", "price": 90.0, "anchor": 90.0, "atr": 4.0,
                 "slack": 0.0, "basis": "no ponto 2 — a spec põe o stop abaixo dele"},
        "leituras": [_leitura("ponto2", 108.0, 128.0, 1.11),
                     _leitura("ponto3", 105.0, 125.0, 1.33)],
        "qualidade": "perfeita", "opera": True, "veto": None,
        "motivo": "ponto 3 inteiro acima da MME 80 — a tendência principal sustenta a reversão",
    }
    if not opera:
        plano.update({
            "eden": {"disponivel": True, "alinhado": False, "direcao": None,
                     "armadilha": True, "ema_rapida": 101.2, "ema_lenta": 118.0,
                     "preco": 104.0,
                     "motivo": ("ARMADILHA: preço acima da MME 8 mas ABAIXO da MME 80 — "
                                "repique dentro de tendência de baixa, não reversão")},
            "qualidade": "ruim", "opera": False,
            "veto": ("sem Éden alinhado — ARMADILHA: preço acima da MME 8 mas ABAIXO da "
                     "MME 80 — repique dentro de tendência de baixa, não reversão"),
        })
    plano.update(over)
    return plano


def _snap(storm):
    return {
        "run_id": "R-STORM", "ticker": "MSFT", "date": "2026-08-29",
        "asset_type": "stock", "status": "done", "elapsed": 2, "cost": {"usd": 0.0},
        "verdict": None, "verdict_timeframe": "1d", "method": "storm123",
        "result": {
            "storm123": True, "setup123": False, "verdict": None, "final_decision": "",
            "timeframe": "1d", "as_of_price": 104.0,
            "actionable": {
                "symbol": "MSFT", "price": 104.0, "as_of": "2026-08-29",
                "timeframe": "1d", "horizon": "dias", "setup_state": "sem_setup",
                "setup_source": None, "buy_zone": None, "realize_zone": None,
                "pullback_zone": None, "pattern": None, "invalidation": None,
                "stop": None, "target": None, "risk_reward": None,
                "storm": storm,
            },
            "live_price": None, "price_chart": {}, "degraded": [],
            "bull": "", "bear": "", "research_manager": "", "investment_plan": "",
            "trader_plan": "", "risk_decision": "", "market_report": "",
            "sentiment_report": "", "news_report": "", "fundamentals_report": "",
            "erick_report": "", "drop_nature": {}, "derivatives_report": "",
        },
    }


def _abre(page, base_url, storm, largura=1500):
    snap = _snap(storm)

    def handler(route):
        url = route.request.url
        if "/api/status/" in url or re.search(r"/api/run/[^/]+$", url):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(snap))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)
    page.goto(base_url, wait_until="networkidle")
    page.evaluate("() => watchRun('R-STORM')")
    page.wait_for_selector("#setupCards:not(.hidden)")
    page.wait_for_timeout(120)


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_storm_tem_chip_proprio_na_barra(base):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.goto(base, wait_until="networkidle")
        page.wait_for_selector("#launchMethods .lb-method")
        m = page.evaluate("""() => ({
          metodos: [...document.querySelectorAll('#launchMethods .lb-method')]
            .map(b => b.dataset.method),
          rotulos: [...document.querySelectorAll('#launchMethods .lb-method')]
            .map(b => b.innerText.trim()),
          fileiras: [...document.querySelectorAll('#launchMethods .lb-method-row')]
            .map(r => [...r.querySelectorAll('.lb-method')].map(b => b.dataset.method)),
        })""")
        # DUAS fileiras contando como UM elemento da barra: em cima os que rodam
        # MODELO (custam), embaixo os ESTRUTURAIS ($0). Cinco numa fila só empurravam
        # a barra além dos 1440 e o campo ATIVO encolhia pra pagar.
        assert m["fileiras"] == [["padrao", "erick", "compare"],
                                 ["setup123", "storm123"]], m
        assert "Storm123" in m["rotulos"], m
        # e o chip do Storm NÃO é o chip do 1-2-3 (são métodos, não uma flag)
        assert m["metodos"].count("storm123") == 1 and m["metodos"].count("setup123") == 1
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_card_do_storm_traz_os_niveis_DELE(base):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1100})
        _abre(page, base, _storm())
        m = page.evaluate("""() => ({
          titulo: document.querySelector('#setupCards .sc-storm .sc-title').innerText,
          txt: document.querySelector('#setupCards .sc-storm').innerText,
          chaves: [...document.querySelectorAll('#setupCards .sc-storm .sc-k')]
            .map(e => e.innerText.trim()),
          leituras: document.querySelectorAll('#setupCards .sc-storm .sc-leitura').length,
          badge: document.querySelector('#verdictBadge').innerText.trim(),
        })""")
        assert "Storm123" in m["titulo"] and "de compra" in m["titulo"], m
        assert m["badge"] == "Storm123", ("o veredito do cabeçalho diz o método", m)
        # os níveis DELE: ponto 2 como invalidação/stop, alvo por projeção, R:R.
        # Stop e invalidação são O MESMO nível no Storm (sem folga inventada), então
        # saem numa linha só — dois rótulos com o mesmo número seria a duplicata que
        # a DA-077 proíbe.
        assert "stop (SL) = invalidação (ponto 2)" in m["chaves"], m
        assert m["txt"].count("90,00") == 1, ("o ponto 2 aparece UMA vez", m["txt"])
        # AS DUAS ENTRADAS, cada uma com o seu gatilho, o seu alvo e o seu R:R —
        # colapsá-las num número esconderia justamente a que entra antes (023).
        assert "108,00" in m["txt"] and "128,00" in m["txt"], m
        assert "105,00" in m["txt"] and "125,00" in m["txt"], m
        assert "entrada no ponto 2" in m["txt"] and "entrada no ponto 3" in m["txt"], m
        # `innerText` aplica o `text-transform` do CSS — o qualificador sai em caixa alta
        assert "ANTECIPADA" in m["txt"].upper() and "CONFIRMADA" in m["txt"].upper(), m
        assert m["chaves"].count("gatilho") == 2, ("um gatilho por leitura", m["chaves"])
        assert m["leituras"] == 2, ("duas leituras, com régua entre elas", m)
        assert "amplitude" in m["txt"], m
        assert "1,11:1" in m["txt"] and "1,33:1" in m["txt"], m
        # o Éden com as DUAS médias — veto que não se confere é palpite
        assert "101,20" in m["txt"] and "88,40" in m["txt"], m
        # opera + qualidade
        assert "opera" in m["txt"] and "perfeita" in m["txt"], m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_sem_eden_a_tela_diz_NAO_OPERA_e_o_motivo(base):
    """Veto declarado, nunca um setup silenciosamente rebaixado — e a ARMADILHA
    aparece nomeada, que é o caso mais caro."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1100})
        _abre(page, base, _storm(opera=False))
        m = page.evaluate("""() => ({
          txt: document.querySelector('#setupCards .sc-storm').innerText,
          classe: document.querySelector('#setupCards .sc-storm').className,
          borda: getComputedStyle(document.querySelector('.sc-storm')).borderLeftColor,
          veto: document.querySelector('.sc-veto') ? document.querySelector('.sc-veto').innerText : "",
        })""")
        assert "NÃO OPERA" in m["txt"], m
        assert "ARMADILHA" in m["veto"], m
        assert "sc-vetado" in m["classe"], m
        # A cor era o estado (DA-076); a DA-078 tirou o âmbar da paleta, então o
        # vetado sai em CINZA — distinto de quem opera (azul) — e quem afirma é a
        # manchete "NÃO OPERA" logo acima.
        assert m["borda"] == "rgb(139, 151, 173)", m
        # os números continuam à vista: "por que não opera" precisa do que ele seria
        assert "90,00" in m["txt"] and "128,00" in m["txt"], m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_setup_vetado_nao_ganha_NIVEL_no_grafico(base):
    """O gráfico é a figura OPERÁVEL. Desenhar os NÍVEIS de um trade que a regra proíbe
    é convidar a operá-lo — e nada se perde, o card tem cada número.

    O PADRÃO em si é outra coisa: ele passou a ser desenhado mesmo vetado, com o veto
    escrito na vela (task 034) — ver o setup que não se opera é parte de aprender a
    reconhecê-lo. O que este teste trava é a fronteira entre os dois."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1100})
        _abre(page, base, _storm())
        opera = page.evaluate(
            """(st) => planZones({storm: st}).map(z => z.tag)""", _storm())
        # Cada LEITURA desenha o seu gatilho e o seu alvo, com o nome dela no rótulo
        # — dois "Storm · gatilho" no mesmo gráfico seriam dois níveis com o mesmo
        # nome, que é o defeito da DA-075. O stop é UM (comum às duas entradas).
        assert opera == ["Storm · stop (SL)",
                         "Storm p2 · gatilho", "Storm p2 · alvo (TP)",
                         "Storm p3 · gatilho", "Storm p3 · alvo (TP)"], opera
        vetado = page.evaluate(
            """(st) => planZones({storm: st}).map(z => z.tag)""", _storm(opera=False))
        assert vetado == [], vetado
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_sem_padrao_o_card_ainda_declara_o_eden(base):
    """"Por que não opera" é informação — a tela não pode ficar muda justamente no
    caso em que o filtro fez o seu trabalho."""
    vazio = _storm()
    vazio.update({"pattern": None, "invalidation": None, "stop": None, "target": None,
                  "risk_reward": None, "qualidade": None, "opera": False, "veto": None,
                  "motivo": "nenhum 1-2-3 Storm na janela lida"})
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1100})
        _abre(page, base, vazio)
        txt = page.inner_text("#setupCards .sc-storm")
        assert "Nenhum 1-2-3 Storm" in txt, txt
        assert "101,20" in txt and "88,40" in txt, ("o Éden continua declarado", txt)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("w,h", [(1500, 1100), (390, 844)])
def test_o_card_do_storm_cabe_no_desktop_e_no_telefone(base, w, h):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": w, "height": h})
        _abre(page, base, _storm())
        m = page.evaluate("""() => {
          const card = document.querySelector('#setupCards .sc-storm');
          const c = card.getBoundingClientRect();
          const fora = [...card.querySelectorAll('.sc-row')].filter(e => {
            const r = e.getBoundingClientRect();
            return r.left < c.left - 1 || r.right > c.right + 1; }).map(e => e.innerText);
          return {fora, rola: document.documentElement.scrollWidth >
                              document.documentElement.clientWidth};
        }""")
        assert m["fora"] == [] and not m["rola"], m
        browser.close()


# ─────────────────── ZONA NEUTRA na tela (task 20260830-016) ──────────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("largura", [1500, 390])
def test_a_zona_neutra_NAO_se_veste_de_opera_limpo(base, largura):
    """O terceiro estado do Éden precisa se distinguir de relance. "opera · qualidade
    zona neutra" lido rápido vira só "opera", e o aviso do Stormer ("operar aqui é
    muito mais perigoso") sumiria justamente na leitura que importa."""
    st = _storm()
    st["qualidade"] = "neutra"
    st["opera"] = True
    st["veto"] = None
    st["motivo"] = ("ZONA NEUTRA (entre a MME 8 e a MME 80): a estrutura existe e vai a "
                    "favor das médias (compra), mas operar aqui é muito mais perigoso — "
                    "o setup vale MENOS e exige seletividade extra. Não é veto; é aviso.")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": largura, "height": 1100})
        _abre(page, base, st)
        m = page.evaluate("""() => {
          const el = document.querySelector('#setupCards .sc-storm');
          const s = el.querySelector('.sc-state');
          return {estado: s.innerText.trim(), cls: s.className,
                  txt: el.innerText,
                  cortados: [...el.querySelectorAll('*')]
                    .filter(e => e.scrollWidth > e.clientWidth + 1).length};
        }""")
        assert "OPERA COM CAUTELA" in m["estado"], m
        assert "zona neutra" in m["estado"], ("a qualidade nomeada", m)
        assert "ativo" not in m["cls"], ("não pode se vestir do verde de 'ativo'", m)
        assert "muito mais perigoso" in m["txt"], m["txt"]
        assert "seletividade extra" in m["txt"], m["txt"]
        assert "Não é veto" in m["txt"], ("o que ela É, escrito", m["txt"])
        assert m["cortados"] == 0, ("nada cortado", m)
        browser.close()

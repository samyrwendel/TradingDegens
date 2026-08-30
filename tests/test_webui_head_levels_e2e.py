"""E2E do rodapé do cabeçalho: gatilhos no canto inferior direito + preço que DIZ
qual preço é (task de UI 010, pedidos 2 e 3 do Samyr).

Pedido 2: o veredito FICA em cima; os gatilhos descem pro canto inferior direito do
card, ao lado do preço atual, alinhados à direita.

Pedido 3: a análise 1-2-3 tem que buscar a cotação ATUAL, e a tela tem que dizer QUE
preço está mostrando. O plano é date-guarded — o número que ele carrega é o último
FECHAMENTO da série (MSFT em 29/08: 505,06 de 27/08 com o papel valendo 513,53) — e
a tela o exibia como se fosse "agora". Fechamento, pré-market e after-market são
preços diferentes; o rótulo é o que impede a tela de chamar qualquer um de "agora".

Task 021: a segunda linha da tira — o PLANO (gatilho · SL · TP · R:R) — saiu daqui.
Ela não era uma família do cabeçalho: era o conteúdo de UMA das duas leituras que a
tela desenha (o padrão 1-2-3), e foi pro card dela, na coluna de contexto. O que o
pedido 2 defendia — os níveis não terem que ser caçados no texto abaixo do gráfico —
continua travado, agora contra o card. No cabeçalho fica o MERCADO, que é o chão
comum: não pertence a leitura nenhuma.
"""

import json
import re
import threading

import pytest

from tradingagents.webui import timeutil
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.server import make_server
from tradingagents.webui.store import HistoryStore

# A cotação só vale como ATUAL no dia em que foi tirada (DA-073), e o front confere
# isso contra o dia de HOJE. Carimbar uma data FIXA na fixture faz o teste passar o
# dia todo e quebrar sozinho à meia-noite — foi o que aconteceu na virada de 29 pra
# 30/08. O carimbo acompanha o relógio do servidor, que é a mesma fonte que o front lê.
_HOJE = timeutil.today()

pytestmark = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None


_SNAP = {
    "run_id": "R-HEAD", "ticker": "MSFT", "date": "2026-08-29", "asset_type": "stock",
    "status": "done", "elapsed": 1, "cost": {"usd": 0.0},
    "verdict": None, "verdict_timeframe": "1d",
    "result": {
        "setup123": True, "verdict": None, "final_decision": "",
        "timeframe": "1d", "as_of_price": 505.06,
        "actionable": {
            "price": 505.06, "as_of": "2026-08-27", "setup_state": "aguardar_rompimento",
            "pattern": {"trigger": 512.76, "state": "formando", "direction": "compra"},
            "stop": {"price": 471.35, "basis": "invalidação + folga"},
            "target": {"price": 515.06, "label": "topo anterior 2025-09-19"},
            "risk_reward": {"rr": 0.06, "entry": 512.76, "risk": 41.41, "reward": 2.3,
                            "note": None, "entry_basis": "gatilho"},
            "invalidation": {"price": 476.25, "meaning": "perde o ponto 3"},
        },
        # cotação ATUAL com a sessão declarada (o que o runner passou a anexar)
        "live_price": {"price": 513.53, "change_pct": 1.68, "currency": "USD",
                       "sessao": "fechado", "rotulo": "último fechamento",
                       "as_of": "28/08 16:00", "regular_price": 513.53,
                       "fuso": "America/New_York", "em": _HOJE},
        "price_chart": {}, "degraded": [],
        "bull": "", "bear": "", "research_manager": "", "investment_plan": "",
        "trader_plan": "", "risk_decision": "", "market_report": "",
        "sentiment_report": "", "news_report": "", "fundamentals_report": "",
        "erick_report": "", "drop_nature": {}, "derivatives_report": "",
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


def _abre_resultado(page, snap):
    """Serve a run pronta e manda o front abri-la (sem rodar nada)."""
    def handler(route):
        url = route.request.url
        if "/api/status/" in url or re.search(r"/api/run/[^/]+$", url):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(snap))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)
    page.goto(page.url if page.url.startswith("http") else "about:blank")


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_os_niveis_saem_do_cabecalho_e_viram_o_card_do_123(base):
    """Pedido 2 na forma da 021: o VEREDITO continua em cima e a tira do cabeçalho
    segue encostada à direita — mas ela carrega só o MERCADO. Os níveis operáveis
    (gatilho · SL · TP · R:R) não voltaram pro texto abaixo do gráfico: eles são o
    conteúdo do card do PADRÃO 1-2-3, que os mostra com o nome da análise no
    título. O que não pode acontecer é o número existir nos dois lugares."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_resultado(page, _SNAP)
        page.goto(base, wait_until="networkidle")
        page.evaluate("() => watchRun('R-HEAD')")
        page.wait_for_selector("#headLevels:not(.hidden)")
        page.wait_for_selector("#setupCards:not(.hidden)")

        m = page.evaluate("""() => {
          const box = (s) => { const e = document.querySelector(s); if (!e) return null;
            const r = e.getBoundingClientRect(); return {top: r.top, right: r.right, left: r.left}; };
          const c123 = document.querySelector('#setupCards .sc-123');
          return {veredito: box('#verdictBadge'), tira: box('#headLevels'),
                  preco: box('#headPrice'), card: box('#resultPanel'),
                  txtTira: document.querySelector('#headLevels').innerText,
                  titulo: c123.querySelector('.sc-title').innerText,
                  txt123: c123.innerText};
        }""")
        # veredito EM CIMA da tira (pedido: ele permanece onde está)
        assert m["veredito"]["top"] < m["tira"]["top"], m
        # A tira DEIXOU de ser pinçada na borda direita (DA-078 regra 11, task 004):
        # ela flui junto da meta e a sobra de espaço fica no FIM da fileira. O que
        # vale agora é ela começar onde a informação começa.
        info = page.evaluate("""() => Math.round(
            document.querySelector('.result-info').getBoundingClientRect().left)""")
        assert m["tira"]["left"] - info < 8, ("a tira começa junto com a informação", m, info)
        # o card do 1-2-3 DIZ de qual análise se trata e carrega os níveis
        assert "Setup123" in m["titulo"], m
        for chave, num in (("gatilho", "512,76"), ("stop (SL)", "471,35"), ("alvo (TP)", "515,06")):
            assert chave in m["txt123"] and num in m["txt123"], (chave, m)
        # DENTE: o mesmo número nos dois lugares era o defeito que a 021 matou
        for num in ("512,76", "471,35", "515,06"):
            assert num not in m["txtTira"], (num, m["txtTira"])
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_preco_diz_QUAL_preco_e(base):
    """DENTE do pedido 3: sem o rótulo, a tela mostra 505,06 (fechamento de 27/08)
    como se fosse a cotação de agora. Com ele, aparecem os DOIS, cada um nomeado."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_resultado(page, _SNAP)
        page.goto(base, wait_until="networkidle")
        page.evaluate("() => watchRun('R-HEAD')")
        page.wait_for_selector("#headPrice:not(.hidden)")
        txt = page.inner_text("#headPrice")
        assert "513,53" in txt, txt                     # a cotação atual
        assert "ÚLTIMO FECHAMENTO" in txt.upper(), txt  # DIZENDO que é fechamento
        assert "28/08 16:00" in txt, txt                # e de quando, no fuso da bolsa
        assert "505,06" in txt and "27/08" in txt, txt  # o preço da ANÁLISE ao lado
        assert "ANÁLISE" in txt.upper(), txt
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_cotacao_de_ontem_nao_se_disfarca_de_agora(base):
    """A run é persistida inteira: reaberta amanhã, a cotação carimbada com o dia
    anterior não pode aparecer como atual. Só o preço da análise sobra."""
    snap = json.loads(json.dumps(_SNAP))
    snap["result"]["live_price"]["em"] = "2020-01-01"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_resultado(page, snap)
        page.goto(base, wait_until="networkidle")
        page.evaluate("() => watchRun('R-HEAD')")
        page.wait_for_selector("#headPrice:not(.hidden)")
        txt = page.inner_text("#headPrice")
        assert "513,53" not in txt, txt
        assert "505,06" in txt, txt
        browser.close()


# ---------------------------------------------------------------------------
# Task 018 — "arruma essa bagunça": a tira eram DUAS famílias numa fila só
#
# O que saía na tela (ZEC-USD, print do Samyr):
#   ⬆️ gatilho 834,82  🛑 SL 764,76  🎯 TP 856,72  ⚖️ R:R 0,31  835,37
#   COTAÇÃO AGORA · 24H · 29/08 20:42 ANÁLISE 834,74 em 2…
# — oito números em fila, correndo até a borda direita, com a última data cortada.
#
# Quatro defeitos, e a forma escolhida pra cada um:
#   1. corria até a borda      → DUAS LINHAS, e cada preço é uma unidade `nowrap`:
#                                a tira quebra ENTRE unidades, nunca dentro
#   2. famílias sem separação  → linha de cima MERCADO, linha de baixo PLANO (com o
#                                rótulo dizendo o nome da família)
#   3. rótulo e hora grudados  → o rótulo é rótulo (caixa alta apagada) e a hora é
#                                carimbo (mono), cada um DENTRO da sua unidade
#   4. "análise" sem peso      → virou régua de 1px (peso visual de verdade) + rótulo
# ---------------------------------------------------------------------------

_ZEC = {
    "run_id": "R-ZEC", "ticker": "ZEC-USD", "date": "2026-08-29", "asset_type": "crypto",
    "status": "done", "elapsed": 1, "cost": {"usd": 0.0},
    "verdict": "HOLD", "verdict_timeframe": "4h",
    "result": {
        "setup123": True, "verdict": "HOLD", "final_decision": "MANTER",
        "timeframe": "4h", "as_of_price": 834.74,
        "actionable": {
            # as_of REAL da run de produção: no intradiário ele traz a HORA do candle
            "price": 834.74, "as_of": "2026-08-29 20:00", "setup_state": "setup_ativo",
            "pattern": {"trigger": 834.82, "state": "rompeu_retracou", "direction": "compra"},
            "stop": {"price": 764.76, "basis": "invalidação + folga"},
            "target": {"price": 856.72, "label": "topo anterior"},
            "risk_reward": {"rr": 0.31, "entry": 834.82, "risk": 70.06, "reward": 21.9,
                            "note": None, "entry_basis": "gatilho"},
            "invalidation": {"price": 790.29, "meaning": "perde o ponto 3"},
        },
        "live_price": {"price": 835.37, "change_pct": 4.32, "currency": "USD",
                       "sessao": "24h", "rotulo": "cotação agora · 24h",
                       "as_of": "29/08 20:42", "regular_price": 835.37,
                       "fuso": "UTC", "em": _HOJE},
        "price_chart": {}, "degraded": [],
        "bull": "", "bear": "", "research_manager": "", "investment_plan": "",
        "trader_plan": "", "risk_decision": "", "market_report": "",
        "sentiment_report": "", "news_report": "", "fundamentals_report": "",
        "erick_report": "", "drop_nature": {}, "derivatives_report": "",
    },
}

# NADA pode ser cortado. `scrollWidth > clientWidth` NÃO basta e o mutante provou:
# com a tira alinhada à direita, o que não cabe transborda pra ESQUERDA, e scrollWidth
# (que só conta o transbordo à direita, em LTR) fica igual ao clientWidth — a tira
# cortada passava no teste. O que responde "sumiu informação?" é a caixa de cada peça
# estar DENTRO da caixa visível da tira, nos dois lados.
_CORTADOS = """() => {
  const tira = document.querySelector('#headLevels');
  const t = tira.getBoundingClientRect();
  const pecas = [...tira.querySelectorAll('.hp-unit, .hp-k')];
  const fora = pecas.filter(e => { const r = e.getBoundingClientRect();
    return r.left < t.left - 1 || r.right > t.right + 1; });
  const cortado = [tira, ...tira.querySelectorAll('*')]
    .filter(e => e.scrollWidth > e.clientWidth + 1);
  return [...fora, ...cortado].map(e => ({cls: e.className, txt: e.innerText.slice(0, 30)}));
}"""


def _abre_zec(page, base, snap=None):
    _abre_resultado(page, snap or _ZEC)
    page.goto(base, wait_until="networkidle")
    page.evaluate("() => watchRun('R-ZEC')")
    page.wait_for_selector("#headLevels:not(.hidden)")
    page.wait_for_timeout(120)


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_as_duas_familias_ficam_em_superficies_separadas_e_com_nome(base):
    """A 018 separou MERCADO e PLANO em duas linhas da mesma tira; a 021 os separa em
    duas SUPERFÍCIES, porque nunca foram duas famílias do mesmo assunto — um é o chão
    comum (a cotação) e o outro é o conteúdo de uma das leituras. O que se prova aqui
    é que a tira ficou só com o mercado, em duas unidades fechadas, e que o plano tem
    caixa própria com título."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_zec(page, base)
        m = page.evaluate("""() => ({
          unidades: [...document.querySelectorAll('#headPrice .hp-unit')]
            .map(u => u.innerText.split('\\n').join(' ')),
          filhosDaTira: [...document.querySelector('#headLevels').children].map(c => c.id),
          titulo123: document.querySelector('#setupCards .sc-123 .sc-title').innerText,
          txtTira: document.querySelector('#headLevels').innerText,
        })""")
        # a tira carrega SÓ o mercado — a linha do plano saiu daqui
        assert m["filhosDaTira"] == ["headPrice"], m
        assert "PLANO" not in m["txtTira"].upper(), m
        # e a linha de mercado são DUAS unidades fechadas (cotação | análise)
        assert len(m["unidades"]) == 2, m
        assert "835,37" in m["unidades"][0] and "COTAÇÃO AGORA" in m["unidades"][0].upper(), m
        assert "834,74" in m["unidades"][1] and "ANÁLISE" in m["unidades"][1].upper(), m
        # o plano ganhou caixa E nome: "Padrão 1-2-3 de compra"
        assert "Setup123" in m["titulo123"] and "compra" in m["titulo123"], m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("largura", [1500, 1280, 1100])
def test_a_tira_degrada_empilhando_e_nunca_cortando(base, largura):
    """O pior defeito da tira era informação SUMINDO na borda ('em 2…'). Aqui se
    prova o contrário nas três larguras: tudo continua na tela, e o que falta de
    espaço vira quebra ENTRE unidades — nunca corte dentro de uma."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": largura, "height": 950})
        _abre_zec(page, base)
        assert page.evaluate(_CORTADOS) == [], f"algo foi cortado em {largura}px"
        txt = page.inner_text("#headLevels")
        for n in ("835,37", "834,74"):
            assert n in txt, (n, largura, txt)
        assert "29/08 20:42" in txt and "29/08 20:00" in txt, (largura, txt)
        # os níveis não sumiram da tela — mudaram de caixa (task 021)
        card = page.inner_text("#setupCards")
        for n in ("834,82", "764,76", "856,72", "0,31"):
            assert n in card, (n, largura, card)
        assert page.evaluate(
            "() => document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_com_a_coluna_espremida_a_tira_quebra_sem_perder_nada(base):
    """A lateral arrastada encolhe o card sem tocar no viewport (é o mesmo vetor que
    pegou o bug da container query na 010). Card em ~590px: a tira TEM que empilhar."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_zec(page, base)
        alt = page.evaluate(
            "() => Math.round(document.querySelector('#headLevels').getBoundingClientRect().height)")
        page.evaluate("""() => document.querySelector('main.layout')
            .style.setProperty('--sidebar-w', '1100px')""")
        page.wait_for_timeout(200)
        assert page.evaluate(_CORTADOS) == [], "nada pode ser cortado com o card espremido"
        m = page.evaluate("""() => ({
          cardW: Math.round(document.querySelector('#resultPanel').getBoundingClientRect().width),
          tiraH: Math.round(document.querySelector('#headLevels').getBoundingClientRect().height),
          rola: document.documentElement.scrollWidth > document.documentElement.clientWidth,
        })""")
        assert m["cardW"] < 450, m
        # a falta de espaço vira ALTURA (empilhou), não sumiço: 43px → 83px medidos
        assert m["tiraH"] > alt, ("com o card espremido a tira empilha", alt, m)
        assert not m["rola"], m
        # e a quebra preferida é ENTRE unidades: com o card em 394px as duas ainda
        # cabem inteiras, cada uma numa linha só.
        rects = page.evaluate("""() => [...document.querySelectorAll('#headLevels .hp-unit')]
            .map(u => u.getClientRects().length)""")
        assert rects == [1, 1], ("unidade partida ao meio sem necessidade", rects)
        txt = page.inner_text("#headLevels")
        assert "834,74" in txt and "29/08 20:00" in txt, txt
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_cada_carimbo_de_hora_mora_dentro_da_sua_unidade(base):
    """Defeito 3: 'COTAÇÃO AGORA · 24H · 29/08 20:42 ANÁLISE 834,74 em 2…' — não dava
    pra saber de quem era o horário. O que resolve não é texto novo: é cada momento
    ficar DENTRO da unidade do seu preço, e o rótulo ter peso diferente do carimbo."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_zec(page, base)
        m = page.evaluate("""() => {
          const un = [...document.querySelectorAll('#headPrice .hp-unit')].map(u => ({
            txt: u.innerText.replace(/\\n/g, ' '),
            quando: [...u.querySelectorAll('.hp-when')].map(e => e.innerText.trim()),
            tags: [...u.querySelectorAll('.hp-tag, .hp-k')].map(e => e.innerText.trim())}));
          const est = (s) => { const c = getComputedStyle(document.querySelector(s));
            return {size: c.fontSize, cor: c.color, fam: c.fontFamily.split(',')[0]}; };
          return {un, tag: est('#headPrice .hp-tag'), quando: est('#headPrice .hp-when'),
                  num: est('#headPrice .hp-live b')};
        }""")
        # cada unidade carrega UM carimbo — o seu
        assert [u["quando"] for u in m["un"]] == [["29/08 20:42"], ["29/08 20:00"]], m
        assert m["un"][0]["tags"] == ["COTAÇÃO AGORA · 24H"], m
        assert m["un"][1]["tags"] == ["ANÁLISE"], m
        # e o carimbo NÃO se veste de rótulo: mono, e não a caixa-alta de 10px
        assert m["quando"]["fam"] != m["tag"]["fam"], m
        assert m["quando"]["fam"] == m["num"]["fam"], ("hora é número, fonte de número", m)
        assert float(m["tag"]["size"][:-2]) < float(m["num"]["size"][:-2]), m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_separacao_entre_cotacao_e_analise_tem_peso_visual(base):
    """Defeito 4: o 'análise' solto tinha o mesmo peso do resto e não separava nada.
    Virou régua sólida de 1px (DA-070: régua, não degradê) — e ela some quando não há
    o que separar (run reaberta noutro dia, sem cotação atual)."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_zec(page, base)
        regua = page.evaluate("""() => { const c = getComputedStyle(document.querySelector('.hp-ref'));
          return {w: c.borderLeftWidth, estilo: c.borderLeftStyle, pad: c.paddingLeft,
                  img: c.borderImageSource, fundo: c.backgroundImage}; }""")
        assert regua["estilo"] == "solid" and regua["w"] != "0px", regua
        assert float(regua["pad"][:-2]) >= 10, regua
        # DA-070: nada de degradê nem na borda nem no fundo
        assert regua["img"] == "none" and regua["fundo"] == "none", regua

        # sem cotação atual, a régua não abre a linha sozinha
        snap = json.loads(json.dumps(_ZEC))
        snap["result"]["live_price"]["em"] = "2020-01-01"
        page2 = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_zec(page2, base, snap)
        m = page2.evaluate("""() => {
          const c = getComputedStyle(document.querySelector('.hp-ref'));
          return {w: c.borderLeftWidth, txt: document.querySelector('#headPrice').innerText};
        }""")
        assert m["w"] == "0px", m
        assert "835,37" not in m["txt"] and "834,74" in m["txt"], m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_momento_mostra_a_hora_quando_o_dado_tem_hora_e_nunca_inventa(base):
    """A hora do candle vinha no as_of e era JOGADA FORA por fmtDate ('em 29/08'),
    justo o dado que distingue o momento da análise do da cotação — os dois caem no
    mesmo dia num frame de 4h. Mostrar não é inventar: sem hora no dado, nada aparece."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_zec(page, base)
        m = page.evaluate("""() => [
          fmtMomento('2026-08-29 20:00'), fmtMomento('2026-08-29T20:00:00-04:00'),
          fmtMomento('2026-08-29'), fmtMomento(''), fmtMomento('nada disso')]""")
        assert m == ["29/08 20:00", "29/08 20:00", "29/08", "", "nada disso"], m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_rotulo_mais_longo_do_backend_nao_transborda_no_card_estreito(base):
    """O rótulo da cotação não é sempre curto: ``live_price.py`` produz "último
    fechamento (pré-market sem negócio ainda)". Com a unidade travada em
    ``white-space: nowrap``, esse rótulo passava POR CIMA da borda num card de ~390px
    — o defeito 1 (informação saindo da caixa) voltando por outra porta. A unidade
    fica numa linha só enquanto couber e quebra por DENTRO quando não couber; o que
    nunca parte é o número e o carimbo de hora."""
    snap = json.loads(json.dumps(_ZEC))
    snap["result"]["live_price"]["rotulo"] = "último fechamento (pré-market sem negócio ainda)"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_zec(page, base, snap)
        assert page.evaluate(_CORTADOS) == [], "o rótulo longo não pode transbordar já no card largo"
        page.evaluate("""() => document.querySelector('main.layout')
            .style.setProperty('--sidebar-w', '1100px')""")
        page.wait_for_timeout(200)
        # DENTE: aqui a unidade travada em nowrap saía da tira ("835,37 ÚLTIMO FECHAM…")
        assert page.evaluate(_CORTADOS) == [], "rótulo longo + card estreito não pode transbordar"
        m = page.evaluate("""() => ({
          txt: document.querySelector('#headLevels').innerText,
          atomicos: [...document.querySelectorAll('#headLevels .hp-unit b, #headLevels .hp-when')]
            .map(e => e.getClientRects().length),
        })""")
        assert "835,37" in m["txt"] and "29/08 20:42" in m["txt"], m
        assert set(m["atomicos"]) == {1}, ("número e carimbo não partem", m)
        browser.close()

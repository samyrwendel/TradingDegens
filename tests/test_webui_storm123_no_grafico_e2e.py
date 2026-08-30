"""STORM123 NO GRÁFICO — os três pontos, o fantasma e a taxa (task 20260830-032).

Os três pedidos do Samyr depois de aprovar a spec:

  1. *"colocasse a numeração 123 nos pontos que vc identificou"* — o Storm entrava no
     gráfico só como LINHAS de nível; os círculos numerados eram do detector de
     swings, de OUTRO método. E quando os pontos do Storm passaram a ser desenhados
     (DA-088), saíram sem o PREÇO ao lado — o número que se usa pra montar a ordem;
  2. *"marcação fantasma do 123 se ele foi invalidado"* — o fantasma existia no CARD
     e no 1-2-3 de swings, nunca no Storm dentro do gráfico: um Storm morto continuava
     com a cor de um vivo e com gatilho, alvo e stop traçados;
  3. *"medir as taxas de acertos"* — a taxa tem de aparecer ONDE SE DECIDE, com o gate
     de N declarado e a EXPECTATIVA liderando (70% de acerto com R:R 0,13 perde
     dinheiro).

E a armadilha que atravessa os três: as duas leituras numeram 1-2-3 pontos
DIFERENTES. Com as duas camadas ligadas, ①②③ de uma não pode ser lida como ①②③ da
outra — por isso a FORMA do marcador é a família (círculo = Setup123, losango =
Storm123), e a legenda carrega a mesma forma.
"""

import json
import re

import pytest

from tests.test_webui_frame_e_cor_e2e import DESKTOP, TELEFONE, sobe_servidor
from tests.test_webui_um_grafico_um_metodo_e2e import _CHART, _PLANO, _STORM

pytestmark = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None


@pytest.fixture
def base(tmp_path):
    yield from sobe_servidor(tmp_path)


def _storm(*, invalidado=False):
    """O Storm do fixture comum, com a morte ligada ou desligada."""
    pat = dict(_STORM["pattern"], invalidado=invalidado,
               invalidado_em="2026-08-27" if invalidado else None)
    return {**_STORM, "pattern": pat}


# Índice de confiabilidade: os dois extremos do gate de N, como o backend os monta.
_CONF_SEM_AMOSTRA = {
    "n_minimo": 5, "n_operavel": 20,
    "setups": {
        "123": {"n": 4, "n_fechados": 3, "nivel": "insuficiente", "expectativa_r": None,
                "rr_medio": None, "acerto_equilibrio": None, "n_com_rr": 0,
                "taxa_acerto": None, "ic95": None,
                "texto": "amostra insuficiente (n=3) — sem fechados suficientes pra "
                         "medir; track record em construção."},
        "storm": {"n": 2, "n_fechados": 2, "nivel": "insuficiente", "expectativa_r": None,
                  "rr_medio": None, "acerto_equilibrio": None, "n_com_rr": 0,
                  "taxa_acerto": None, "ic95": None,
                  "texto": "amostra insuficiente (n=2) — sem fechados suficientes pra "
                           "medir; track record em construção."},
    },
}

_CONF_COM_AMOSTRA = {
    "n_minimo": 5, "n_operavel": 20,
    "setups": {
        "123": {"n": 30, "n_fechados": 22, "nivel": "operavel", "expectativa_r": -0.42,
                "rr_medio": 0.13, "acerto_equilibrio": 0.885, "n_com_rr": 22,
                "taxa_acerto": 0.7, "ic95": [0.4869, 0.8535],
                "texto": "amostra operável — a taxa já é número de trabalho."},
        "storm": {"n": 40, "n_fechados": 25, "nivel": "operavel", "expectativa_r": 0.35,
                  "rr_medio": 1.2, "acerto_equilibrio": 0.4545, "n_com_rr": 25,
                  "taxa_acerto": 0.6, "ic95": [0.4074, 0.766],
                  "texto": "amostra operável — a taxa já é número de trabalho."},
    },
}


def _card(conf):
    return {"veredito": {"estado": "aguardar_recuo", "rotulo": "aguardar recuo",
                         "motivo": "o preço está esticado sobre o gatilho",
                         "rr_agora": 0.9, "nivel": 452.0, "direcao": "venda"},
            "ordens": [], "invalidacao": {"price": 466.0, "meaning": "retomada do ponto 2"},
            "saida": None, "protecao": {}, "peso": None, "confiabilidade": conf}


def _snapshot(*, invalidado=False):
    st = _storm(invalidado=invalidado)
    r = {
        "verdict": None, "final_decision": "", "timeframe": "1d",
        "as_of_price": 465.58, "actionable": {**_PLANO, "storm": st},
        "live_price": None, "price_chart": _CHART, "degraded": [],
        "bull": "", "bear": "", "research_manager": "", "investment_plan": "",
        "trader_plan": "", "risk_decision": "", "market_report": "",
        "sentiment_report": "", "news_report": "", "fundamentals_report": "",
        "erick_report": "", "drop_nature": {}, "derivatives_report": "",
        "setup123": False, "storm123": True,
    }
    return {"run_id": "R-032", "ticker": "MSFT", "date": "2026-08-29",
            "asset_type": "stock", "status": "done", "elapsed": 2,
            "cost": {"usd": 0.0}, "verdict": None, "verdict_timeframe": "1d",
            "result": r}


def _abre(page, base_url, *, invalidado=False, conf=None):
    snap = _snapshot(invalidado=invalidado)
    card = _card(conf) if conf is not None else None

    def handler(route):
        url = route.request.url
        if "/api/execucao" in url:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ticker": "MSFT", "card": card}))
        elif "/api/status/" in url or re.search(r"/api/run/[^/]+$", url):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(snap))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)
    page.goto(base_url, wait_until="networkidle")
    page.evaluate("() => watchRun('R-032')")
    page.wait_for_selector("#setupCards:not(.hidden)")
    page.wait_for_timeout(250)


# O que foi realmente PINTADO na vela — família, número, preço, forma, cor, fantasma.
_PINTADO = "() => JSON.parse(document.getElementById('priceChart').dataset.pat123 || '[]')"
_PILULAS = "() => JSON.parse(document.getElementById('priceChart').dataset.axisPills || '[]')"
_ROTULOS = "() => JSON.parse(document.getElementById('priceChart').dataset.levelLabels || '[]')"
_LEGENDA = "() => document.getElementById('chartLegend').innerHTML"


# ─────────────────── (1) os três pontos do Storm, com PREÇO ────────────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [DESKTOP, TELEFONE], ids=["desktop", "telefone"])
def test_os_tres_pontos_do_storm_saem_numerados_e_com_preco(base, viewport):
    """DENTE: o método que o Samyr mais usa era o único cujos pontos não diziam
    quanto valem — o marcador dizia "aqui houve um ponto", nunca a que altura."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre(page, base)
        pintado = page.evaluate(_PINTADO)
        storm = [d for d in pintado if d["familia"] == "storm"]
        assert [d["lab"] for d in storm] == ["1", "2", "3"], pintado
        # o preço de CADA ponto, na formatação da tela (pt-BR)
        assert [d["preco"] for d in storm] == ["474,00", "436,00", "466,00"], storm
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_setup123_tambem_leva_preco__a_regra_e_a_mesma_dos_dois_lados(base):
    """O outro lado da régua: o desenhador é UM só, então o que vale pro Storm vale
    pro plano — se este teste cair, os dois blocos voltaram a divergir."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        page.click('.camada-btn[data-camada="plano"]')
        page.wait_for_timeout(250)
        plano = [d for d in page.evaluate(_PINTADO) if d["familia"] == "plano"]
        assert [d["lab"] for d in plano] == ["1", "2", "3"], plano
        assert [d["preco"] for d in plano] == ["470,00", "440,00", "462,00"], plano
        browser.close()


# ──────────── (2) as duas camadas ligadas: ①②③ de um ≠ ①②③ do outro ───────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [DESKTOP, TELEFONE], ids=["desktop", "telefone"])
def test_com_as_duas_camadas_a_numeracao_nao_confunde(base, viewport):
    """As duas leituras numeram pontos DIFERENTES (aqui o 2 é o topo do repique; no
    Storm é o EXTREMO). A cor não separa — o azul do Setup123 de compra e o azul do
    Storm são o mesmo azul de longe. A FORMA separa."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre(page, base)
        page.click('.camada-btn[data-camada="plano"]')
        page.wait_for_timeout(250)
        pintado = page.evaluate(_PINTADO)
        formas = {d["familia"]: d["forma"] for d in pintado}
        assert formas == {"plano": "circulo", "storm": "losango"}, pintado
        assert len({d["forma"] for d in pintado}) == 2, ("mesma forma pros dois = a "
                                                         "colisão de volta", pintado)
        # e cada família se NOMEIA na tela, no candle e na legenda
        legenda = page.evaluate(_LEGENDA)
        assert "Setup123 1-2-3" in legenda and "Storm123 1-2-3" in legenda, legenda
        assert 'class="sw dot"' in legenda and 'class="sw dia"' in legenda, (
            "a legenda tem de carregar a FORMA, senão o leitor fica sem a chave", legenda)
        # E O DESENHO TEM DE TERMINAR. A de-colisão dos rótulos já travou a aba aqui:
        # duas caixas se empurravam pra sempre porque a borda reativava a mesma caixa
        # por arredondamento binário. Gráfico em laço infinito congela a tela inteira —
        # e é justo neste caso (telefone, duas famílias, seis marcadores em ~250px de
        # largura útil) que os rótulos mais se disputam.
        ms = page.evaluate("""() => { const cv = document.getElementById('priceChart');
            const t = performance.now();
            drawPriceChart(cv, cv._chart, cv._actionable);
            return performance.now() - t; }""")
        assert ms < 250, ("o desenho não pode se arrastar (nem travar)", ms)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_camada_desligada_nao_deixa_nivel_orfao_no_eixo(base):
    """O gatilho do 1-2-3 de swings ficava na régua mesmo com a camada do plano
    desligada: um preço operável de uma leitura que o usuário mandou sumir."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        so_storm = page.evaluate(_PILULAS)
        assert not any("440" in t for t in so_storm), (
            "gatilho do plano no eixo com a camada do plano desligada", so_storm)
        page.click('.camada-btn[data-camada="plano"]')
        page.wait_for_timeout(250)
        duas = page.evaluate(_PILULAS)
        assert any("440" in t for t in duas), ("ligada, ela volta", duas)
        browser.close()


# ──────────────────── (3) o FANTASMA do Storm, no GRÁFICO ─────────────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [DESKTOP, TELEFONE], ids=["desktop", "telefone"])
def test_storm_invalidado_vira_fantasma_no_grafico(base, viewport):
    """DENTE: o fantasma existia no card e no 1-2-3 de swings — no gráfico o Storm
    morto continuava com a cor de um vivo, e a cor é a primeira coisa que se lê."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)

        _abre(page, base, invalidado=False)
        vivo = [d for d in page.evaluate(_PINTADO) if d["familia"] == "storm"]
        assert vivo and not any(d["fantasma"] for d in vivo), vivo

        _abre(page, base, invalidado=True)
        morto = [d for d in page.evaluate(_PINTADO) if d["familia"] == "storm"]
        assert morto, "o morto continua desenhado — a história explica onde o preço está"
        assert all(d["fantasma"] for d in morto), morto
        assert morto[0]["cor"] != vivo[0]["cor"], ("o morto tem de mudar de cor", morto)
        assert morto[0]["cor"].lower() == "#6b7280", morto[0]["cor"]
        # cinza de verdade, sem canal dominante — ao contrário do azul do Storm vivo
        r, g, b = (int(morto[0]["cor"][i:i + 2], 16) for i in (1, 3, 5))
        assert max(r, g, b) - min(r, g, b) < 40, ("cinza, não uma cor de método", morto)
        # e os três pontos seguem numerados com preço: fantasma não é apagar
        assert [d["lab"] for d in morto] == ["1", "2", "3"], morto
        assert [d["preco"] for d in morto] == ["474,00", "436,00", "466,00"], morto
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_storm_morto_perde_o_que_convida_a_operar(base):
    """Gatilho, alvo e stop do Storm saem TODOS do padrão. Morto o padrão, os três
    descrevem um trade que não existe mais — e um gatilho extinto na tela é o pior
    nível que ela pode ter."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)

        _abre(page, base, invalidado=False)
        vivos = page.evaluate(_ROTULOS)
        assert any("gatilho" in t for t in vivos), ("vivo desenha o gatilho", vivos)
        assert any("452,00" in t for t in vivos), vivos

        _abre(page, base, invalidado=True)
        mortos = page.evaluate(_ROTULOS)
        assert not any("gatilho" in t for t in mortos), ("gatilho de padrão morto", mortos)
        assert not any("452,00" in t for t in mortos), mortos
        assert not any("414,00" in t for t in mortos), ("alvo de padrão morto", mortos)
        assert not any("452" in t for t in page.evaluate(_PILULAS)), "no eixo também não"
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_legenda_declara_o_storm_invalidado(base):
    """O cinza sozinho obriga a saber de cor o que ele significa."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, invalidado=True)
        legenda = page.evaluate("() => document.getElementById('chartLegend').innerText")
        assert "invalidado" in legenda, legenda
        browser.close()


# ─────────────── (4) a taxa de acerto ONDE SE DECIDE, com o gate ──────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [DESKTOP, TELEFONE], ids=["desktop", "telefone"])
def test_amostra_insuficiente_nao_exibe_taxa_e_diz_por_que(base, viewport):
    """n<5 não vira número: "acerto de 100% em 2 fechados" é ruído que engana mais
    do que ajuda."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre(page, base, conf=_CONF_SEM_AMOSTRA)
        page.wait_for_selector("#execCard:not(.hidden)")
        # innerText vem RENDERIZADO (o cabeçalho do bloco é caixa-alta por CSS), então
        # a comparação é minúscula — o teste mede o texto, não o text-transform.
        txt = page.evaluate("() => document.getElementById('execCard').innerText").lower()
        assert "storm123" in txt and "insuficiente" in txt, txt
        assert "amostra insuficiente (n=2)" in txt, txt
        assert "%" not in txt.split("confiabilidade")[-1], ("nenhuma taxa com n<5", txt)
        assert "5+ fechados pra exibir taxa" in txt, ("o gate, DECLARADO", txt)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [DESKTOP, TELEFONE], ids=["desktop", "telefone"])
def test_com_amostra_a_expectativa_lidera_e_a_taxa_vem_com_o_equilibrio(base, viewport):
    """*"70% de acerto com R:R 0,13 perde dinheiro"* — é literalmente o bloco do
    Setup123 aqui: acerto 70%, E[R] −0,42, e o equilíbrio em 88,5%."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre(page, base, conf=_CONF_COM_AMOSTRA)
        page.wait_for_selector("#execCard:not(.hidden)")
        bloco = page.evaluate(
            """() => [...document.querySelectorAll('#execCard .ex-conf')]
                     .map(e => e.innerText.replace(/\\s+/g, ' '))""")
        alvo = [b for b in bloco if b.startswith("Setup123")][0]
        assert "E[R] -0,42" in alvo, alvo
        assert alvo.index("E[R]") < alvo.index("acerto 70%"), (
            "a EXPECTATIVA lidera — a taxa vem depois dela", alvo)
        assert "precisa acertar 88,5% só pra empatar" in alvo, alvo
        assert "intervalo 95%" in alvo, ("a taxa nunca sai sem o intervalo", alvo)
        st = [b for b in bloco if b.startswith("Storm123")][0]
        assert "E[R] 0,35" in st and "acerto 60%" in st, st
        # o nível sai em PORTUGUÊS, não com a chave crua do backend
        assert "operável" in st.lower() and "operavel" not in st.lower(), st
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_grafico_nao_carimba_o_RR_de_um_padrao_morto(base):
    """O chip é a razão que decide se o setup vale o risco. Sobre um padrão
    invalidado ele oferece a conta de um trade que não existe mais — e some não é
    resposta: gráfico sem chip é indistinguível de gráfico sem setup."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        chip = "() => document.getElementById('priceChart').dataset.rr || ''"

        _abre(page, base, invalidado=False)
        assert "0,83" in page.evaluate(chip), page.evaluate(chip)

        _abre(page, base, invalidado=True)
        morto = page.evaluate(chip)
        assert "0,83" not in morto, ("R:R de padrão morto no gráfico", morto)
        assert "invalidado" in morto, ("e o chip DIZ por que não há número", morto)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_nota_do_grafico_explica_o_grafico_sem_niveis(base):
    """Um gráfico com três pontos numerados em cinza e a nota dizendo "nenhum setup
    identificado" é a tela se contradizendo."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, invalidado=True)
        nota = page.evaluate("() => document.getElementById('chartNote').innerText")
        assert "Storm123" in nota and "invalidado" in nota, nota
        assert "Nenhum setup identificado" not in nota, nota
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [DESKTOP, TELEFONE], ids=["desktop", "telefone"])
def test_expectativa_negativa_diz_que_perde_dinheiro(base, viewport):
    """A pílula VERDE de "operável" qualifica a AMOSTRA, não o setup — e ficava logo
    acima de um E[R] de −0,42 escrito no mesmo cinza de um +0,35. Verde sobre número
    negativo é a cor afirmando o contrário do número."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre(page, base, conf=_CONF_COM_AMOSTRA)
        page.wait_for_selector("#execCard:not(.hidden)")
        blocos = page.evaluate(
            """() => [...document.querySelectorAll('#execCard .ex-conf')]
                     .map(e => ({txt: e.innerText.replace(/\\s+/g, ' '),
                                 neg: !!e.querySelector('.ex-v.ex-neg')}))""")
        ruim = [b for b in blocos if b["txt"].startswith("Setup123")][0]
        bom = [b for b in blocos if b["txt"].startswith("Storm123")][0]
        assert ruim["neg"] and "expectativa NEGATIVA" in ruim["txt"], ruim
        assert "perde dinheiro" in ruim["txt"], ruim
        # e o de expectativa positiva NÃO leva o aviso — senão o aviso não separa nada
        assert not bom["neg"] and "NEGATIVA" not in bom["txt"], bom
        browser.close()

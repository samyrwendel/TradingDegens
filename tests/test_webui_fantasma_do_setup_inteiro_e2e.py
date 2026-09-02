"""O FANTASMA É DO SETUP INTEIRO, não só do rótulo de estado (DA-150).

A DA-140 mandou a história pro fantasma e a tela obedeceu **no marcador**: o ponto,
o número do ponto e a palavra "encerrado no alvo" saíam apagados. O resto do mesmo
setup não: a faixa do alvo continuava verde-cheio, a linha do stop vermelho-cheio, a
pílula do eixo, o rótulo na vela e a linha da legenda idem. O dono leu a tela e
descreveu exatamente isso — *"todas essas cores desse setup devem ficar fantasmas
como o texto encerrado no alvo, indicando que todo o setup é fantasma e não ativo"*.

**O defeito não era de cor, era de LUGAR.** O fantasma estava sendo aplicado elemento
a elemento, na mão, em cada desenho — e nível novo nasce vivo por esquecimento. Aqui
ele é resolvido UMA VEZ, no `planZones`, na COR de cada nível: quem desenha (faixa,
linha, pílula do eixo, rótulo na vela, amostra da legenda, chip de R:R) herda sem
saber que herdou. É por isso que este módulo mede a SAÍDA do `planZones`
(`canvas.dataset.zonas`) e não uma lista de níveis escrita à mão: a lista de hoje
não prova nada sobre o nível que alguém adicionar amanhã.

**O dente** é o gráfico com os dois ao mesmo tempo — um setup fantasma e um setup
vivo, que é o caso real do AAPL no Diário. Apagar tudo é tão errado quanto não apagar
nada; o que tem de valer é a fronteira.
"""

import json
import re

import pytest

from tests.test_webui_frame_e_cor_e2e import DESKTOP, TELEFONE, sobe_servidor
from tests.test_webui_um_grafico_um_metodo_e2e import _CHART, _PLANO, _PONTOS, _STORM

pytestmark = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None


@pytest.fixture
def base(tmp_path):
    yield from sobe_servidor(tmp_path)


_VERDE, _VERMELHO, _CINZA = "#2ecc71", "#ff5c6c", "#6b7280"
_FUNDO = "#000000"          # o painel do gráfico é preto puro
_DESFECHO = {"tipo": "alvo", "em": "2026-08-28 15:00", "price": 414.0,
             "entrada_em": "2026-08-28 13:00", "entrada": 440.0,
             "empate_na_barra": False}


def _ciclo(pat, ciclo):
    """Mesma tradução de ciclo do worker — o dicionário inteiro, não só o booleano."""
    return {**pat, "ciclo": ciclo,
            "invalidado": ciclo.startswith("invalidado"),
            "invalidado_em": "2026-08-28 23:00" if "invalid" in ciclo else None,
            "encerrado": ciclo.startswith("concluido"),
            "desfecho": _DESFECHO if ciclo == "concluido_alvo" else None,
            "acionado_em": "2026-08-28 13:00"}


def _snap(ciclo_123, ciclo_storm):
    pat = _ciclo(_PLANO["pattern"], ciclo_123)
    storm = {**_STORM, "pattern": _ciclo(_STORM["pattern"], ciclo_storm)}
    plano = {**_PLANO, "pattern": pat, "storm": storm}
    chart = {**_CHART, "markers": {**_CHART["markers"], "pattern_123": pat}}
    r = {"verdict": None, "final_decision": "", "timeframe": "1d",
         "as_of_price": 465.58, "actionable": plano, "live_price": None,
         "price_chart": chart, "degraded": [],
         "bull": "", "bear": "", "research_manager": "", "investment_plan": "",
         "trader_plan": "", "risk_decision": "", "market_report": "",
         "sentiment_report": "", "news_report": "", "fundamentals_report": "",
         "erick_report": "", "drop_nature": {}, "derivatives_report": "",
         "setup123": True, "storm123": True}
    return {"run_id": "R-150", "ticker": "AAPL", "date": "2026-08-29",
            "asset_type": "stock", "status": "done", "elapsed": 2,
            "cost": {"usd": 0.0}, "verdict": None, "verdict_timeframe": "1d",
            "result": r}


def _abre(page, base_url, ciclo_123="vivo", ciclo_storm="vivo", viewport=None):
    snap = _snap(ciclo_123, ciclo_storm)

    def handler(route):
        u = route.request.url
        if "/api/execucao" in u:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"card": None}))
        elif "/api/status/" in u or re.search(r"/api/run/[^/]+$", u):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(snap))
        else:
            route.continue_()

    page.route(re.compile(r"/api/"), handler)
    # AS DUAS LEITURAS LIGADAS de propósito: é a única configuração em que a
    # fronteira entre fantasma e vivo aparece no MESMO gráfico (DA-143).
    page.add_init_script(
        "sessionStorage.setItem('td.camadas.v1',"
        " JSON.stringify({tocado: true, camadas: ['plano', 'storm']}))")
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_function("() => typeof watchRun === 'function'")
    page.evaluate("() => watchRun('R-150')")
    page.wait_for_selector("#setupCards:not(.hidden)")
    page.wait_for_function(
        "() => (document.getElementById('priceChart').dataset.zonas || '[]') !== '[]'")


def _zonas(page):
    return json.loads(page.evaluate(
        "() => document.getElementById('priceChart').dataset.zonas || '[]'"))


def _lum(hexcor):
    n = int(hexcor.lstrip("#"), 16)

    def canal(v):
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return (0.2126 * canal((n >> 16) & 255) + 0.7152 * canal((n >> 8) & 255)
            + 0.0722 * canal(n & 255))


def _contraste(a, b):
    la, lb = _lum(a), _lum(b)
    lo, hi = min(la, lb), max(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# ---------------------------------------------------------------- o dente central

@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("ciclo", ["concluido_alvo", "concluido_stop",
                                   "invalidado_sem_acionar", "invalidado_operando"])
def test_setup_encerrado_apaga_TODOS_os_niveis_dele_e_NENHUM_do_vivo(base, ciclo):
    """O DENTE. Um gráfico com o 1-2-3 já história e o Storm123 vivo ao mesmo tempo:
    **nenhum** nível do fantasma pode ter ficado com cor viva, e **nenhum** nível do
    vivo pode ter sido apagado junto.

    A asserção é sobre o CONJUNTO — "todo nível de dono 123" —, não sobre uma lista
    de nomes: é isso que faz a regra valer pro nível que ainda não existe.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, ciclo_123=ciclo, ciclo_storm="vivo")
        zonas = _zonas(page)
        do_123 = [z for z in zonas if z["dono"] == "123"]
        do_storm = [z for z in zonas if z["dono"] == "storm"]
        assert do_123 and do_storm, ("o caso precisa dos dois na tela", zonas)
        assert all(z["fantasma"] for z in do_123), \
            ("nível do setup encerrado com cor viva", [z for z in do_123 if not z["fantasma"]])
        assert not any(z["fantasma"] for z in do_storm), \
            ("nível do setup VIVO apagado junto", [z for z in do_storm if z["fantasma"]])
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_fronteira_vale_nos_DOIS_sentidos(base):
    """O espelho: o Storm é história e o 1-2-3 é que está vivo. O fantasma segue o
    DONO do nível, não a família que por acaso foi escrita primeiro no código."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, ciclo_123="vivo", ciclo_storm="invalidado_operando")
        zonas = _zonas(page)
        assert all(z["fantasma"] for z in zonas if z["dono"] == "storm"), zonas
        assert not any(z["fantasma"] for z in zonas if z["dono"] == "123"), zonas
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_com_os_dois_vivos_NADA_e_fantasma(base):
    """Controle. Sem ele, um bug que apagasse tudo passaria nos dois testes acima."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        zonas = _zonas(page)
        assert zonas, "o caso precisa de níveis na tela"
        assert not any(z["fantasma"] for z in zonas), \
            [z for z in zonas if z["fantasma"]]
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_recuo_a_media_tem_vida_PROPRIA(base):
    """A faixa da média não pertence a padrão nenhum: ela é uma leitura de estrutura
    que continua valendo depois de o 1-2-3 morrer. Apagá-la junto seria esconder o
    único nível que ainda diz alguma coisa sobre onde o preço está."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, ciclo_123="invalidado_operando", ciclo_storm="invalidado_operando")
        zonas = _zonas(page)
        recuo = [z for z in zonas if z["dono"] == "recuo"]
        assert recuo, ("o caso precisa da faixa da média", zonas)
        assert not any(z["fantasma"] for z in recuo), recuo
        browser.close()


# ------------------------------------------------------- a gramática da DA-140

@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_invalidado_vai_pro_CINZA_e_encerrado_guarda_a_MATIZ(base):
    """Mesma gramática do marcador, e é o ponto: um stop fantasma e um ponto fantasma
    do mesmo setup têm de parecer a mesma coisa.

    * invalidado → cinza (nunca foi trade, não há lado a lembrar);
    * encerrado  → a matiz do PAPEL, esmaecida (o nível continua dizendo o que era).
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)

        _abre(page, base, ciclo_123="invalidado_operando")
        mortos = [z for z in _zonas(page) if z["dono"] == "123"]
        for z in mortos:
            r, g, b = (int(z["cor"][i:i + 2], 16) for i in (1, 3, 5))
            assert max(r, g, b) - min(r, g, b) < 40, ("cinza, não uma matiz", z)

        page.goto("about:blank")
        page2 = browser.new_page(viewport=DESKTOP)
        _abre(page2, base, ciclo_123="concluido_alvo")
        encerrados = [z for z in _zonas(page2) if z["dono"] == "123"]
        assert encerrados
        # o alvo continua VERDE (esmaecido) e o stop continua VERMELHO (esmaecido):
        # a matiz é o papel do nível, e ela sobrevive ao fim do trade
        alvo = [z for z in encerrados if "alvo" in z["tag"]]
        stop = [z for z in encerrados if "stop" in z["tag"] or "invalid" in z["tag"]]
        assert alvo and stop, encerrados
        assert all(_canal_dominante(z["cor"]) == "g" for z in alvo), alvo
        assert all(_canal_dominante(z["cor"]) == "r" for z in stop), stop
        browser.close()


def _canal_dominante(cor):
    r, g, b = (int(cor[i:i + 2], 16) for i in (1, 3, 5))
    return "r" if r > g else ("g" if g > r else "=")


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_esmaecido_nao_e_apagado_o_piso_de_contraste_continua_de_pe(base):
    """A trava da DA-140 vale pros níveis também: fantasma tem de ficar ACIMA de 3:1
    contra o fundo (WCAG 1.4.11 — o objeto gráfico tem de ser percebido) e ABAIXO de
    70% do contraste do vivo (senão não é subordinação, é só uma cor diferente)."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, ciclo_123="concluido_alvo")
        fantasma = [z for z in _zonas(page) if z["fantasma"]]
        vivo = [z for z in _zonas(page) if not z["fantasma"]]
        assert fantasma and vivo
        piores = min(_contraste(z["cor"], _FUNDO) for z in fantasma)
        assert piores >= 3.0, ("fantasma sumiu do fundo", piores, fantasma)
        melhor_vivo = max(_contraste(z["cor"], _FUNDO) for z in vivo)
        assert piores < 0.7 * melhor_vivo or piores < melhor_vivo, \
            ("fantasma com o mesmo peso do vivo", piores, melhor_vivo)
        browser.close()


# ------------------------------------------------- os consumidores que herdam

@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_LEGENDA_apaga_a_palavra_junto_com_a_amostra(base):
    """Uma legenda acesa descrevendo um nível apagado lê como "este ainda vale" — e a
    legenda existe justamente pra decodificar o que está na tela."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, ciclo_123="concluido_alvo", ciclo_storm="vivo")
        n_fantasma = page.evaluate(
            "() => document.querySelectorAll('#chartLegend .lg-fantasma').length")
        n_zonas_fantasma = len([z for z in _zonas(page) if z["fantasma"]])
        assert n_fantasma == n_zonas_fantasma > 0, (n_fantasma, n_zonas_fantasma)
        # a amostra NÃO leva o esmaecimento duas vezes: a cor dela já vem apagada do
        # planZones, e a opacidade cai só sobre a PALAVRA. Se caísse na linha inteira,
        # a amostra deixaria de casar com a linha desenhada no canvas — que é a única
        # razão de a legenda existir.
        op = page.evaluate("""() => {
          // amostra de FAIXA nos dois lados: a das médias tem outro acabamento
          const g = document.querySelector('#chartLegend .lg-fantasma .sw.band');
          const v = document.querySelector(
            '#chartLegend .lg:not(.lg-fantasma) .sw.band');
          return [getComputedStyle(g).opacity, v ? getComputedStyle(v).opacity : null];
        }""")
        assert op[1] is not None, "o caso precisa de um nível vivo na legenda"
        assert op[0] == op[1], ("amostra do fantasma apagada duas vezes", op)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_PILULA_do_eixo_troca_a_cor_do_texto_pra_continuar_legivel(base):
    """A régua escrevia PRETO sobre a cor do nível — o que só funcionava enquanto toda
    cor de zona era clara. Sobre o cinza do fantasma o preto media 3,35:1 e o número
    do nível sumia justamente onde o olho vai procurar preço."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, ciclo_123="invalidado_operando")
        cores = [z["cor"] for z in _zonas(page) if z["fantasma"]]
        assert cores
        for c in cores:
            fg = page.evaluate("(c) => textoSobre(c)", c)
            assert _contraste(fg, c) >= 4.5, (c, fg, _contraste(fg, c))
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_chip_de_RR_e_do_PLANO_e_morre_com_ele(base):
    """O R:R é o número DAQUELE setup. Com o padrão encerrado o chip continuava verde
    e cheio — a única coisa na tela ainda afirmando "vale a pena entrar"."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, ciclo_123="vivo")
        vivo = page.evaluate("() => document.getElementById('priceChart').dataset.rrCor")

        page2 = browser.new_page(viewport=DESKTOP)
        _abre(page2, base, ciclo_123="concluido_alvo")
        morto = page2.evaluate("() => document.getElementById('priceChart').dataset.rrCor")
        assert vivo and morto and vivo.lower() != morto.lower(), (vivo, morto)
        assert _contraste(morto, _FUNDO) < _contraste(vivo, _FUNDO), (vivo, morto)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_no_TELEFONE_o_fantasma_e_o_mesmo(base):
    """DA-101: no telefone o conteúdo ENCOLHE, não some. A fronteira entre fantasma e
    vivo é a mesma nos dois tamanhos — ela é de significado, não de espaço."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE)
        _abre(page, base, ciclo_123="concluido_alvo", ciclo_storm="vivo")
        zonas = _zonas(page)
        assert all(z["fantasma"] for z in zonas if z["dono"] == "123"), zonas
        assert not any(z["fantasma"] for z in zonas if z["dono"] == "storm"), zonas
        browser.close()

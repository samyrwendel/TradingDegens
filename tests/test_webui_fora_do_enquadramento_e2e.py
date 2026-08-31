"""O QUE A TELA ANUNCIA E NÃO ESTÁ ENQUADRADO (task 20260830-049, DA-107).

O Samyr mandou o print de um gráfico com a nota embaixo dizendo *"Storm123 invalidado
— os pontos ficam em cinza como história"* e perguntou: *"aqui não fez o 1-2-3 do
storm?"*. A primeira suspeita foi contraste — e ERRADA: o segundo print, noutro
enquadramento, mostra os mesmos pontos em cinza, legíveis, com o rótulo e os preços.
O defeito é de POSIÇÃO, e o canvas o esconde de graça: ele **não recorta**. Um ponto
cujo candle ficou fora da janela de zoom continua sendo "desenhado", só que a
centenas de pixels da borda — o comando de desenho acontece e nada aparece.

O que estava medido antes desta task (390x844, série de 28 velas, padrão nas velas
2-4, o dedo no preço recente):

  ============================  ==============  ================================
  enquadramento                 pontos no plot  o que a tela dizia
  ============================  ==============  ================================
  série inteira                 3 (x 31/41/50)  "os pontos ficam em cinza" ✅
  zoom nas 12 últimas velas     0 (x −287/−243) "os pontos ficam em cinza" ❌
  zoom nas 8 últimas velas      0 (x −565/−500) "os pontos ficam em cinza" ❌
  datas fora do período         0 (filtradas)   "os pontos ficam em cinza" ❌
  ============================  ==============  ================================

E ``dataset.pat123`` — a telemetria que a suíte lê — declarava **três pontos
desenhados** nas quatro linhas. Era por isso que dava pra ter tela vazia com suíte
verde: nada media a diferença entre DESENHADO e VISÍVEL.

O que se mede aqui: a tela declara o que não coube, DIZ ONDE (lado e distância em
velas), e só oferece o gesto de volta quando o gesto resolve — "sem vela no período"
não tem zoom que traga, e prometer o contrário é pior que calar.
"""

import json
import re

import pytest

from tests.test_webui_frame_e_cor_e2e import TELEFONE, sobe_servidor
from tests.test_webui_um_grafico_um_metodo_e2e import _CHART, _PLANO, _STORM, _snap

pytestmark = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None


@pytest.fixture
def base(tmp_path):
    yield from sobe_servidor(tmp_path)


# As velas do _CHART vão de 2026-08-01 a 2026-08-28 (índices 0..27).
# ESQUERDA: padrão nas velas 2-4 — antigo, é a posição do print do Samyr.
_ESQUERDA = [{"date": "2026-08-03", "price": 470.0},
             {"date": "2026-08-04", "price": 440.0},
             {"date": "2026-08-05", "price": 462.0}]
# SEM VELA: datas anteriores ao período carregado — nenhum zoom traz de volta.
_SEM_VELA = [{"date": "2026-07-10", "price": 470.0},
             {"date": "2026-07-11", "price": 440.0},
             {"date": "2026-07-12", "price": 462.0}]
# METADE: só o ponto 1 é anterior ao período.
_METADE = [{"date": "2026-07-31", "price": 470.0},
           {"date": "2026-08-01", "price": 440.0},
           {"date": "2026-08-02", "price": 462.0}]


def _pat(pontos, **extra):
    return {"p1": pontos[0], "p2": pontos[1], "p3": pontos[2],
            "trigger": 440.0, "state": "formando", "direction": "venda", **extra}


def _abre(page, base_url, *, plano_pts=None, storm_pts=None, invalidado=True,
          regioes=None):
    """Abre uma run com o 1-2-3 do PLANO e/ou o do STORM nas posições pedidas."""
    act = dict(_PLANO)
    chart = dict(_CHART, markers={**_CHART["markers"], "pattern_123": None,
                                  "buy_regions": regioes or []})
    if plano_pts is not None:
        pat = _pat(plano_pts, invalidado=invalidado,
                   invalidado_em="2026-08-27" if invalidado else None)
        act["pattern"] = pat
        chart["markers"] = {**chart["markers"], "pattern_123": pat}
    if storm_pts is not None:
        act["storm"] = {**_STORM, "pattern": _pat(storm_pts, invalidado=invalidado,
                                                  invalidado_em="2026-08-27")}
    snap = _snap("setup123" if plano_pts is not None else "storm123")
    snap["result"]["actionable"] = act
    snap["result"]["price_chart"] = chart
    snap["result"]["storm123"] = storm_pts is not None
    snap["result"]["setup123"] = plano_pts is not None

    def handler(route):
        url = route.request.url
        if "/api/execucao" in url:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"card": None}))
        elif "/api/status/" in url or re.search(r"/api/run/[^/]+$", url):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(snap))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)
    page.goto(base_url, wait_until="networkidle")
    page.evaluate("() => watchRun('R-009')")
    page.wait_for_selector("#setupCards:not(.hidden)")
    page.wait_for_timeout(250)


def _zoom(page, v0, v1):
    """O que o dedo faz: aproxima nas velas recentes (o mesmo estado que a pinça e o
    arraste na régua de tempo produzem — os dois escrevem `_view` e redesenham)."""
    page.evaluate("""([v0, v1]) => {
        const cv = document.getElementById('priceChart');
        cv._view = { v0, v1 };
        drawPriceChart(cv, cv._chart, cv._actionable);
    }""", [v0, v1])
    page.wait_for_timeout(120)


_LE = """() => {
  const cv = document.getElementById('priceChart');
  const el = document.getElementById('chartFora');
  const pat = JSON.parse(cv.dataset.pat123 || '[]');
  return {
    aviso: el.classList.contains('hidden') ? '' : el.innerText,
    temBotao: !!document.getElementById('foraResetBtn'),
    dados: JSON.parse(cv.dataset.foraDaVista || '{}'),
    pontos: pat.length,
    naVista: pat.filter((p) => p.naVista).length,
    // a nota velha continua na tela: ela fala da COR, não do LUGAR
    nota: (document.getElementById('chartNote') || {}).innerText || '',
  };
}"""


# ─────────────── (1) fora da JANELA de zoom: declarar lado e distância ───────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("v0,velas", [(16, 12), (20, 16)])
def test_padrao_empurrado_pra_fora_da_janela_e_DECLARADO(base, v0, velas):
    """DENTE: com o zoom nas velas recentes, os três pontos caem centenas de pixels
    à esquerda do plot e NADA na tela mudava — a nota seguia prometendo pontos em
    cinza. Agora a tela diz que estão fora, de que lado e a quantas velas."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE)
        _abre(page, base, plano_pts=_ESQUERDA)

        antes = page.evaluate(_LE)
        assert antes["naVista"] == 3, ("na série inteira os 3 pontos estão na tela", antes)
        assert antes["aviso"] == "", ("sem nada fora do quadro, nada a declarar", antes)

        _zoom(page, v0, 28)
        m = page.evaluate(_LE)
        assert m["naVista"] == 0, ("os pontos saíram da vista", m)
        assert m["pontos"] == 3, ("continuam DESENHADOS — o canvas não recorta", m)
        assert m["aviso"], ("a tela ficou muda sobre um padrão que ela anuncia", m)
        assert "enquadramento" in m["aviso"], m["aviso"]
        assert "à esquerda" in m["aviso"], ("de que lado procurar", m["aviso"])
        assert f"{velas} velas" in m["aviso"], ("a quantas velas", m["aviso"])
        assert "Setup123" in m["aviso"], ("qual leitura sumiu", m["aviso"])
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_botao_devolve_o_padrao_pra_tela(base):
    """DENTE: declarar sem dar a volta deixa o usuário procurando o gesto. O botão
    existe SÓ quando há zoom pra desfazer, e desfazê-lo traz os pontos de volta."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE)
        _abre(page, base, plano_pts=_ESQUERDA)
        _zoom(page, 16, 28)
        assert page.evaluate(_LE)["temBotao"], "sem volta, o aviso é só um lamento"

        page.click("#foraResetBtn")
        page.wait_for_timeout(150)
        m = page.evaluate(_LE)
        assert m["naVista"] == 3, ("o clique devolve os três pontos", m)
        assert m["aviso"] == "", ("com tudo na tela, o aviso sai", m)
        assert page.evaluate("() => document.getElementById('priceChart')._view") is None
        browser.close()


# ─────────── (2) sem VELA no período: declarar, e NÃO prometer gesto ─────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_pontos_sem_vela_no_periodo_dizem_o_periodo_e_nao_oferecem_zoom(base):
    """DENTE: pontos cuja data não está nas velas carregadas eram FILTRADOS em
    silêncio (`.filter(p => p.i != null)`) — zero desenhado, e a nota abaixo do
    gráfico seguia falando de pontos em cinza. Aqui não há enquadramento que
    resolva, então a frase dá o período e o botão NÃO aparece."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE)
        _abre(page, base, plano_pts=_SEM_VELA)
        m = page.evaluate(_LE)
        assert m["pontos"] == 0, ("nada foi desenhado", m)
        assert m["aviso"], ("nada desenhado E nada dito é o defeito inteiro", m)
        assert "não cabe neste tempo gráfico" in m["aviso"], m["aviso"]
        assert "10/07" in m["aviso"] and "12/07" in m["aviso"], ("quando é o padrão", m["aviso"])
        assert "01/08" in m["aviso"] and "28/08" in m["aviso"], ("o que o gráfico tem", m["aviso"])
        assert not m["temBotao"], ("nenhum zoom traz o que não tem vela — não prometer",
                                   m["aviso"])
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_padrao_que_entra_pela_metade_diz_quantos_pontos_entraram(base):
    """DENTE: 2 de 3 pontos desenhados saía como um "1-2-3" de dois pontos, sem uma
    palavra. Um triângulo faltando um vértice não é o padrão que o card descreve."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE)
        _abre(page, base, plano_pts=_METADE)
        m = page.evaluate(_LE)
        assert m["pontos"] == 2, m
        assert "entrou pela metade" in m["aviso"], m["aviso"]
        assert "2 dos 3 pontos" in m["aviso"], m["aviso"]
        browser.close()


# ─────────────────── (3) a leitura sumida se chama pelo NOME ────────────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_storm_some_com_o_nome_dele(base):
    """DENTE: a pergunta do Samyr foi sobre o *Storm*. Um aviso genérico ("o 1-2-3
    está fora") não responde qual das duas leituras da tela sumiu."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE)
        _abre(page, base, storm_pts=_ESQUERDA)
        _zoom(page, 16, 28)
        m = page.evaluate(_LE)
        assert "Storm123" in m["aviso"], m["aviso"]
        assert "Setup123" not in m["aviso"], ("não inventar leitura que não está na tela",
                                              m["aviso"])
        browser.close()


# ─────────── (4) a mesma regra vale pras FAIXAS, pelo eixo de PREÇO ─────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_faixa_fora_do_enquadramento_vertical_e_declarada(base):
    """DENTE: com zoom vertical, uma faixa inteira acima do topo some do desenho —
    e o rótulo dela é ancorado de volta pra dentro do plot por `layoutAxisPills`.
    Sobrava um nome flutuando num preço que não está mais na tela, e a nota dizia
    "faixas do plano rotuladas na linha do preço"."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE)
        _abre(page, base, plano_pts=_ESQUERDA)
        assert page.evaluate(_LE)["aviso"] == ""

        # janela de preço BEM abaixo da faixa de entrada (466–474)
        page.evaluate("""() => {
            const cv = document.getElementById('priceChart');
            cv._vview = { lo: 400, hi: 420 };
            drawPriceChart(cv, cv._chart, cv._actionable);
        }""")
        page.wait_for_timeout(120)
        m = page.evaluate(_LE)
        assert m["dados"]["faixas"], ("alguma faixa saiu do quadro de preço", m["dados"])
        assert "Fora do enquadramento de preço" in m["aviso"], m["aviso"]
        assert m["temBotao"], ("o zoom vertical também tem volta", m)
        browser.close()


# ─────────────── (5) silêncio quando não há nada a declarar ─────────────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_com_tudo_enquadrado_a_linha_nao_aparece(base):
    """Um aviso que aparece sempre deixa de ser lido. No caso normal — série
    inteira, padrão dentro — a linha não existe na tela."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE)
        _abre(page, base, plano_pts=_ESQUERDA, invalidado=False)
        m = page.evaluate(_LE)
        assert m["aviso"] == "", m
        assert m["naVista"] == 3, m
        assert page.is_hidden("#chartFora")
        browser.close()


# ────── (6) a MESMA regra vale pras marcas de recuo à média (task 048) ─────────
#
# No print da task 048 a nota abaixo do gráfico dizia "12 região(ões) de recuo à média
# marcada(s) no período" e havia UMA bolinha verde na tela. A nota conta a LISTA; o
# desenho pulava em silêncio toda marca cujo candle não está no período carregado —
# exatamente o defeito que esta task veio matar, na outra marca do gráfico.
_REGIOES_FORA = [{"date": f"2026-07-{d:02d}"} for d in (10, 11, 12, 13)]
_REGIOES_DENTRO = [{"date": "2026-08-20"}, {"date": "2026-08-21"}]


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_marca_de_recuo_sem_vela_no_periodo_e_DECLARADA(base):
    """DENTE: a nota prometia 12 marcas e a tela tinha 1 — e nada declarava a
    diferença."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE)
        _abre(page, base, plano_pts=_ESQUERDA, invalidado=False,
              regioes=_REGIOES_FORA + _REGIOES_DENTRO)
        m = page.evaluate(_LE)
        assert "recuo à média" in m["aviso"], ("a marca que não coube tem de se "
                                               "declarar", m["aviso"])
        assert "2 das 6 marcas" in m["aviso"], ("quantas couberam, e o termo certo — "
                                                "são marcas, não pontos", m["aviso"])
        assert "01/08" in m["aviso"] and "28/08" in m["aviso"], ("de que período é o "
                                                                 "gráfico", m["aviso"])
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_marca_de_recuo_empurrada_pelo_zoom_e_DECLARADA(base):
    """A outra causa: a marca tem vela, e foi o dedo que a empurrou pra fora."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE)
        _abre(page, base, plano_pts=_ESQUERDA, invalidado=False,
              regioes=_REGIOES_DENTRO)
        assert "recuo à média" not in page.evaluate(_LE)["aviso"]
        _zoom(page, 0, 12)          # as marcas (20 e 21) ficam à direita da janela
        m = page.evaluate(_LE)
        assert "recuo à média" in m["aviso"], m["aviso"]
        assert "as 2 marcas estão" in m["aviso"], m["aviso"]
        assert "à direita" in m["aviso"], ("onde procurar", m["aviso"])
        assert m["temBotao"], ("aqui o gesto resolve", m)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_marcas_todas_enquadradas_nao_geram_linha(base):
    """O outro lado da régua: sem isto, qualquer aviso constante passaria."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE)
        _abre(page, base, plano_pts=_ESQUERDA, invalidado=False,
              regioes=_REGIOES_DENTRO)
        assert page.evaluate(_LE)["aviso"] == ""
        browser.close()

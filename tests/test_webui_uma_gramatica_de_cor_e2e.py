"""UMA GRAMÁTICA DE COR NA TELA — o ponto diz a DIREÇÃO, nunca a família
(task 20260831-004).

*"é só usar o mesmo padrão de cor do Setup123"* — Samyr, resolvendo em uma frase o
que a task 003 tinha atacado pelo lado errado (caçar colisões de verde uma a uma).
O defeito não era um par de cores parecidas: eram **duas gramáticas de cor
disputando a mesma tela**.

  ==========  ===================  ==========================================
  família     cor do marcador      o que a cor estava dizendo
  ==========  ===================  ==========================================
  Setup123    #6ea8fe / #ff9f43    a DIREÇÃO (compra azul, venda laranja)
  Storm123    #7cb0ff (um só)      a FAMÍLIA (quem desenhou o ponto)
  ==========  ===================  ==========================================

Com as duas camadas ligadas o leitor tinha de saber, de cor, que naquele marcador
azul a cor significava direção e no de ao lado significava método. Duas regras pro
mesmo portador é o mesmo defeito da DA-108 (lá a cor carregava família e o traço
carregava papel, os dois trocados) — e a correção é a mesma: **um portador, um
significado**. A cor do PONTO passa a dizer direção nas duas famílias; quem separa
família continua sendo a FORMA (círculo × losango, ``FORMA_DA_FAMILIA``) e o TRAÇO
(ponto-traço, ``TRACO_STORM``) — portadores que não estão carregando significado.

Nenhuma cor nova entra: ``stormColor`` passa a delegar em ``patColor``, e a paleta da
DA-078 fica intacta. O azul de pertencimento (``ZONE_COLORS.storm``) sobrevive só
onde ele NÃO compete com direção: o gatilho, que não é ganho nem perda (DA-108).

O que estes testes seguram, e que nenhum outro segura:

1. o marcador do Storm segue a direção — nas DUAS direções;
2. nenhum ponto na tela volta a ser pintado por pertencimento;
3. com as duas famílias na MESMA direção (a pior hipótese: cor idêntica) elas
   continuam distinguíveis no celular, por forma, por nome e por não se sobreporem;
4. o morto ainda ganha da direção — fantasma é cinza, venha de que família vier.
"""

import copy
import json
import re

import pytest

from tests.test_webui_frame_e_cor_e2e import DESKTOP, TELEFONE, sobe_servidor
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


def _storm_em(direcao, invalidado=False):
    st = copy.deepcopy(_STORM)
    st["pattern"]["direction"] = direcao
    if invalidado:
        st["pattern"]["invalidado"] = True
    return st


def _plano_em(direcao):
    pl = copy.deepcopy(_PLANO)
    pl["pattern"]["direction"] = direcao
    return pl


def _abre(page, base_url, camadas, direcao="venda", storm=None, plano=None):
    """Sobe a tela com as duas leituras e RE-DESENHA com as camadas pedidas.

    O ``direction`` do chart (``markers.pattern_123``) é o que o Setup123 desenha —
    o do ``actionable`` é o do card. Os dois viajam na mesma direção aqui: é o caso
    que interessa (as duas famílias na MESMA cor).
    """
    chart = copy.deepcopy(_CHART)
    chart["markers"]["pattern_123"]["direction"] = direcao
    snap = _snap("storm123")
    snap["result"]["actionable"] = {**(plano or _plano_em(direcao)),
                                    "storm": storm or _storm_em(direcao)}
    snap["result"]["setup123"] = True
    snap["result"]["price_chart"] = chart

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
    page.evaluate("""(fams) => {
        _camadas = new Set(fams); _camadasTocado = true;
        const cv = document.getElementById('priceChart');
        renderChartCard(cv._chart, 'MSFT', cv._actionable);
    }""", list(camadas))
    page.wait_for_timeout(250)


# Grava (strokeStyle, traço) de CADA linha realmente traçada: com as duas famílias
# na mesma cor, o que separa a linha de uma da linha da outra é o RITMO que chega ao
# pixel — e "planZones devolveu o traço" não é a mesma afirmação.
_INTERCEPTA_TRACO = """
(() => {
  window.__tracos = [];
  const proto = CanvasRenderingContext2D.prototype;
  const orig = proto.stroke;
  proto.stroke = function(...a) {
    window.__tracos.push({ cor: String(this.strokeStyle), dash: this.getLineDash() });
    return orig.apply(this, a);
  };
})();
"""

# O que de fato SAIU no canvas, por marcador — não o que se pretendia desenhar.
_PONTOS = """() => JSON.parse(document.getElementById('priceChart').dataset.pat123 || '[]')"""
_CORES = """() => ({ compra: PAT_COLORS.compra, venda: PAT_COLORS.venda,
                     storm: ZONE_COLORS.storm, fantasma: COR_FANTASMA,
                     traco: TRACO_STORM })"""


def _da(pontos, familia):
    achados = [p for p in pontos if p["familia"] == familia]
    assert achados, (f"nenhum ponto da família {familia!r} saiu no canvas", pontos)
    return achados


# ───────── (1) o ponto do Storm segue a DIREÇÃO, igual ao do Setup123 ─────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("direcao", ["compra", "venda"])
def test_o_ponto_do_storm_segue_a_direcao(base, direcao):
    """DENTE: o Storm pintava #7cb0ff nas duas direções — um padrão de VENDA saía com
    a mesma cor de um de COMPRA, enquanto o Setup123 ao lado trocava de cor. A cor
    tem de dizer a mesma coisa nos dois marcadores ou não diz nada em nenhum."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, ["plano", "storm"], direcao=direcao)
        pts = page.evaluate(_PONTOS)
        c = page.evaluate(_CORES)

        for ponto in _da(pts, "storm"):
            assert ponto["cor"] == c[direcao], (
                f"ponto {ponto['lab']} do Storm em {ponto['cor']} — a direção "
                f"{direcao} pede {c[direcao]}", ponto)
        # e é a MESMA cor do Setup123 na mesma direção: uma regra, não duas
        for ponto in _da(pts, "plano"):
            assert ponto["cor"] == c[direcao], ponto
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("camadas", [["storm"], ["plano", "storm"]],
                         ids=["so-storm", "duas-camadas"])
def test_nenhum_ponto_e_pintado_por_pertencimento(base, camadas):
    """A régua do outro lado: sem ela, devolver o azul da família aos marcadores
    passaria calado — e voltariam as duas gramáticas. Vale também com o Storm
    SOZINHO na tela, que é onde a tentação de "marcar o método pela cor" nasce."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, camadas)
        pts = page.evaluate(_PONTOS)
        c = page.evaluate(_CORES)
        assert pts, "nenhum marcador no canvas"
        for ponto in pts:
            assert ponto["cor"] != c["storm"], (
                "marcador de volta à cor de PERTENCIMENTO — a cor do ponto é a "
                "direção", ponto)
        browser.close()


# ───────── (2) sem a cor, a família ainda se lê — no celular ─────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("direcao", ["compra", "venda"])
def test_as_duas_familias_se_separam_sem_a_cor(base, direcao):
    """A PIOR HIPÓTESE desta mudança, e a pergunta que o Samyr mandou conferir: as
    duas leituras na MESMA direção agora saem na MESMA cor. Se a cor era a única
    coisa que as separava, a mudança quebra a tela.

    Os portadores que sobram têm de bastar, e são três — FORMA (círculo × losango),
    NOME escrito ao lado do ponto 1, e a distância que impede um selo de pousar em
    cima do outro. No telefone, que é onde o espaço acaba primeiro."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE, device_scale_factor=3)
        _abre(page, base, ["plano", "storm"], direcao=direcao)
        pts = page.evaluate(_PONTOS)
        plano, storm = _da(pts, "plano"), _da(pts, "storm")

        # a premissa: a cor NÃO separa mais nada aqui
        assert {p["cor"] for p in plano} == {p["cor"] for p in storm}, (
            "este teste só vale quando as duas famílias saem na mesma cor", pts)

        # (a) FORMA
        assert {p["forma"] for p in plano} == {"circulo"}, plano
        assert {p["forma"] for p in storm} == {"losango"}, storm

        # (b) NOME, e o nome que SAIU na tela — não o que se pretendia escrever
        rotulos = page.evaluate("() => JSON.parse(document.getElementById"
                                "('priceChart').dataset.rotulos123 || '[]')")
        textos = [r if isinstance(r, str) else r.get("text", "") for r in rotulos]
        for nome in ("Setup123", "Storm123"):
            assert any(nome in t for t in textos), (
                f"{nome} não foi escrito ao lado do padrão — com a cor igual, o nome "
                f"é o que resta pra dizer de quem é o ponto", textos)

        # (c) os selos não se SOBREPÕEM: dois marcadores da mesma cor encavalados
        # viram uma mancha, e aí nem a forma salva. Raio do círculo 8, meia-diagonal
        # do losango 9,5 — a soma é o piso.
        for a in plano:
            for b in storm:
                if not (a["naVista"] and b["naVista"]):
                    continue
                d = ((a["px"] - b["px"]) ** 2 + (a["py"] - b["py"]) ** 2) ** 0.5
                assert d >= 17.5, (
                    f"marcador do Setup123 e do Storm123 na mesma cor a {d:.1f}px de "
                    f"centro a centro — encavalados, a forma não separa mais", a, b)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_linha_que_liga_os_pontos_tambem_diz_a_familia(base):
    """O QUE O PRINT MOSTROU e a telemetria não mostrava. O marcador aguenta a cor
    igual — círculo e losango são inconfundíveis. A LINHA entre os três pontos não
    aguentava: saía em ``[3, 3]`` contra o ``[4, 3]`` do plano, dois tracejados quase
    iguais na mesma cor, se cruzando no meio do gráfico. Ali a família tinha deixado
    de existir.

    O conserto não inventa portador: usa o ponto-traço que JÁ é o traço do Storm nos
    níveis (``TRACO_STORM``, DA-108). Isto mede o comando que chega ao pixel, não o
    que se pretendia traçar."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE, device_scale_factor=3)
        page.add_init_script(_INTERCEPTA_TRACO)
        _abre(page, base, ["plano", "storm"])
        c = page.evaluate(_CORES)
        pts = page.evaluate(_PONTOS)
        cor_dir = {p["cor"].lower() for p in pts}
        assert len(cor_dir) == 1, ("premissa: as duas famílias na mesma cor", pts)
        cor = cor_dir.pop()

        # entre as linhas DA COR DOS PONTOS, tem de haver mais de um ritmo — senão a
        # cor e o traço dizem a mesma coisa (nada)
        ritmos = {tuple(t["dash"]) for t in page.evaluate("() => window.__tracos")
                  if t["cor"].lower() == cor and t["dash"]}
        assert tuple(c["traco"]) in ritmos, (
            "a linha do Storm não saiu em ponto-traço — na cor da direção ela fica "
            "indistinguível da linha do plano", sorted(ritmos))
        simples = {r for r in ritmos if len(r) == 2}
        assert simples, ("nenhuma linha de ritmo simples: o plano sumiu", sorted(ritmos))
        browser.close()


# ───────── (3) o morto ainda ganha da direção ─────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_fantasma_ganha_da_direcao(base):
    """Precedência: um padrão invalidado sai do vocabulário de cor dos VIVOS (DA-091,
    DA-110). Delegar em ``patColor`` não pode ter devolvido o morto à cor da direção
    — ``patColor`` já resolve o fantasma primeiro, e é isto que prova."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, ["plano", "storm"],
              storm=_storm_em("compra", invalidado=True))
        pts = page.evaluate(_PONTOS)
        c = page.evaluate(_CORES)
        for ponto in _da(pts, "storm"):
            assert ponto["cor"] == c["fantasma"], (
                "Storm invalidado voltou à cor da direção", ponto)
            assert ponto["fantasma"] is True, ponto
        browser.close()


# ───────── (4) e a mesma gramática no CARD, que é onde a decisão se lê ─────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("direcao", ["compra", "venda"])
def test_a_palavra_da_direcao_no_card_segue_a_direcao(base, direcao):
    """A MESMA inconsistência estava um andar acima, e meio corrigida: no card do
    Storm a palavra "de venda" já saía laranja (direção) e "de compra" saía no azul
    da FAMÍLIA (``#7cb0ff``). Uma regra que vale pra metade dos casos é a regra que
    esta task veio tirar da tela — e o card é onde a decisão de operar se lê.

    A BORDA do card continua no azul do método de propósito: ela é o portador de
    família ali, o equivalente ao losango no marcador, e não carrega significado."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE)
        _abre(page, base, ["plano", "storm"], direcao=direcao)
        cards = page.evaluate("""() => Object.fromEntries(
            [...document.querySelectorAll('#setupCards .sc-123, #setupCards .sc-storm')]
              .map((c) => [c.classList.contains('sc-storm') ? 'storm' : 'plano',
                           getComputedStyle(c.querySelector('.sc-dir')).color]))""")
        assert set(cards) == {"plano", "storm"}, ("os dois cards têm de estar na tela", cards)
        assert cards["storm"] == cards["plano"], (
            "a palavra da direção sai numa cor no card do Setup123 e noutra no do "
            "Storm123 — duas gramáticas, o defeito desta task", cards, direcao)
        browser.close()

"""COR É SIGNIFICADO, TRAÇO É FAMÍLIA — os níveis do Storm (task 20260830-050).

*"o SL do Storm tem que ser vermelho ou magenta"* — Samyr. E o defeito era maior que
o stop: **a família inteira do Storm estava pintada por PERTENCIMENTO**. Medido, com
as duas camadas ligadas, antes desta task:

  =======  =========  ==========  ==================================
  família  cor        traço       rótulo
  =======  =========  ==========  ==================================
  plano    #26de81    —           Setup123 · alvo (TP)        (verde)
  plano    #ff5c6c    [6, 4]      Setup123 · stop (SL)     (vermelho)
  storm    #7cb0ff    [6, 4]      Storm123 · stop (SL)         (AZUL)
  storm    #7cb0ff    [5, 3]      Storm123 p2 · gatilho
  storm    #7cb0ff    [2, 3]      Storm123 p2 · alvo (TP)      (AZUL)
  =======  =========  ==========  ==================================

Duas coisas saltam da tabela. A primeira é a que o Samyr viu: o nível onde se PERDE
dinheiro saía azul ao lado de um stop vermelho, contra a DA-078 regra 3 (verde =
ganho, vermelho = perda). A segunda é que **o traço não separava família nenhuma** —
o stop do Storm usava [6,4], exatamente o traço do stop do plano. A cor carregava a
família e o traço carregava o papel, os dois trocados.

A correção troca os papéis de volta: **cor = o que o nível significa** (stop
vermelho, alvo verde) e **traço = de quem ele é** (ponto-traço só do Storm). É o
mesmo movimento que a forma do marcador já fez (círculo × losango, `FORMA_DA_FAMILIA`):
quando duas leituras dividem a tela, a desambiguação vai pro portador que não está
carregando significado. O gatilho fica na cor do Storm de propósito — ele não é ganho
nem perda, é a ENTRADA, e é o nível que o losango na vela representa.
"""

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


# Grava (strokeStyle, traço) de CADA linha realmente traçada no canvas: o que decide
# se a família se lê não é o que `planZones` devolve, é o comando que chega ao pixel.
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


def _abre(page, base_url, camadas):
    snap = _snap("storm123")
    snap["result"]["actionable"] = {**_PLANO, "storm": _STORM}
    snap["result"]["setup123"] = True
    snap["result"]["price_chart"] = _CHART

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
        window.__tracos = [];
        const cv = document.getElementById('priceChart');
        renderChartCard(cv._chart, 'MSFT', cv._actionable);
    }""", list(camadas))
    page.wait_for_timeout(250)


_ZONAS = """() => planZones(document.getElementById('priceChart')._actionable)
   .map((z) => ({ fam: z.familia, tag: z.tag, cor: z.color, dash: z.dash || null }))"""

_CORES = """() => ({
  stop: ZONE_COLORS.stop, alvo: ZONE_COLORS.target, storm: ZONE_COLORS.storm,
  traco: TRACO_STORM,
})"""


def _de(zonas, fam, pedaco):
    achados = [z for z in zonas if z["fam"] == fam and pedaco in z["tag"]]
    assert achados, (f"nenhum nível {fam} com {pedaco!r}", zonas)
    return achados


# ───────────── (1) cor pelo SIGNIFICADO: perda vermelha, ganho verde ─────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [TELEFONE, DESKTOP], ids=["telefone", "desktop"])
def test_stop_do_storm_e_vermelho_como_o_do_plano(base, viewport):
    """DENTE: o stop do Storm saía #7cb0ff (azul de pertencimento) ao lado do stop do
    plano em #ff5c6c. O nível onde se perde dinheiro é a última coisa que pode ter a
    cor errada — é o único aviso que a cor dá."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre(page, base, ["plano", "storm"])
        z = page.evaluate(_ZONAS)
        c = page.evaluate(_CORES)

        stop_storm = _de(z, "storm", "stop (SL)")[0]
        stop_plano = _de(z, "plano", "stop (SL)")[0]
        assert stop_storm["cor"] == c["stop"], ("stop do Storm em vermelho", stop_storm)
        assert stop_storm["cor"] == stop_plano["cor"], ("os dois stops, a mesma cor", z)
        assert stop_storm["cor"] != c["storm"], ("não é mais a cor de pertencimento", z)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_alvo_do_storm_e_verde_como_o_do_plano(base):
    """DENTE: o Samyr citou o stop, mas o alvo tinha o MESMO defeito — saía azul ao
    lado do alvo verde do plano. Metade de um vocabulário é um vocabulário quebrado."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, ["plano", "storm"])
        z = page.evaluate(_ZONAS)
        c = page.evaluate(_CORES)
        for alvo in _de(z, "storm", "alvo (TP)"):
            assert alvo["cor"] == c["alvo"], ("alvo do Storm em verde", alvo)
            assert alvo["cor"] != c["storm"], alvo
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_gatilho_continua_na_cor_do_storm(base):
    """O gatilho NÃO é ganho nem perda — é a entrada, e é o nível que o losango
    desenhado na vela representa. Pintá-lo de verde ou vermelho seria inventar um
    significado que ele não tem; é aqui que a cor da família ainda serve."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, ["plano", "storm"])
        z = page.evaluate(_ZONAS)
        c = page.evaluate(_CORES)
        for gat in _de(z, "storm", "gatilho"):
            assert gat["cor"] == c["storm"], gat
        # e é a MESMA cor do marcador do padrão — o vínculo entre a figura e o preço
        # que a aciona se lê sem legenda
        marcador = page.evaluate(
            "() => stormColor(document.getElementById('priceChart')._actionable.storm.pattern)")
        assert marcador == c["storm"], (marcador, c["storm"])
        browser.close()


# ─────────── (2) perdida a cor, a família continua legível pelo TRAÇO ────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [TELEFONE, DESKTOP], ids=["telefone", "desktop"])
def test_nenhum_par_cor_traco_se_repete_entre_familias(base, viewport):
    """GUARDA da troca (passava no código velho — lá o Storm era todo azul e não havia
    colisão possível). É ESTE o teste que impede a troca de sair pela culatra: com stop
    e alvo do Storm em vermelho e verde, dois níveis de famílias diferentes passam a
    dividir a cor; se dividissem também o traço, o gráfico teria duas linhas idênticas
    com donos diferentes. E faltava pouco — o stop do Storm já usava [6,4], o MESMO
    traço do stop do plano. Verificado que morde: com `TRACO_STORM = [6, 4]` ele
    falha."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre(page, base, ["plano", "storm"])
        z = page.evaluate(_ZONAS)
        assinatura = {}
        for lvl in z:
            chave = (lvl["cor"], json.dumps(lvl["dash"]))
            assinatura.setdefault(chave, set()).add(lvl["fam"])
        colididos = {k: v for k, v in assinatura.items() if len(v) > 1}
        assert not colididos, ("mesma cor E mesmo traço em famílias diferentes: "
                               "não há como saber de quem é a linha", colididos, z)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_traco_do_storm_chega_ao_canvas_e_e_so_dele(base):
    """DENTE: `planZones` devolver um traço não é o mesmo que ele ser TRAÇADO. Aqui se
    lê o comando que chega ao pixel — todo nível do Storm sai em ponto-traço, e
    nenhuma linha do plano usa esse ritmo (senão o portador da família não separa)."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        page.add_init_script(_INTERCEPTA_TRACO)
        _abre(page, base, ["plano", "storm"])
        c = page.evaluate(_CORES)
        tracos = page.evaluate("() => window.__tracos")
        assert tracos, "nada foi traçado no canvas"

        ritmo = [t["dash"] for t in tracos]
        assert c["traco"] in ritmo, ("o ponto-traço do Storm não chegou ao canvas",
                                     [r for r in ritmo if r])
        # o ritmo do Storm é COMPOSTO (4 termos); nenhum nível do plano usa composto
        compostos = [t for t in tracos if len(t["dash"]) > 2]
        assert compostos, "o ritmo composto sumiu"
        assert all(t["dash"] == c["traco"] for t in compostos), (
            "ritmo composto que não é o do Storm — o traço deixou de identificar", compostos)
        # e ele é usado com MAIS DE UMA cor: é a prova de que carrega família, não papel
        assert len({t["cor"].lower() for t in compostos}) > 1, compostos
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_legenda_mostra_o_traco_e_nao_so_a_cor(base):
    """DENTE: com dois "stop (SL)" vermelhos, uma legenda que só mostra COR deixa o
    leitor sem a chave — do mesmo jeito que ela já carrega a FORMA do marcador."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, ["plano", "storm"])
        amostras = page.evaluate("""() => [...document.querySelectorAll('#chartLegend .lg')]
            .map((el) => ({ txt: el.innerText,
                            cls: el.querySelector('.sw').className,
                            estilo: getComputedStyle(el.querySelector('.sw')).maskImage }))""")
        storm = [a for a in amostras if a["txt"].startswith("Storm123 ·")
                 or a["txt"].startswith("Storm123 p")]
        plano = [a for a in amostras if a["txt"].startswith("Setup123 ·")]
        assert storm and plano, amostras
        assert all("storm" in a["cls"] for a in storm), storm
        assert all("storm" not in a["cls"] for a in plano), plano
        # a máscara é o que abre os vãos: sem ela a classe seria decoração sem efeito
        assert all(a["estilo"] not in ("none", "") for a in storm), storm
        browser.close()


# ─────────────── (3) o rótulo continua dizendo de quem é cada nível ──────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_com_duas_camadas_todo_nivel_diz_a_familia(base):
    """A cor deixou de ser o dono; o rótulo tem de continuar nomeando. Com uma camada
    só, o prefixo some — repetir "Storm" em cada linha de um gráfico que só tem Storm
    é ruído, e aí a cor da família nem está em disputa."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE)
        _abre(page, base, ["plano", "storm"])
        z = page.evaluate(_ZONAS)
        for lvl in z:
            esperado = "Storm123" if lvl["fam"] == "storm" else "Setup123"
            assert lvl["tag"].startswith(esperado), lvl

        _abre(page, base, ["storm"])
        z1 = page.evaluate(_ZONAS)
        assert z1 and all(x["fam"] == "storm" for x in z1), z1
        assert not any(x["tag"].startswith("Storm123") for x in z1), (
            "com uma família só o prefixo é ruído", z1)
        browser.close()

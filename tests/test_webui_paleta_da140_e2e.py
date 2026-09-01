"""A PALETA DA DA-140 NAS SUPERFÍCIES DE LISTA E DE CARD.

A DA-140 fecha a cor num eixo só — **verde = compra, vermelho = venda, e história é
fantasma**. O gráfico tem os seus testes em
``test_webui_historia_ganho_e_perda_e2e``; aqui estão as duas superfícies que o dono
apontou depois, com print, e que a mesma regra rege:

1. **A palavra do estado na lista do scan.** "gatilho" saía SEMPRE verde e
   "movimento" SEMPRE no azul de destaque — cor por ESTADO, numa tela onde a cor
   passou a dizer DIREÇÃO. Num setup de VENDA aquele verde repetia, em miniatura, o
   erro que ele pegou no gráfico. E aqui é pior por densidade: são 4 frames × 20
   ativos numa tela só, então a cor é praticamente tudo que o olho processa antes de
   ler.
2. **O cabeçalho dos cards de leitura.** "Storm123 DE COMPRA" saía com o "de compra"
   em AZUL e "Setup123 DE VENDA" com o "de venda" em LARANJA — exatamente o par que a
   DA-140 mandou sair de cena, aplicado à palavra que NOMEIA a direção.

E o portão que impede a volta: **zero azul e zero laranja** como cor de direção ou
estado de 1-2-3, medido no pixel e não no fonte.
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

DESKTOP = {"width": 1500, "height": 1000}
TELEFONE = {"width": 390, "height": 844}

# rgb() como o navegador devolve — é assim que `getComputedStyle` fala.
VERDE = "rgb(46, 204, 113)"     # --green  #2ecc71
VERMELHO = "rgb(255, 92, 108)"  # --red    #ff5c6c
# os dois que a DA-140 tirou de cena
AZUL = "rgb(110, 168, 254)"     # #6ea8fe
LARANJA = "rgb(255, 159, 67)"   # #ff9f43


def _storm(estado, direction, **kw):
    st = {"estado": estado, "direction": direction, "entrada": "ponto3",
          "rr": 2.4, "eden_rotulo": "Éden de Alta", "eden_ok": True,
          "sl": 100.0, "leituras": []}
    st.update(kw)
    return st


def _frame(frame, estado, direction="compra", storm=None):
    return {"frame": frame, "estado": estado, "direction": direction, "price": 513.53,
            "dist_pct": 0.0015, "dist_txt": "0.15%", "trigger": 512.76, "sl": 471.35,
            "tp": 515.06, "rr": 1.2, "rr_note": None, "pattern_state": "formando",
            "rr_entry": 512.76, "rr_basis": "gatilho", "rr_risco": 41.41,
            "rr_retorno": 2.3, "rr_residual": False,
            "storm": storm if storm is not None else _storm(estado, direction)}


# COMPRA e VENDA na MESMA tela: o dente é a diferença entre as duas, não o valor de
# uma delas isolada — uma paleta que pinta tudo de verde passaria em metade dos
# testes possíveis.
_SCAN = {
    "date": "2026-09-01", "frames": ["1d", "4h", "1h"],
    "resumo": {"em_gatilho": 2},
    "ativos": [
        {"ticker": "CMP", "melhor": _frame("1d", "em_gatilho", "compra"),
         "frames": [_frame("1d", "em_gatilho", "compra"),
                    _frame("4h", "em_movimento", "compra")]},
        {"ticker": "VND", "melhor": _frame("1d", "em_gatilho", "venda"),
         "frames": [_frame("1d", "em_gatilho", "venda"),
                    _frame("4h", "em_movimento", "venda")]},
        # a história: encerrado e invalidado NÃO podem sair na cor viva
        {"ticker": "HIST", "melhor": _frame("1d", "concluido", "compra"),
         "frames": [_frame("1d", "concluido", "compra"),
                    _frame("4h", "invalidou", "venda")]},
    ],
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


def _abre_scan(page, base, view="lista"):
    def handler(route):
        url = route.request.url
        if "/api/scan" in url and "verdicts" not in url and "/salvo" not in url:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(_SCAN))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)
    page.goto(base, wait_until="domcontentloaded")
    page.evaluate("(v) => localStorage.setItem('td_scan_view', v)", view)
    page.reload(wait_until="domcontentloaded")
    # No telefone a lista de observação nasce RECOLHIDA (o `<details>` é fechado por
    # JS abaixo de 900px) e o botão do scan mora dentro dela: abrir o painel é o
    # gesto do usuário, não uma concessão do teste.
    if not page.evaluate("() => document.getElementById('historyPanel').open"):
        page.click("#historyPanel > summary")
    page.click("#scanOpenBtn")
    page.click("#scanRunBtn")
    # `attached` e não `visible`: no telefone o painel nasce com a lista rolada e o
    # primeiro elemento pode estar fora da viewport — esperar por visibilidade seria
    # esperar por um gesto do usuário, não pelo render.
    page.wait_for_selector("#scanList .scan-chip", state="attached", timeout=20000)


# O que SAIU no pixel, por ativo e por frame — a cor computada da palavra do estado.
_PALAVRAS = """() => {
  const out = [];
  for (const cel of document.querySelectorAll('#scanList .scan-cell.scan-storm')) {
    const e = cel.querySelector('.ss-e');
    if (!e) continue;
    const linha = cel.closest('li, .scan-frame-row, .scan-row') || cel.parentElement;
    out.push({
      palavra: e.textContent.trim(),
      cor: getComputedStyle(e).color,
      classes: [...cel.classList],
      title: cel.title,
      opacidade: getComputedStyle(e).opacity,
      linha: (linha ? linha.innerText : '').replace(/\\s+/g, ' ').slice(0, 60),
    });
  }
  return out;
}"""


def _por(palavras, estado, direcao):
    achados = [p for p in palavras
               if estado in p["classes"] and direcao in p["classes"]]
    assert achados, (f"nenhuma célula {estado}/{direcao} na tela", palavras)
    return achados


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_palavra_GATILHO_sai_na_cor_da_DIRECAO(base):
    """O DENTE do pedido: "gatilho" verde num setup de VENDA é o erro do gráfico
    repetido em miniatura — e numa tela com 4 frames × 20 ativos a cor é o que se lê
    antes de tudo.

    A célula de ESTADO do Storm existe na visão LISTA; no modo CARDS a sub-linha
    dele nomeia a entrada e os níveis, sem palavra de estado — quem carrega a
    direção ali é o chip principal, que tem o seu próprio teste abaixo."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre_scan(page, base)
        pal = page.evaluate(_PALAVRAS)
        for c in _por(pal, "em_gatilho", "compra"):
            assert c["palavra"].lower() == "gatilho" and c["cor"] == VERDE, c
        for v in _por(pal, "em_gatilho", "venda"):
            assert v["palavra"].lower() == "gatilho" and v["cor"] == VERMELHO, v
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_MOVIMENTO_tambem_larga_o_azul_de_estado(base):
    """O vizinho da mesma linha: "movimento" saía no azul de destaque — cor por
    ESTADO. Corrigir só o "gatilho" deixaria o vizinho contando outra história, que
    é o defeito de classe que esta leva veio fechar."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre_scan(page, base)
        pal = page.evaluate(_PALAVRAS)
        for c in _por(pal, "em_movimento", "compra"):
            assert c["cor"] == VERDE, c
        for v in _por(pal, "em_movimento", "venda"):
            assert v["cor"] == VERMELHO, v
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_DIRECAO_e_recuperavel_sem_a_cor(base):
    """Cor NUNCA é o único portador (DA-078). A palavra visível diz o ESTADO
    ("gatilho"), não o lado — então o lado tem de estar no `title`, que é o que
    leitor de tela lê e o que resolve pra quem não distingue as matizes."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre_scan(page, base)
        pal = page.evaluate(_PALAVRAS)
        for c in _por(pal, "em_gatilho", "compra"):
            assert "de compra" in c["title"], c["title"]
        for v in _por(pal, "em_gatilho", "venda"):
            assert "de venda" in v["title"], v["title"]
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_HISTORIA_na_lista_NAO_volta_a_cor_viva(base):
    """Encerrado e invalidado são "já aconteceu". Na lista a célula tem a largura de
    uma palavra e o vizinho de cima é um setup VIVO da mesma direção: devolver a
    matiz aqui faria os dois se lerem iguais — que é justamente confundir história
    com oportunidade, o erro mais caro que esta tela pode induzir."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre_scan(page, base)
        pal = page.evaluate(_PALAVRAS)
        mortas = [x for x in pal
                  if "concluido" in x["classes"] or "invalidou" in x["classes"]]
        assert mortas, ("nenhuma célula de história na tela", pal)
        for x in mortas:
            assert x["cor"] not in (VERDE, VERMELHO), ("história em cor viva", x)
        browser.close()


# O CHIP PRINCIPAL da linha — o que existe nas DUAS visões (Cards e Lista) e diz
# COMPRA/VENDA quando o padrão está no gatilho.
_CHIPS = """() => [...document.querySelectorAll('#scanList .scan-chip')].map((c) => ({
  texto: c.textContent.trim(), cor: getComputedStyle(c).color,
  classes: [...c.classList],
}))"""


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("view", ["lista", "cards"])
def test_o_CHIP_da_linha_diz_a_direcao_nas_duas_visoes(base, view):
    """O marcador equivalente no modo CARDS. Ele já nascia direção-aware — este teste
    é a ÂNCORA que impede a leva da DA-140 de estragá-lo de passagem, e prova que a
    regra vale na visão que o dono usa pra ler os níveis."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre_scan(page, base, view)
        chips = page.evaluate(_CHIPS)
        compra = [c for c in chips if "compra" in c["classes"]]
        venda = [c for c in chips if "venda" in c["classes"]]
        assert compra and venda, ("as duas direções têm de estar na tela", chips)
        assert all(c["cor"] == VERDE for c in compra), compra
        assert all(c["cor"] == VERMELHO for c in venda), venda
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("view", ["lista", "cards"])
def test_zero_AZUL_e_zero_LARANJA_em_estado_ou_direcao_do_scan(base, view):
    """O PORTÃO, medido no pixel e não no fonte: nenhuma palavra de estado nem chip
    de direção do scan volta ao par que a DA-140 tirou de cena."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre_scan(page, base, view)
        for x in page.evaluate(_PALAVRAS) + page.evaluate(_CHIPS):
            assert x["cor"] not in (AZUL, LARANJA), ("azul/laranja de estado", x)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [DESKTOP, TELEFONE], ids=["desktop", "telefone"])
def test_a_regra_vale_igual_no_TELEFONE(base, viewport):
    """DA-101: no celular encolhe, não muda de significado. E é no celular que a cor
    pesa mais, porque cabe menos texto."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre_scan(page, base)
        pal = page.evaluate(_PALAVRAS)
        assert _por(pal, "em_gatilho", "compra")[0]["cor"] == VERDE, pal
        assert _por(pal, "em_gatilho", "venda")[0]["cor"] == VERMELHO, pal
        browser.close()


# ─────────────── O CABEÇALHO DOS CARDS DE LEITURA (o outro pedido) ───────────────
#
# No print do dono: "Storm123 DE COMPRA" com o "de compra" em AZUL e "Setup123 DE
# VENDA" com o "de venda" em LARANJA — o par que a DA-140 tirou de cena, aplicado à
# palavra que NOMEIA a direção. É o caso mais direto da regra que existe na tela.

from tests.test_webui_uma_gramatica_de_cor_e2e import _abre as _abre_run  # noqa: E402

_CABECALHOS = """() => Object.fromEntries(
  [...document.querySelectorAll('#setupCards .sc-123, #setupCards .sc-storm')]
    .map((c) => [c.classList.contains('sc-storm') ? 'storm' : 'plano', {
      dir: getComputedStyle(c.querySelector('.sc-dir')).color,
      borda: getComputedStyle(c).borderLeftColor,
      texto: c.querySelector('.sc-dir').textContent.trim(),
    }]))"""


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("direcao,cor", [("compra", VERDE), ("venda", VERMELHO)])
@pytest.mark.parametrize("viewport", [DESKTOP, TELEFONE], ids=["desktop", "telefone"])
def test_de_compra_VERDE_e_de_venda_VERMELHO_no_titulo_do_card(base, direcao, cor,
                                                               viewport):
    """A palavra da direção sai na cor da direção — nos DOIS métodos e nos dois
    tamanhos. Antes o Setup123 usava azul/laranja e o Storm123 os mesmos: duas
    leituras, a mesma paleta errada."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre_run(page, base, ["plano", "storm"], direcao=direcao)
        cards = page.evaluate(_CABECALHOS)
        assert set(cards) == {"plano", "storm"}, ("os dois cards na tela", cards)
        for nome, c in cards.items():
            assert c["dir"] == cor, (nome, direcao, c)
            assert c["borda"] == cor, ("a borda da leitura segue a direção", nome, c)
            assert c["dir"] not in (AZUL, LARANJA), (nome, c)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_DESFECHO_dentro_do_card_tambem_sai_da_paleta_direcional(base):
    """O terceiro ponto onde os dois eixos colidiam, e o mais escondido: DENTRO do
    card. A linha "ENCERRADO NO ALVO" saía em VERDE e a "ENCERRADO NO STOP" em
    VERMELHO, e o selo do topo reusava a pílula verde de "ativo" — três lugares
    dizendo "compra" e "opera" sobre um trade que já tinha acabado.

    O que eles perdem em cor recuperam em PESO: caixa alta e negrito. E o texto
    continua distinguindo alvo de stop, que é onde o resultado passou a morar.
    """
    from tests.test_webui_historia_ganho_e_perda_e2e import _abre as _abre_hist, _pat

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _ler = """() => {
          const linha = [...document.querySelectorAll('#setupCards .sc-row')]
            .find((r) => /ENCERRADO NO/.test(r.innerText));
          const selo = document.querySelector('#setupCards .sc-state');
          const k = linha && linha.querySelector('.sc-k');
          return {
            texto: k ? k.textContent.trim() : null,
            cor: k ? getComputedStyle(k).color : null,
            peso: k ? getComputedStyle(k).fontWeight : null,
            selo: selo ? {t: selo.textContent.trim(),
                          c: getComputedStyle(selo).color,
                          classes: [...selo.classList]} : null,
          };
        }"""
        for ciclo, palavra in (("concluido_alvo", "ENCERRADO NO ALVO"),
                               ("concluido_stop", "ENCERRADO NO STOP")):
            _abre_hist(page, base, _pat(ciclo, "venda"))
            m = page.evaluate(_ler)
            assert m["texto"] == palavra, (ciclo, m)
            assert m["cor"] not in (VERDE, VERMELHO, AZUL, LARANJA), (ciclo, m)
            assert int(m["peso"]) >= 700, ("perdeu a cor, tem de manter o peso", m)
        browser.close()


_BORDAS = """() => [...document.querySelectorAll(
  '#scanList .scan-line-row, #scanList .scan-frame-row, #scanList .scan-row')]
  .map((r) => ({classes: [...r.classList],
                borda: getComputedStyle(r).borderLeftColor,
                todas: getComputedStyle(r).borderColor}))"""


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("view", ["lista", "cards"])
def test_a_BORDA_da_linha_do_scan_tambem_larga_o_azul_de_estado(base, view):
    """O quarto ponto do inventário: a borda de "JÁ ANDOU" saía num azul
    (``#60a5fa``) que não dizia direção nem existia em mais lugar nenhum da paleta —
    estado pintado, do lado da linha cuja palavra o dono lê primeiro.

    Ela passa a dizer a DIREÇÃO, como a do gatilho. O que separa os dois continua
    sendo o FUNDO, que só a linha em gatilho recebe: é a que pede decisão agora.
    """
    azuis = ("rgb(96, 165, 250)", AZUL, LARANJA)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre_scan(page, base, view)
        linhas = page.evaluate(_BORDAS)
        mov = [r for r in linhas if "em_movimento" in r["classes"]]
        assert mov, ("nenhuma linha 'já andou' na tela", linhas)
        for r in mov:
            assert r["borda"] not in azuis and r["todas"] not in azuis, r
        # e a direção manda: compra e venda não podem sair na mesma borda
        cor = {d: {r["borda"] for r in mov if d in r["classes"]}
               for d in ("compra", "venda")}
        assert cor["compra"] and cor["venda"] and cor["compra"] != cor["venda"], cor
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_card_fantasma_continua_LEGIVEL(base):
    """ESMAECIDO NÃO É APAGADO — e agora vale pra toda história, não só pra
    invalidada, então o piso de contraste passou a valer em muito mais card.

    O título do card fantasma é `--muted` sobre o painel, 14px/700: texto pequeno
    pela WCAG, piso 4,5:1. DENTE: baixar a opacidade de fantasma "só um pouco" para
    o esmaecido ficar mais evidente é a tentação óbvia, e é ela que este teste pega —
    o custo não aparece no print, aparece em quem não enxerga bem.
    """
    def _lum(c):
        r, g, b = (int(c[i:i + 2], 16) / 255 for i in (1, 3, 5))
        def f(x):
            return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)

    from tests.test_webui_historia_ganho_e_perda_e2e import _abre as _abre_hist, _pat

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre_hist(page, base, _pat("concluido_alvo", "compra"))
        m = page.evaluate("""() => {
          const c = document.querySelector('#setupCards .sc-fantasma');
          const t = c && c.querySelector('.sc-title');
          if (!t) return null;
          const cs = getComputedStyle(t);
          return {cor: cs.color, tam: parseFloat(cs.fontSize), peso: cs.fontWeight,
                  alfa: parseFloat(getComputedStyle(c).opacity),
                  fundo: getComputedStyle(c).backgroundColor};
        }""")
        assert m, "nenhum card fantasma na tela"
        # a cor COMPOSTA: o navegador devolve a cor declarada, e a opacidade do card
        # é aplicada na composição — a conta tem de refazer a mistura contra o fundo.
        cor = [int(x) for x in re.findall(r"\d+", m["cor"])[:3]]
        fundo = [int(x) for x in re.findall(r"\d+", m["fundo"])[:3]]
        a = m["alfa"]
        mist = "#" + "".join(
            f"{round(cor[i] * a + fundo[i] * (1 - a)):02x}" for i in range(3))
        fundo_hex = "#" + "".join(f"{x:02x}" for x in fundo)
        lt, lf = _lum(mist), _lum(fundo_hex)
        razao = (max(lt, lf) + 0.05) / (min(lt, lf) + 0.05)
        assert razao >= 4.5, (
            f"título do card fantasma a {razao:.2f}:1 — abaixo do piso de 4,5:1 "
            f"para texto pequeno", m, mist)
        browser.close()

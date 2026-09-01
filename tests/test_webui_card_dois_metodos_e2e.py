"""O CARD MOSTRA OS DOIS MÉTODOS, e o veredito para de esmagar (DA-137).

Samyr, sobre a faixa: *"coloca na mesma linha do Preço e Retorno do investimento
alinhado à direita! deixa o veredito exclusivo no lugar dele!"*; e, sobre os
métodos: *"teremos que ter 2 linhas, uma para o Padrão 1-2-3 e outra para o
Storm 1-2-3"*.

**Medido no scan salvo:** 20 dos 20 ativos têm leitura nos DOIS métodos — duas
linhas é o caso NORMAL, não a exceção. Por isso a regra que compensa: **a linha de
um método só aparece quando ele tem algo a dizer**; quando não tem, o card encolhe
sozinho.

E o veredito ganha a fileira dele. Isso resolve **na raiz** o defeito da task
20260830-002 (o veredito crescia e esmagava o ticker): sem vizinho na fileira, não
há de quem roubar largura. Medido: o veredito truncava em 7 dos 20 cards e passa a
truncar em **0** a partir de 420px de lateral.
"""

import json
import re

import pytest

from tests.test_webui_frame_e_cor_e2e import DESKTOP, TELEFONE, sobe_servidor

pytestmark = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None


def _linha(frame, estado, direction="compra", storm=None):
    d = {"frame": frame, "estado": estado, "direction": direction, "price": 100.0}
    if storm is not None:
        d["storm"] = storm
    return d


# AAA tem os DOIS métodos · BBB só o 1-2-3 · CCC tem o Storm VETADO
_ATIVOS = [
    {"ticker": "AAA", "frames": [
        _linha("1d", "em_gatilho", "compra", {"estado": "formando", "direction": "venda"}),
        _linha("4h", "formando", "compra", {"estado": "em_gatilho", "direction": "venda"}),
        _linha("1h", "em_movimento", "compra", {"estado": "invalidou", "direction": "venda"})]},
    {"ticker": "BBB", "frames": [
        _linha("1d", "em_gatilho", "venda", {"estado": "sem_setup", "direction": None}),
        _linha("4h", "formando", "venda", {"estado": "sem_dado", "direction": None}),
        _linha("1h", "formando", "venda", {"estado": "sem_setup", "direction": None})]},
    {"ticker": "CCC", "frames": [
        _linha("1d", "sem_setup", None, {"estado": "vetado", "direction": "venda"}),
        _linha("4h", "sem_setup", None, {"estado": "vetado", "direction": "venda"}),
        _linha("1h", "formando", "compra", {"estado": "vetado", "direction": "venda"})]},
]
for _a in _ATIVOS:
    _a["melhor"] = _a["frames"][0]

_SCAN = {"date": "2026-08-31", "frames": ["1d", "4h", "1h"],
         "gerado_em": "2026-08-31T22:00:00-04:00", "ativos": _ATIVOS,
         "oportunidades": [], "resumo": {}}

_LONGO = "Compra especulativa com risco elevado"
_HIST = [{"run_id": f"R-{a['ticker']}", "ticker": a["ticker"], "date": "2026-08-31",
          "asset_type": "stock", "status": "done", "verdict": _LONGO,
          "elapsed": 1, "cost": {"usd": 0.0}, "finished_at": "2026-08-31 20:00"}
         for a in _ATIVOS]


def _abre(page, base_url, largura=None):
    def handler(route):
        u = route.request.url
        if "/api/scan" in u:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(_SCAN))
        elif "/api/history" in u:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"runs": _HIST}))
        elif "/api/watchlist" in u:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"tickers": [{"ticker": a["ticker"]}
                                                       for a in _ATIVOS]}))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_selector(".history li", state="attached", timeout=15000)
    page.wait_for_selector(".h-faixa", state="attached", timeout=15000)
    if largura:
        page.evaluate("""(w) => { const l = document.querySelector('main.layout');
          l.style.setProperty('--sidebar-w', w + 'px'); }""", largura)
    page.wait_for_timeout(350)


_CARD = """(tk) => {
  const li = [...document.querySelectorAll('.history li')]
    .find(e => e.dataset.ticker === tk);
  if (!li) return null;
  const linhas = [...li.querySelectorAll('.h-faixa-linha')].map(l => ({
    metodo: (l.querySelector('.fx-met') || {}).textContent || '',
    marcas: [...l.querySelectorAll('.fx-m')].map(m => ({
      tf: m.textContent, cls: m.className, title: m.title })),
  }));
  const corta = (e) => !!(e && e.scrollWidth > e.clientWidth + 1);
  const pr = li.querySelector('.h-price'), fx = li.querySelector('.h-faixa');
  const vd = li.querySelector('.h-verdict'), tk_ = li.querySelector('.tk-sym');
  const rp = pr && pr.getBoundingClientRect(), rf = fx && fx.getBoundingClientRect();
  return {
    linhas, altura: Math.round(li.getBoundingClientRect().height),
    tickerCortado: corta(tk_), precoCortado: corta(pr), veredictoCortado: corta(vd),
    // "mesma fileira" é medido: os dois têm de se sobrepor verticalmente
    mesmaFileira: !!(rp && rf && rf.top < rp.bottom - 2),
    vao: (rp && rf) ? Math.round(rf.left - rp.right) : null,
    // o veredito na SUA fileira: nada mais pode dividi-la com ele
    veredictoSozinho: (() => {
      const r = li.querySelector('.h-right');
      if (!r) return null;
      const rr = r.getBoundingClientRect();
      return ![...li.children].some(c => c !== r && c.getBoundingClientRect().height
        && Math.abs(c.getBoundingClientRect().top - rr.top) < 4);
    })(),
  };
}"""


@pytest.fixture
def base(tmp_path):
    yield from sobe_servidor(tmp_path)


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_UMA_LINHA_POR_METODO_e_so_de_quem_tem_o_que_dizer(base):
    """DENTE dos dois lados: com os dois métodos vêm duas linhas rotuladas; com um
    método só, vem uma — o card encolhe sozinho em vez de reservar espaço."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        dois = page.evaluate(_CARD, "AAA")
        assert len(dois["linhas"]) == 2, dois["linhas"]
        assert [x["metodo"] for x in dois["linhas"]] == ["123", "storm"], dois["linhas"]

        um = page.evaluate(_CARD, "BBB")
        assert len(um["linhas"]) == 1, ("o Storm não tem nada a dizer aqui", um["linhas"])
        assert um["linhas"][0]["metodo"] == "123", um["linhas"]
        # e o card com uma linha é MENOR que o de duas
        assert um["altura"] < dois["altura"], (um["altura"], dois["altura"])
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_VETADO_nao_e_o_mesmo_que_SEM_SETUP(base):
    """Vetado é um padrão que EXISTE e o Éden barrou (com motivo); sem setup é não
    haver o que mostrar. Os dois caem em "sem leitura" no eixo de fase, então quem
    os separa é a forma — e a palavra no title."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        ccc = page.evaluate(_CARD, "CCC")
        storm = next(x for x in ccc["linhas"] if x["metodo"] == "storm")
        assert all("fx-vetado" in m["cls"] for m in storm["marcas"]), storm["marcas"]
        assert all("não opera" in m["title"] for m in storm["marcas"]), storm["marcas"]
        # e o 1-2-3 do MESMO card, que está sem setup, usa a marca do sem-leitura
        s123 = next(x for x in ccc["linhas"] if x["metodo"] == "123")
        semes = [m for m in s123["marcas"] if m["tf"] in ("D", "4h")]
        assert all("fx-sem" in m["cls"] for m in semes), semes
        assert all("fx-vetado" not in m["cls"] for m in semes), semes
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_TITULO_diz_de_QUAL_metodo_e_a_marca(base):
    """Os dois métodos numeram 1-2-3 coisas diferentes: sem dizer de quem é a linha,
    não há como saber qual convenção está sendo lida."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        m = page.evaluate(_CARD, "AAA")
        for linha, nome in zip(m["linhas"], ("Setup123", "Storm123"), strict=True):
            for x in linha["marcas"]:
                # Desde a DA-143 o marcador navegável abre o title com a AÇÃO
                # ("Abrir AAA no 4h com Storm123 — …"); a LEITURA vem depois do
                # travessão, e é ela que tem de dizer de quem é a linha.
                acao = x["title"].startswith("Abrir ")
                leitura = x["title"].split(" — ", 1)[-1] if acao else x["title"]
                assert leitura.startswith(nome + " ·"), (nome, x["title"])
                if acao:
                    assert f"com {nome} —" in x["title"], (nome, x["title"])
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_PRECO_a_esquerda_e_FAIXA_a_direita_na_MESMA_fileira(base):
    """O layout que o Samyr fechou — e "mesma fileira" é MEDIDO (sobreposição
    vertical), não presumido pela ordem no HTML."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        m = page.evaluate(_CARD, "AAA")
        assert m["mesmaFileira"] is True, m
        assert m["veredictoSozinho"] is True, ("o veredito divide a fileira", m)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("largura", [200, 240, 280, 340, 420, 560])
def test_em_NENHUMA_largura_o_ativo_ou_o_preco_e_sacrificado(base, largura):
    """A regra 11 da DA-078 avisa que "um à esquerda + outro à direita" é o padrão
    que abre vão no meio e trunca nas pontas. Quem cede é a FAIXA; o ticker e o
    preço, nunca (task 20260830-002)."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, largura=largura)
        m = page.evaluate(_CARD, "AAA")
        assert m["tickerCortado"] is False, (largura, m)
        assert m["precoCortado"] is False, (largura, m)
        assert m["vao"] is None or m["vao"] < 60, ("vão central", largura, m["vao"])
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_veredito_LONGO_deixa_de_truncar_na_largura_padrao(base):
    """A raiz do defeito da task 20260830-002: com a fileira só pra ele, o veredito
    para de disputar largura. Medido no scan real: truncava em 7 de 20."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, largura=560)
        m = page.evaluate(_CARD, "AAA")
        assert m["veredictoCortado"] is False, ("ainda trunca com lateral larga", m)
        assert m["tickerCortado"] is False, m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_no_TELEFONE_as_duas_linhas_continuam_legiveis(base):
    """DA-101: encolhe, não some — e não vira mancha."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE)
        _abre(page, base)
        m = page.evaluate(_CARD, "AAA")
        assert len(m["linhas"]) == 2, m["linhas"]
        assert all(len(x["marcas"]) == 3 for x in m["linhas"]), m["linhas"]
        assert m["tickerCortado"] is False and m["precoCortado"] is False, m
        browser.close()

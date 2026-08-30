"""DA-070, correção de ESCOPO: degradê nenhum em superfície da UI.

A primeira redação da DA falava em "card, linha ou chip" e o scan foi chapado —
mas a instrução do Samyr foi "NÃO USAR DEGRADÊ", sem qualificar. Sobravam cinco:
quatro botões primários repetindo `180deg, #3b82f6 → #2563eb` copiado e colado
(#runBtn, .cfg-save, .err-action, .btn-primary) e a .bar-fill em azul→ciano.

Fica de fora, de propósito, a máscara das amostras da legenda: aquele
`repeating-linear-gradient` não é cor de fundo, é o PADRÃO TRACEJADO que
distingue a EMA da linha cheia — e, desde a DA-108, o ponto-traço que diz que um
nível é do Storm123 depois que a cor dele passou a significar ganho/perda.
Chapar apagaria informação — por isso o teste não só permite a máscara como
EXIGE que ela continue lá.

A regra do portão estático é a INTENÇÃO, não uma contagem: toda ocorrência de
`gradient` tem de ser máscara de amostra de legenda (`.sw`), em par
`-webkit-mask-image` + `mask-image`. Contar linhas fazia a próxima amostra
legítima parecer violação e convidava a afrouxar o teste em vez de conferi-lo.

Dois portões porque falham por motivos diferentes: o estático pega o degradê
novo que alguém escreveu no CSS (mesmo em regra morta), e o de runtime prova o
que o navegador aplica — e que os quatro botões saem do MESMO token, não de
cinco literais que voltam a divergir na próxima cor.
"""

import pathlib
import threading

import pytest

from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.server import make_server
from tradingagents.webui.store import HistoryStore

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None

_CSS = pathlib.Path(__file__).resolve().parents[1] / "tradingagents" / "webui" / "static" / "style.css"


def test_style_css_so_tem_degrade_em_mascara_de_amostra_da_legenda():
    """Portão estático: TODA ocorrência de degradê é máscara de amostra de legenda.

    Nenhuma pode ser `background`, `background-image` ou `border-image` — é aí que
    o degradê volta como superfície. E cada máscara vem em par (com e sem prefixo),
    senão ela só funciona em metade dos navegadores e o traço some justamente onde
    ninguém testa.
    """
    texto = _CSS.read_text()
    linhas = [(n, ln.strip()) for n, ln in enumerate(texto.splitlines(), 1)
              if "gradient" in ln]
    assert linhas, "a máscara da legenda sumiu — o traço é informação, não enfeite"
    assert all("mask-image" in ln and "repeating-linear-gradient" in ln
               for _, ln in linhas), ("degradê fora de máscara é superfície", linhas)
    padrao = [ln for _, ln in linhas if ln.startswith("mask-image")]
    webkit = [ln for _, ln in linhas if ln.startswith("-webkit-mask-image")]
    assert len(padrao) == len(webkit) == len(linhas) / 2, ("cada máscara em par", linhas)
    # e cada uma pertence a uma amostra da legenda (`.sw`), nunca a uma superfície:
    # o seletor é a última linha aberta com `{` antes da declaração
    dono, abertos = {}, ""
    for n, ln in enumerate(texto.splitlines(), 1):
        crua = ln.strip()
        if crua.endswith("{"):
            abertos = crua[:-1].strip()
        elif "gradient" in crua:
            dono[n] = abertos
    fora = {n: s for n, s in dono.items() if ".sw" not in s}
    assert not fora, ("degradê fora de amostra da legenda", fora)


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


@pytest.mark.integration
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_acao_primaria_e_barra_saem_chapadas_do_mesmo_token(base):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.goto(base, wait_until="networkidle")
        page.wait_for_selector("#runBtn")
        # .err-action e .btn-primary só existem depois de um erro / da caixa de
        # dono; o que se mede é a REGRA de CSS, então basta plantar o elemento.
        page.evaluate("""() => {
          const d = document.createElement('div');
          d.innerHTML = `<div class="error-card"><button class="err-action">x</button></div>
                         <div class="owner-box"><button class="btn-primary">y</button></div>`;
          document.body.appendChild(d);
        }""")
        m = page.evaluate("""() => {
          const g = (s) => { const c = getComputedStyle(document.querySelector(s));
            return {bg: c.backgroundImage, cor: c.backgroundColor}; };
          return {runBtn: g('#runBtn'), barFill: g('.bar-fill'), cfgSave: g('.cfg-save'),
                  errAction: g('.error-card .err-action'), btnPrimary: g('.owner-box .btn-primary')};
        }""")
        assert all(v["bg"] == "none" for v in m.values()), m
        # UM token, não cinco literais: as cinco superfícies pintam a MESMA cor
        assert len({v["cor"] for v in m.values()}) == 1, m
        assert m["runBtn"]["cor"] != "rgba(0, 0, 0, 0)", m
        browser.close()


@pytest.mark.integration
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_legenda_ema_continua_tracejada(base):
    """A exceção declarada da DA-070: máscara que carrega INFORMAÇÃO fica."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.goto(base, wait_until="networkidle")
        mask = page.evaluate("""() => {
          const d = document.createElement('div');
          d.className = 'chart-legend';
          d.innerHTML = '<span class="lg"><i class="sw ema"></i>EMA</span>';
          document.body.appendChild(d);
          const c = getComputedStyle(d.querySelector('.sw.ema'));
          return c.maskImage || c.webkitMaskImage;
        }""")
        assert "repeating-linear-gradient" in (mask or ""), mask
        browser.close()

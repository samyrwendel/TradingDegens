"""A visão SINAIS na tela, com o dataset REAL de 31/08 (task 20260831-017).

O que o Samyr pediu foi organização, não campo novo: *"preciso identificar as
entradas de uma maneira mais clara, tipo oportunidade no Setup123 no 1h e 4h
compra janela de x a y… não sei como organizar isso"*. Então o que estes testes
medem é a LEITURA — o que salta da tela sem cruzar linhas com o olho:

* a lista abre na visão de SINAIS, não na tabela de dado;
* a confluência está escrita ("2 frames concordam"), não deduzida;
* a janela aparece com os dois preços E a frase que explica o limite;
* o CONFLITO é um card sem níveis — publicar gatilho e alvo ali convidaria a
  operar um lado de uma leitura dividida;
* o sinal NOVO desde a última visita se marca, e a primeira visita não marca nada
  (com a memória vazia, "novo" seria a lista inteira);
* a tabela (Cards/Lista) continua a um clique — é a visão de dado, e alguém vai
  querer conferir.
"""

import json
import shutil
import threading
from pathlib import Path

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

_FIXTURE = Path(__file__).parent / "data" / "scan_real_20260831.json"


@pytest.fixture
def base(tmp_path):
    """Servidor com o último conhecido REAL de 31/08 já em disco.

    O arquivo é copiado direto para o lugar em que o :class:`ScanSnapshotStore`
    lê: assim a abertura serve o dataset real na hora, e a varredura (represada
    pelo portão) nunca precisa acontecer.
    """
    shutil.copy(_FIXTURE, tmp_path / "last_scan.json")
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "openai",
                     "deep_think_llm": "x", "quick_think_llm": "y"},
        store=HistoryStore(tmp_path))
    httpd = make_server("127.0.0.1", 0, runner=runner)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", runner
    finally:
        httpd.shutdown()


_PORTAO = """() => {
  window.__portao = {};
  const orig = window.fetch;
  window.fetch = (u, o) => String(u).includes('/api/scan?')
    ? new Promise((res) => { window.__portao.ok = res; })
    : orig(u, o);
}"""

_LEITURA = """() => {
  const card = (e) => ({
    ticker: e.querySelector('.sn-tk').textContent,
    dir: (e.querySelector('.sn-dir') || {}).textContent,
    metodo: (e.querySelector('.sn-metodo') || {}).textContent,
    conf: (e.querySelector('.sn-conf') || {}).textContent || null,
    frames: [...e.querySelectorAll('.sn-frames .sn-frame')].map(x => x.textContent),
    faixa: (e.querySelector('.sn-faixa') || {}).textContent || null,
    motivo: (e.querySelector('.sn-motivo') || {}).textContent || null,
    niveis: (e.querySelector('.sn-niveis') || {}).textContent || null,
    novo: !!e.querySelector('.sn-novo'),
    estado: [...e.classList].filter(c => c !== 'sn-card'),
    lados: [...e.querySelectorAll('.sn-lado')].map(l => l.textContent),
    dissidente: (e.querySelector('.sn-dissidente') || {}).textContent || null,
    outro: (e.querySelector('.sn-outro') || {}).textContent || null,
    acoes: e.querySelectorAll('.scan-go').length,
  });
  return {
    secoes: [...document.querySelectorAll('.sn-secao')].map(e => ({
      titulo: e.querySelector('.sn-secao-tit').textContent,
      n: e.querySelector('.sn-secao-n').textContent,
      nota: e.querySelector('.sn-secao-nota').textContent,
    })),
    cards: [...document.querySelectorAll('.sn-card')].map(card),
    filtrosVisiveis: !document.querySelector('#scanFilters').classList.contains('hidden'),
    viewAtiva: (document.querySelector('.scan-view.is-active') || {}).dataset,
  };
}"""


def _abre(page, url, limpa_memoria=True):
    page.goto(url, wait_until="load")
    if limpa_memoria:
        page.evaluate("() => localStorage.removeItem('td_sinais_vistos')")
    page.evaluate("() => localStorage.removeItem('td_scan_view')")
    page.evaluate(_PORTAO)
    page.click("#scanOpenBtn")
    page.wait_for_selector("#scanList .sn-card", timeout=10000)


def _card(m, ticker, metodo):
    return next((c for c in m["cards"]
                 if c["ticker"] == ticker and c["metodo"] == metodo), None)


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_visao_de_SINAIS_e_a_entrada_padrao_e_organiza_em_secoes(base):
    url, _ = base
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1500, "height": 950},
                                  timezone_id="America/Manaus")
        page = ctx.new_page()
        _abre(page, url)
        m = page.evaluate(_LEITURA)

        assert m["viewAtiva"]["view"] == "sinais", m["viewAtiva"]
        # (a caixa alta é do CSS; o texto vem como está escrito no código)
        titulos = [s["titulo"] for s in m["secoes"]]
        assert titulos == ["Entrada agora", "A caminho", "Fora da janela",
                           "Conflito entre frames"], titulos
        # cada seção diz o que ela SIGNIFICA — título sozinho não organiza
        assert all(s["nota"] for s in m["secoes"]), m["secoes"]
        # os chips de estado (vocabulário do DADO) não competem com as seções
        assert not m["filtrosVisiveis"]
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_ENTRADA_traz_janela_com_os_dois_precos_e_o_limite_em_palavras(base):
    """INTC Storm123 venda, 1d+1h: a única janela aberta do dataset real."""
    url, _ = base
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1500, "height": 950},
                                  timezone_id="America/Manaus")
        page = ctx.new_page()
        _abre(page, url)
        m = page.evaluate(_LEITURA)

        c = _card(m, "INTC", "Storm123")
        assert "entrada" in c["estado"] and c["dir"] == "VENDA"
        assert c["frames"] == ["D", "1h"], c
        assert "2 frames concordam" in c["conf"], c
        assert " a " in c["faixa"], ("a janela tem de mostrar os DOIS preços", c)
        # o limite não vai sozinho: o PORQUÊ é a coisa nova que esta tela traz
        assert "não paga o risco" in c["motivo"], c
        # numa VENDA quem estraga o R:R é entrar mais BARATO
        assert "abaixo de" in c["motivo"], ("o lado do limite está invertido", c)
        assert "R:R no gatilho" in c["niveis"] and "gatilho" in c["niveis"], c
        assert c["acoes"] == 2, ("entrada aberta oferece a análise a um clique", c)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_CONFLITO_mostra_os_dois_lados_e_NENHUM_nivel(base):
    """AAPL: venda no 1d contra compra no 4h e 1h.

    DENTE: publicar gatilho/alvo aqui convidaria a operar um lado de uma leitura
    dividida — e a divisão é justamente a informação.
    """
    url, _ = base
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1500, "height": 950},
                                  timezone_id="America/Manaus")
        page = ctx.new_page()
        _abre(page, url)
        m = page.evaluate(_LEITURA)

        c = _card(m, "AAPL", "Setup123")
        assert "conflito" in c["estado"] and c["dir"] == "CONFLITO"
        assert len(c["lados"]) == 2, c
        assert any("COMPRA" in x for x in c["lados"]) and any("VENDA" in x for x in c["lados"])
        assert c["faixa"] is None and c["niveis"] is None, ("conflito publicou níveis", c)
        assert c["acoes"] == 0, ("conflito não oferece entrada", c)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_confluencia_conflito_padrao_morto_e_ausencia_na_MESMA_tela(base):
    url, _ = base
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1500, "height": 950},
                                  timezone_id="America/Manaus")
        page = ctx.new_page()
        _abre(page, url)
        m = page.evaluate(_LEITURA)

        # CRWD: dois frames de compra num card só
        crwd = _card(m, "CRWD", "Storm123")
        assert crwd["frames"] == ["4h", "1h"] and "2 frames" in crwd["conf"]

        # MSFT: três concordam e ainda assim não há janela — confluência não é permissão
        msft = _card(m, "MSFT", "Setup123")
        assert "3 frames concordam" in msft["conf"]
        assert msft["faixa"] is None and "não há preço de entrada" in msft["motivo"]

        # GOOGL: o 1d invalidado NÃO vota, e aparece declarado como dissidente
        googl = _card(m, "GOOGL", "Setup123")
        assert "conflito" not in googl["estado"] and googl["dir"] == "VENDA"
        assert googl["dissidente"] and "invalidado" in googl["dissidente"], googl

        # MRVL no 1-2-3: os três frames invalidados, nenhuma oportunidade
        assert _card(m, "MRVL", "Setup123") is None

        # O OUTRO MÉTODO é mencionado quando DISCORDA — nunca fundido (DA-077).
        # AVGO é o caso real: o 1-2-3 lê compra e o Storm lê venda, cada um no seu
        # card, e o card do 1-2-3 avisa. Em GOOGL os dois leem venda: nada a
        # avisar, e a menção não aparece — aviso que sempre aparece não avisa nada.
        avgo = _card(m, "AVGO", "Setup123")
        assert avgo["outro"] and "Storm123" in avgo["outro"] and "VENDA" in avgo["outro"], avgo
        assert googl["outro"] is None, ("os dois métodos concordam em GOOGL", googl)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_NOVO_desde_a_ultima_visita_marca_so_o_que_apareceu_depois(base):
    """A primeira visita não marca nada; a seguinte marca só o sinal que nasceu.

    DENTE: com a memória vazia "novo" seria a lista inteira — um alarme que não
    distingue nada de nada.
    """
    url, runner = base
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1500, "height": 950},
                                  timezone_id="America/Manaus")
        page = ctx.new_page()
        _abre(page, url)
        m = page.evaluate(_LEITURA)
        assert not any(c["novo"] for c in m["cards"]), "a primeira visita marcou tudo"

        # chega uma passada com um ativo NOVO em gatilho
        salvo = json.loads(_FIXTURE.read_text(encoding="utf-8"))
        novo = {"ticker": "ZZZ", "melhor": {"estado": "em_gatilho"}, "frames": [{
            "frame": "1d", "estado": "em_gatilho", "direction": "compra",
            "price": 100.0, "trigger": 100.0, "sl": 90.0, "tp": 130.0,
            "dist_pct": 0.0, "storm": {"estado": "sem_setup"}}]}
        salvo["ativos"] = salvo["ativos"] + [novo]
        salvo["gerado_em"] = "2026-08-31T18:00:00-04:00"
        runner.scan_snapshot.path.write_text(json.dumps(salvo), encoding="utf-8")

        page.reload(wait_until="load")
        page.evaluate(_PORTAO)
        page.click("#scanOpenBtn")
        page.wait_for_selector("#scanList .sn-card", timeout=10000)
        m2 = page.evaluate(_LEITURA)
        marcados = [c["ticker"] for c in m2["cards"] if c["novo"]]
        assert marcados == ["ZZZ"], marcados
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_tabela_de_dado_continua_a_um_clique(base):
    """A visão de OPORTUNIDADE não substitui a de DADO — alguém vai querer conferir."""
    url, _ = base
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1500, "height": 950},
                                  timezone_id="America/Manaus")
        page = ctx.new_page()
        _abre(page, url)
        page.click('.scan-view[data-view="lista"]')
        page.wait_for_selector("#scanList .scan-line-row", timeout=5000)
        est = page.evaluate("""() => ({
          sinais: document.querySelectorAll('.sn-card').length,
          linhas: document.querySelectorAll('.scan-line-row').length,
          filtros: !document.querySelector('#scanFilters').classList.contains('hidden'),
        })""")
        assert est["sinais"] == 0 and est["linhas"] > 0, est
        assert est["filtros"], ("na visão de dado os chips de estado voltam", est)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_no_celular_o_card_cabe_e_a_pagina_nao_rola_na_horizontal(base):
    """390×844 (DA-062/DA-101)."""
    url, _ = base
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 390, "height": 844},
                                  timezone_id="America/Manaus")
        page = ctx.new_page()
        page.goto(url, wait_until="load")
        page.evaluate("() => { localStorage.clear();"
                      " document.getElementById('historyPanel').open = true; }")
        page.evaluate(_PORTAO)
        page.click("#scanOpenBtn")
        page.wait_for_selector("#scanList .sn-card", timeout=10000)
        est = page.evaluate("""() => {
          const cards = [...document.querySelectorAll('.sn-card')];
          const larg = cards.map(c => c.getBoundingClientRect().right);
          return {n: cards.length, maxDir: Math.max(...larg),
                  doc: document.documentElement.scrollWidth,
                  secoes: document.querySelectorAll('.sn-secao').length};
        }""")
        assert est["n"] > 0 and est["secoes"] == 4, est
        assert est["maxDir"] <= 390.5, ("card vazou do viewport", est)
        assert est["doc"] <= 390, ("a página passou a rolar na horizontal", est)
        browser.close()

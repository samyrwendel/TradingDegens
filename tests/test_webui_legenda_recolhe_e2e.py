"""A LEGENDA RECOLHE O QUE O DESENHO JÁ DIZ (DA-135).

Samyr: *"a gente tinha combinado de tirar toda essa legenda do topo do gráfico"* —
o combinado não existe registrado, e isso foi dito a ele; **o problema, sim, existe
e piorou** depois que as duas famílias passaram a conviver.

**Medido na run REAL do TSM** (`20260831-184230-2affed`), com Setup123 e Storm123
ligados: **17 itens de legenda, 109px no desktop e 156px no telefone** antes de o
gráfico começar. E **10 dos 17 (59%)** são os rótulos das zonas do plano, repetidos
**verbatim** do que já está escrito na linha do preço, dentro do próprio gráfico.

A divisória, então, não é de gosto — é de redundância medida:

* o que o DESENHO já rotula (as 10 zonas) recolhe, e nasce recolhido;
* a CHAVE (médias, marca de recuo, os dois marcadores 1-2-3) fica sempre, porque é
  a única coisa do gráfico que **não** tem rótulo ao lado do traço.

E nada sai sem destino: o botão diz QUANTOS são, então a ausência é declarada.
"""

import contextlib
import json
import pathlib

import pytest

from tests.test_webui_frame_e_cor_e2e import DESKTOP, TELEFONE, sobe_servidor

pytestmark = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None

_REC = pathlib.Path(
    "/home/clawd/.tradingagents/logs/webui/runs/20260831-184230-2affed.json")


def _sobe(tmp_path):
    """O servidor com a run REAL do print — sem fixture inventado."""
    rec = json.loads(_REC.read_text(encoding="utf-8"))
    runs = tmp_path / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{rec['run_id']}.json").write_text(json.dumps(rec), encoding="utf-8")
    return rec["run_id"], sobe_servidor(tmp_path)


_MED = """() => {
  const cont = document.getElementById('chartLegend');
  const vis = (e) => !!(e && e.getClientRects().length);
  const itens = [...cont.querySelectorAll('.lg')];
  const cv = document.getElementById('priceChart');
  const btn = document.getElementById('chartNiveisBtn');
  return {
    visiveis: itens.filter(vis).map(e => e.textContent.trim()),
    todos: itens.length,
    altura: Math.round(cont.getBoundingClientRect().height),
    topoDoCanvas: Math.round(cv.getBoundingClientRect().top),
    zonasNoDesenho: planZones(cv._actionable || {}).map(z => z.tag),
    temBotao: vis(btn),
    rotuloBotao: btn ? btn.textContent.trim() : '',
    expanded: btn ? btn.getAttribute('aria-expanded') : null,
    guardado: localStorage.getItem('td_chart_niveis'),
  };
}"""


def _abre(page, base_url, run_id, storm=True):
    page.goto(base_url, wait_until="domcontentloaded")
    page.evaluate(f"() => watchRun({run_id!r})")
    page.wait_for_selector("#setupCards:not(.hidden)")
    page.wait_for_timeout(400)
    if storm:
        page.evaluate("""() => {
          const b = [...document.querySelectorAll('#camadasSelector button')]
            .find(x => /storm/i.test(x.textContent || ''));
          if (b) b.click();
        }""")
        page.wait_for_timeout(500)


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.skipif(not _REC.exists(), reason="a run real não está neste disco")
def test_o_que_o_DESENHO_ja_rotula_sai_do_topo_e_a_CHAVE_fica(tmp_path):
    """A divisória, provada contra o desenho: cada item recolhido tem de estar na
    lista de zonas que o gráfico rotula, e nenhum item da chave pode estar nela."""
    run_id, it = _sobe(tmp_path)
    base_url = next(it)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport=DESKTOP)
            _abre(page, base_url, run_id)
            m = page.evaluate(_MED)
            zonas = set(m["zonasNoDesenho"])
            assert len(zonas) >= 8, ("a run tem de ter as duas famílias", m)
            # nada do que ficou visível é uma zona já rotulada no gráfico
            assert not (set(m["visiveis"]) & zonas), (
                "sobrou no topo algo que o desenho já diz", set(m["visiveis"]) & zonas)
            # e a chave continua: médias e os marcadores
            juntos = " · ".join(m["visiveis"])
            assert "MMS20" in juntos and "recuo à média (histórico)" in juntos, m
            assert "1-2-3" in juntos, m
            browser.close()
    finally:
        with contextlib.suppress(StopIteration):
            next(it)   # fecha o servidor


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.skipif(not _REC.exists(), reason="a run real não está neste disco")
def test_a_ALTURA_cai_de_verdade_e_o_grafico_comeca_antes(tmp_path):
    """Medido em pixels: o ganho é a altura que o gráfico recebe de volta."""
    run_id, it = _sobe(tmp_path)
    base_url = next(it)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport=DESKTOP)
            _abre(page, base_url, run_id)
            fechada = page.evaluate(_MED)
            page.click("#chartNiveisBtn")
            page.wait_for_timeout(250)
            aberta = page.evaluate(_MED)
            assert aberta["altura"] > fechada["altura"] * 1.8, (fechada, aberta)
            assert aberta["topoDoCanvas"] > fechada["topoDoCanvas"], (
                "o gráfico não subiu quando a legenda recolheu", fechada, aberta)
            browser.close()
    finally:
        with contextlib.suppress(StopIteration):
            next(it)   # fecha o servidor


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.skipif(not _REC.exists(), reason="a run real não está neste disco")
def test_nasce_RECOLHIDA_e_a_ausencia_e_DECLARADA(tmp_path):
    """Ao contrário da dica dos gestos (DA-128), que nasce ABERTA: lá recolher
    custava o ensino do gesto; aqui não custa nada, porque a informação continua na
    tela — no desenho, no preço exato, com o mesmo texto. E o botão diz QUANTOS
    são: ausência declarada, não silenciosa."""
    run_id, it = _sobe(tmp_path)
    base_url = next(it)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport=DESKTOP)
            _abre(page, base_url, run_id)
            m = page.evaluate(_MED)
            assert m["expanded"] == "false", m
            assert m["temBotao"], "sem botão, o que sumiu some calado"
            assert "níveis" in m["rotuloBotao"], m["rotuloBotao"]
            # o NÚMERO no rótulo é o dos que estão recolhidos
            n = len(m["zonasNoDesenho"])
            assert str(n) in m["rotuloBotao"], (n, m["rotuloBotao"])
            browser.close()
    finally:
        with contextlib.suppress(StopIteration):
            next(it)   # fecha o servidor


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.skipif(not _REC.exists(), reason="a run real não está neste disco")
def test_a_decisao_SOBREVIVE_a_visita_seguinte_nos_dois_sentidos(tmp_path):
    """A mesma disciplina da largura da lateral e da dica: abrir não pode virar
    tarefa a refazer toda visita — nem recolher, pra quem abriu."""
    run_id, it = _sobe(tmp_path)
    base_url = next(it)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport=DESKTOP)
            _abre(page, base_url, run_id)
            page.click("#chartNiveisBtn")
            page.wait_for_timeout(200)
            assert page.evaluate(_MED)["guardado"] == "on"

            _abre(page, base_url, run_id)          # nova visita
            m = page.evaluate(_MED)
            assert m["expanded"] == "true", ("a preferência não sobreviveu", m)

            page.click("#chartNiveisBtn")
            page.wait_for_timeout(200)
            _abre(page, base_url, run_id)
            m2 = page.evaluate(_MED)
            assert m2["expanded"] == "false", m2
            browser.close()
    finally:
        with contextlib.suppress(StopIteration):
            next(it)   # fecha o servidor


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.skipif(not _REC.exists(), reason="a run real não está neste disco")
def test_no_TELEFONE_o_ganho_e_MAIOR(tmp_path):
    """No celular a legenda quebrava em mais linhas — 156px medidos. É onde a altura
    é mais escassa (DA-101)."""
    run_id, it = _sobe(tmp_path)
    base_url = next(it)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport=TELEFONE)
            _abre(page, base_url, run_id)
            fechada = page.evaluate(_MED)
            page.click("#chartNiveisBtn")
            page.wait_for_timeout(250)
            aberta = page.evaluate(_MED)
            assert aberta["altura"] > fechada["altura"], (fechada, aberta)
            assert fechada["altura"] < 100, ("recolhida ainda ocupa muito", fechada)
            browser.close()
    finally:
        with contextlib.suppress(StopIteration):
            next(it)   # fecha o servidor


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.skipif(not _REC.exists(), reason="a run real não está neste disco")
def test_SEM_niveis_no_plano_nao_aparece_botao(tmp_path):
    """DENTE do exagero oposto: um botão que abre o nada é pior que nenhum botão."""
    run_id, it = _sobe(tmp_path)
    base_url = next(it)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport=DESKTOP)
            _abre(page, base_url, run_id)
            m = page.evaluate("""() => {
              const cv = document.getElementById('priceChart');
              // um plano SEM zona nenhuma
              document.getElementById('chartLegend').innerHTML =
                chartLegendHtml(cv._chart, {});
              return {btn: !!document.getElementById('chartNiveisBtn'),
                      itens: document.querySelectorAll('#chartLegend .lg').length};
            }""")
            assert m["btn"] is False, ("botão sem níveis pra abrir", m)
            browser.close()
    finally:
        with contextlib.suppress(StopIteration):
            next(it)   # fecha o servidor

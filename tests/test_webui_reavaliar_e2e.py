"""REAVALIAR: o que continua válido não se apaga, e o frame não pode divergir (DA-136).

Duas queixas do mesmo clique, e elas tiveram destinos diferentes.

**(a) O gráfico SUMIA.** `reevaluate()` escondia o `#resultPanel` inteiro **antes do
POST** — gráfico, níveis e cards junto. Reavaliar dispara uma análise NOVA, então
mostrar progresso é legítimo; apagar a leitura vigente, que continua válida até a
nova chegar, não é. É a quarta aparição da mesma regra de classe (DA-118), agora no
único caminho que tinha ficado de fora. **Reproduzido antes de corrigir.**

**(b) "O botão dizia 1h e a mensagem dizia Diário".** Instrumentei e **NÃO consegui
reproduzir** — e a razão é estrutural: a mensagem e o frame enviado saem da MESMA
variável, na MESMA chamada (`reevaluate(tf)` usa `tf` nos dois), e todo ponto que
escreve `_tf` chama `renderTfSelector()` em seguida, que redesenha o rótulo do botão.
Inclusive o caminho que parecia o suspeito — o backend DEGRADAR o frame pedido — em
que os três passam a dizer "Diário" juntos, que é o correto.

Como não reproduzi, o que fica é o DENTE: este módulo trava o invariante nos dois
cenários. Se algum dia divergirem, ele cai — em vez de a divergência voltar como
print.
"""

import contextlib
import json
import pathlib
import re

import pytest

from tests.test_webui_frame_e_cor_e2e import DESKTOP, sobe_servidor

pytestmark = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None

_REC = pathlib.Path(
    "/home/clawd/.tradingagents/logs/webui/runs/20260831-184230-2affed.json")


def _sobe(tmp_path):
    rec = json.loads(_REC.read_text(encoding="utf-8"))
    runs = tmp_path / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{rec['run_id']}.json").write_text(json.dumps(rec), encoding="utf-8")
    return rec, sobe_servidor(tmp_path)


def _abre(page, base_url, rec, enviados, degrada_para=None, trava_analyze=True,
          frame="4h"):
    """Abre a run e troca de frame. `degrada_para` faz o backend devolver OUTRO.

    O frame padrão é o **4h** porque o veredito desta run é no 1h: escolher o
    próprio frame do veredito desabilitaria o botão, e o teste passaria sem nunca
    clicar em nada.
    """
    def handler(route):
        u = route.request.url
        if "/api/analyze" in u:
            enviados.append(json.loads(route.request.post_data or "{}"))
            if trava_analyze:   # a run nova fica "rodando": é o estado que se observa
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"run_id": "R-NOVA", "run_token": "t"}))
            else:
                route.fulfill(status=500, content_type="application/json",
                              body=json.dumps({"error": "falhou"}))
        elif "/api/chart" in u and degrada_para:
            d = rec["result"]
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "timeframe": degrada_para,
                "timeframes": ["1w", "1d", "4h", "1h", "15m"],
                "price_chart": d.get("price_chart"), "actionable": d.get("actionable"),
                "degraded": [], "live_price": None}))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)
    page.goto(base_url, wait_until="domcontentloaded")
    page.evaluate(f"() => watchRun({rec['run_id']!r})")
    page.wait_for_selector("#setupCards:not(.hidden)")
    page.wait_for_timeout(400)
    page.evaluate("""(f) => { const b = [...document.querySelectorAll('#tfSelector button')]
        .find(x => x.dataset.tf === f); if (b) b.click(); }""", frame)
    page.wait_for_timeout(900)


_ESTADO = """() => ({
  rotulo: (document.getElementById('reevalBtn') || {}).textContent || '',
  desabilitado: !!(document.getElementById('reevalBtn') || {}).disabled,
  tf: typeof _tf === 'string' ? _tf : null,
  painelEscondido: document.getElementById('resultPanel').classList.contains('hidden'),
  graficoNaTela: document.getElementById('chartCard').getClientRects().length > 0,
  revalidando: document.getElementById('chartCard').classList.contains('is-revalidando'),
  progresso: (document.getElementById('progressPanel') || {}).innerText || '',
  progressoVisivel: document.getElementById('progressPanel').getClientRects().length > 0,
})"""


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.skipif(not _REC.exists(), reason="a run real não está neste disco")
def test_o_GRAFICO_nao_some_ao_reavaliar(tmp_path):
    """A queixa (a), com a run real: o gráfico e os níveis continuam na tela
    enquanto a análise nova roda — e o progresso aparece do lado, não no lugar."""
    rec, it = _sobe(tmp_path)
    base_url = next(it)
    enviados = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport=DESKTOP)
            _abre(page, base_url, rec, enviados, degrada_para="1d")
            antes = page.evaluate(_ESTADO)
            assert antes["graficoNaTela"], "o teste precisa começar com gráfico na tela"

            page.evaluate("() => document.getElementById('reevalBtn').click()")
            page.wait_for_timeout(400)
            m = page.evaluate(_ESTADO)
            assert m["painelEscondido"] is False, ("o painel foi escondido", m)
            assert m["graficoNaTela"] is True, ("o gráfico sumiu", m)
            assert m["progressoVisivel"] is True, ("sem progresso não se sabe que roda", m)
            browser.close()
    finally:
        with contextlib.suppress(StopIteration):
            next(it)


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.skipif(not _REC.exists(), reason="a run real não está neste disco")
def test_o_que_fica_na_tela_SE_DECLARA_como_leitura_anterior(tmp_path):
    """DENTE do exagero oposto: preservar a leitura sem dizer que ela é a ANTERIOR
    faria a tela apresentar o resultado velho como se fosse o da reavaliação. Usa a
    MESMA marca da revalidação automática — não um aviso novo."""
    rec, it = _sobe(tmp_path)
    base_url = next(it)
    enviados = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport=DESKTOP)
            _abre(page, base_url, rec, enviados, degrada_para="1d")
            assert page.evaluate(_ESTADO)["revalidando"] is False
            page.evaluate("() => document.getElementById('reevalBtn').click()")
            page.wait_for_timeout(400)
            assert page.evaluate(_ESTADO)["revalidando"] is True, "não se declarou"
            browser.close()
    finally:
        with contextlib.suppress(StopIteration):
            next(it)


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.skipif(not _REC.exists(), reason="a run real não está neste disco")
def test_a_marca_SAI_quando_a_reavaliacao_nem_comeca(tmp_path):
    """Sem isto, um POST que falha deixaria o gráfico anunciando para sempre um
    recálculo em curso."""
    rec, it = _sobe(tmp_path)
    base_url = next(it)
    enviados = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport=DESKTOP)
            _abre(page, base_url, rec, enviados, degrada_para="1d", trava_analyze=False)
            page.evaluate("() => document.getElementById('reevalBtn').click()")
            page.wait_for_timeout(600)
            m = page.evaluate(_ESTADO)
            assert m["revalidando"] is False, ("marca presa depois da falha", m)
            assert m["graficoNaTela"] is True, m
            browser.close()
    finally:
        with contextlib.suppress(StopIteration):
            next(it)


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.skipif(not _REC.exists(), reason="a run real não está neste disco")
@pytest.mark.parametrize("degrada", [None, "1d"],
                         ids=["frame_atendido", "backend_degrada_o_frame"])
def test_ROTULO_MENSAGEM_e_FRAME_ENVIADO_dizem_o_MESMO(tmp_path, degrada):
    """A queixa (b), travada como invariante. Os três saem do mesmo frame — e no
    caminho em que o backend DEGRADA o pedido, os três passam a dizer "Diário"
    juntos, que é o comportamento correto (a tela não promete o frame que não tem)."""
    rec, it = _sobe(tmp_path)
    base_url = next(it)
    enviados = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport=DESKTOP)
            _abre(page, base_url, rec, enviados, degrada_para=degrada)
            antes = page.evaluate(_ESTADO)
            # ÂNCORA: sem botão habilitado não há clique, e o teste passaria sem
            # exercitar nada — o veredito desta run é no 1h, por isso o 4h.
            assert not antes["desabilitado"], ("o botão precisa estar ativo", antes)
            page.evaluate("() => document.getElementById('reevalBtn').click()")
            page.wait_for_timeout(400)
            m = page.evaluate(_ESTADO)
            assert enviados, "nada foi enviado"
            enviado = enviados[-1]["timeframe"]
            nome = page.evaluate("(tf) => tfNome(tf)", enviado)
            assert nome in antes["rotulo"], (
                "o rótulo do botão promete um frame e o POST manda outro",
                antes["rotulo"], enviado)
            assert nome in m["progresso"], (
                "a mensagem nomeia um frame e o POST manda outro",
                m["progresso"][:120], enviado)
            browser.close()
    finally:
        with contextlib.suppress(StopIteration):
            next(it)

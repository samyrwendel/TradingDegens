"""O gráfico não some, não pisca, e nunca mente sobre o frame (task 20260831-018).

Três queixas do Samyr, uma raiz só — ``renderChartCard``:

    if (!hasData) { card.classList.add("hidden"); cv._chart = null; return; }
    cv._view = null; cv._vview = null;      // a cada pintura

1. **"às vezes mudo o timeframe e o gráfico não muda"** — o frame pedido voltava
   sem velas, o card sumia, ``_tf`` já valia o frame novo, e o clique SEGUINTE no
   mesmo frame caía no ``if (tf === _tf) return`` e não fazia nada. O clique morria
   em silêncio, e não havia mensagem nenhuma dizendo por quê.
2. **"não precisa apagar o gráfico, apenas atualiza sem piscar"** — a vista
   (zoom/pan) era zerada em TODA pintura, inclusive quando o assunto era o mesmo
   ativo no mesmo frame, ou seja a cada revalidação.
3. **revalidação automática por fechamento de candle** — não existia.

*(Sobre a pista de "JSON inválido do /api/chart": ela não se sustentou. O erro
"Expecting property name enclosed in double quotes: line 2 column 3" é reproduzido
ao pé da letra pelo compressor de saída do `rtk` sobre o `curl`, não pelo servidor
— o mesmo comando com `/usr/bin/curl` devolve JSON válido. O que estes testes
travam é o defeito de verdade, que é o de cima e é determinístico.)*

O dente de cada um está no nome do teste. Todos usam a fixture compartilhada do
``test_webui_frame_e_cor_e2e`` — o mesmo ativo, os mesmos planos por frame.
"""

import json
import re

import pytest

from tests.test_webui_frame_e_cor_e2e import (
    _ACT_1H,
    _ACT_4H,
    _ACT_D,
    _CHART,
    DESKTOP,
    _abre,
    sobe_servidor,
    sync_playwright,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def base(tmp_path):
    yield from sobe_servidor(tmp_path)


_ESTADO = """() => ({
  cardEscondido: document.getElementById('chartCard').classList.contains('hidden'),
  temChart: !!document.getElementById('priceChart')._chart,
  tfDesenhado: document.getElementById('priceChart')._tf || null,
  tf: _tf,
  tfDesenhadoVar: _tfDesenhado,
  aviso: document.getElementById('chartFrameAviso').textContent,
  avisoVisivel: !document.getElementById('chartFrameAviso').classList.contains('hidden'),
  ativo: (document.querySelector('.tf-btn.is-active') || {}).dataset,
  revalidando: document.getElementById('chartCard').classList.contains('is-revalidando'),
})"""


def _rota_sem_velas(page, tf_vazio):
    """Faz o /api/chart de UM frame voltar sem velas — a fonte intradiária caída."""
    def handler(route):
        url = route.request.url
        tf = (re.search(r"[?&]tf=([^&]+)", url) or [None, "1d"])[1]
        plano = {"1h": _ACT_1H, "1d": _ACT_D}.get(tf, _ACT_4H)
        chart = {} if tf == tf_vazio else {**_CHART, "timeframe": tf}
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "timeframe": tf, "timeframes": ["1w", "1d", "4h", "1h", "15m"],
            "actionable": plano, "price_chart": chart, "degraded": []}))
    page.unroute(re.compile(r"/api/chart"))
    page.route(re.compile(r"/api/chart"), handler)


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_frame_sem_velas_NAO_apaga_o_grafico_e_a_tela_diz_de_qual_frame_ele_e(base):
    """DENTE: antes o card sumia inteiro e a tela ficava sem gráfico nenhum."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        antes = page.evaluate(_ESTADO)
        assert antes["temChart"] and not antes["cardEscondido"]

        _rota_sem_velas(page, "1h")
        page.click('.tf-btn[data-tf="1h"]')
        page.wait_for_function("""() => _tf === '1h'""", timeout=10000)
        page.wait_for_timeout(150)

        m = page.evaluate(_ESTADO)
        assert not m["cardEscondido"], ("o gráfico anterior foi apagado", m)
        assert m["temChart"], m
        assert m["tfDesenhadoVar"] == "4h", ("o desenho continua sendo o do 4h", m)
        # e a tela DECLARA isso — sem a linha ela afirmaria o 1h mostrando o 4h
        assert m["avisoVisivel"], ("a tela não disse de qual frame é o desenho", m)
        assert "4h" in m["aviso"] and "1h" in m["aviso"], m
        # e a nota do gráfico volta a descrever o DESENHO: ela não pode continuar
        # afirmando um recálculo que já terminou.
        assert "Recalculando" not in page.inner_text("#chartNote"), page.inner_text("#chartNote")
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_reclicar_o_MESMO_frame_tenta_de_novo_quando_a_tela_nao_esta_nele(base):
    """O clique perdido: era `if (tf === _tf) return` sobre um frame não desenhado.

    DENTE: na implementação antiga o segundo clique não gerava NENHUM pedido —
    era exatamente o "às vezes mudo o timeframe e o gráfico não muda".
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        _rota_sem_velas(page, "1h")
        page.click('.tf-btn[data-tf="1h"]')
        page.wait_for_function("""() => _tf === '1h'""", timeout=10000)

        page.evaluate("""() => {
          window.__pedidos = 0;
          const o = window.fetch;
          window.fetch = (u, x) => { if (String(u).includes('/api/chart')) window.__pedidos++;
                                     return o(u, x); };
        }""")
        page.click('.tf-btn[data-tf="1h"]')          # o MESMO frame, de novo
        page.wait_for_timeout(400)
        assert page.evaluate("() => window.__pedidos") >= 1, "o reclique não tentou de novo"

        # e quando a fonte volta, o reclique PEGA
        page.unroute(re.compile(r"/api/chart"))
        _rota_sem_velas(page, "nenhum")
        page.click('.tf-btn[data-tf="1h"]')
        page.wait_for_function("""() => _tfDesenhado === '1h'""", timeout=10000)
        m = page.evaluate(_ESTADO)
        assert not m["avisoVisivel"], ("desenhou o frame pedido: o aviso sai", m)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_falha_de_carga_DIZ_o_que_houve_em_vez_de_a_tela_fingir_que_nada_aconteceu(base):
    """DENTE: o catch escrevia "Falha ao recalcular timeframe." e engolia a causa."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        page.unroute(re.compile(r"/api/chart"))
        page.route(re.compile(r"/api/chart"), lambda r: r.fulfill(
            status=500, content_type="application/json",
            body=json.dumps({"error": "fonte intradiária fora do ar"})))

        page.click('.tf-btn[data-tf="1h"]')
        page.wait_for_selector("#chartFrameAviso:not(.hidden)", timeout=10000)
        m = page.evaluate(_ESTADO)
        assert "fonte intradiária fora do ar" in m["aviso"], ("a CAUSA não foi pra tela", m)
        assert "4h" in m["aviso"], ("a tela tem de dizer onde ela continua", m)
        # e o frame ATIVO continua sendo o desenhado — a tela não mente
        assert m["ativo"]["tf"] == "4h", m
        assert m["temChart"] and not m["cardEscondido"], m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_revalidar_atualiza_EM_LUGAR_sem_destruir_o_canvas_nem_o_zoom(base):
    """A queixa do "piscar": a vista era zerada em toda pintura.

    DENTE duplo: (a) o elemento canvas é o MESMO objeto depois da revalidação (não
    foi destruído e recriado); (b) o zoom que o usuário ajustou sobrevive — antes
    `cv._view = null` rodava em toda pintura, inclusive na do mesmo frame.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        page.evaluate("""() => {
          const cv = document.getElementById('priceChart');
          cv.__marca = 'sou-o-mesmo';
          cv._view = { i0: 10, i1: 60 };      // o usuário deu zoom
        }""")
        page.evaluate("() => revalidaFrame('teste')")
        page.wait_for_function("""() => !document.getElementById('chartCard')
                                           .classList.contains('is-revalidando')""",
                               timeout=10000)
        m = page.evaluate("""() => {
          const cv = document.getElementById('priceChart');
          return {marca: cv.__marca, view: cv._view, temChart: !!cv._chart,
                  escondido: document.getElementById('chartCard').classList.contains('hidden')};
        }""")
        assert m["marca"] == "sou-o-mesmo", ("o canvas foi destruído e recriado", m)
        assert m["view"] == {"i0": 10, "i1": 60}, ("a revalidação zerou o zoom", m)
        assert m["temChart"] and not m["escondido"], m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_trocar_de_frame_REINICIA_a_vista_porque_o_assunto_mudou(base):
    """O contrário do teste acima, e o motivo de ele não ser "nunca zerar".

    Outro frame é outro conjunto de velas: manter a janela de zoom do 4h no 1h
    mostraria um pedaço arbitrário da série nova. A vista só sobrevive quando o
    ASSUNTO (ativo + frame) é o mesmo.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        page.evaluate("() => { document.getElementById('priceChart')._view = {i0: 10, i1: 60}; }")
        page.click('.tf-btn[data-tf="1h"]')
        page.wait_for_function("""() => _tfDesenhado === '1h'""", timeout=10000)
        assert page.evaluate("() => document.getElementById('priceChart')._view") is None
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_revalidacao_automatica_e_agendada_pelo_RELOGIO_DO_SERVIDOR(base):
    """A tela pergunta quando o candle fecha; não recalcula o horário sozinha.

    DENTE: se alguém trocar isto por um `setInterval` de N minutos em JavaScript,
    o pedido ao `/api/agenda/proxima` some e o teste cai — que é o ponto, porque
    aí passariam a existir dois relógios (este e o da passada agendada do scan).
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        # `add_init_script` porque `_abre` navega: um wrapper instalado por
        # `evaluate` morreria no goto seguinte.
        page.add_init_script("""
          window.__agenda = [];
          window.__fetchOriginal = window.fetch;
          window.fetch = function (u, x) {
            var s = String(u);
            if (s.indexOf('/api/agenda/proxima') >= 0) window.__agenda.push(s);
            return window.__fetchOriginal(u, x);
          };
        """)
        _abre(page, base)
        page.wait_for_function("() => window.__agenda.length > 0", timeout=10000)
        pedidos = page.evaluate("() => window.__agenda")
        assert any("tf=" in u for u in pedidos), pedidos

        # trocar de frame reagenda pro candle do frame NOVO
        page.click('.tf-btn[data-tf="1h"]')
        page.wait_for_function("""() => window.__agenda.some(u => u.includes('tf=1h'))""",
                               timeout=10000)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_revalidacao_automatica_nao_se_empilha(base):
    """Uma resposta lenta não pode gerar fila de recargas do mesmo frame."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        page.evaluate("""() => {
          window.__n = 0;
          const o = window.fetch;
          window.fetch = async (u, x) => {
            if (String(u).includes('/api/chart')) {
              window.__n++;
              await new Promise((r) => setTimeout(r, 400));
            }
            return o(u, x);
          };
        }""")
        page.evaluate("""() => { revalidaFrame('a'); revalidaFrame('b'); revalidaFrame('c'); }""")
        page.wait_for_timeout(700)
        assert page.evaluate("() => window.__n") == 1, "revalidações empilharam"
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_pregao_fechado_nao_agenda_revalidacao_de_acao(base):
    """Fora do pregão a ação repete o mesmo candle: revalidar é chamada sem informação.

    A regra é a do servidor (`agenda.alvos_da_passada`), respondida em `revalida`;
    a tela só obedece.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        page.goto(base, wait_until="load")
        page.route(re.compile(r"/api/agenda/proxima"), lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"tf": "4h", "cadencia_min": 240, "em_segundos": 5,
                             "sessao": "fechada", "revalida": False})))
        _abre(page, base)
        page.wait_for_timeout(300)
        assert page.evaluate("() => !!_revalTimer") is False, "agendou com pregão fechado"
        browser.close()

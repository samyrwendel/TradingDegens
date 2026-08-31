"""A troca de timeframe é ATÔMICA — nunca chip num frame e níveis noutro (007, P0).

Três prints do MESMO ativo, no mesmo minuto (ação, 465,58/465,60, análise de 28/08,
1-2-3 de venda):

  A — chip **D** selecionado, botão "Reavaliar veredito no Diário", **mas** o carimbo
      dentro do gráfico diz "4h" e os níveis são os do 4h: SL 497,59 · invalidação
      491,12 · alvo 437,23 · R:R 0,30.
  B — chip **4h**, carimbo "4h", os MESMOS níveis do A. Coerente.
  C — chip **D**, carimbo "Diário", níveis completamente outros: SL 526,92 ·
      invalidação 517,35 · alvo 460,21 · R:R 0,05.

A e C são o MESMO frame e mostram planos diferentes. **A é a janela de transição:**
``switchTimeframe`` movia ``_tf`` e repintava o seletor no clique, e só depois — ao
fim do ``await`` do ``/api/chart`` — trocava gráfico e cards. Nesse intervalo a tela
afirmava algo falso, e não é cosmético: quem olhasse ali leria **SL 497,59 como se
fosse o do diário, quando o do diário é 526,92**. Trinta pontos num nível que se opera.

O que estes testes travam:
  (a) durante a troca, chip ATIVO, carimbo do gráfico e níveis do card dizem todos o
      mesmo frame — o clicado fica PENDENTE, marcado, e o realce só se move quando os
      níveis chegam;
  (b) resposta ATRASADA de uma troca superada não pinta nada (clicar D e logo 1h não
      pode terminar no diário);
  (c) a COTAÇÃO não some ao trocar de frame — ela é do ativo agora, não do frame;
  (d) o caminho de ERRO não deixa a tela num frame que não está desenhado.

A janela de transição é REPRODUZIDA (``window.fetch`` embrulhado com atraso), não
esperada por sorte — era exatamente o que o print A pegou e o que nenhum teste via.
"""

import pytest

from tests.test_webui_frame_e_cor_e2e import (
    DESKTOP,
    TELEFONE,
    _abre,
    sobe_servidor,
    sync_playwright,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def base(tmp_path):
    yield from sobe_servidor(tmp_path)

# Atrasa a RESOLUÇÃO do /api/chart dentro da página: pro app é uma resposta lenta, e
# a janela de transição fica larga o bastante pra ser medida em vez de perseguida.
_ATRASA = """(ms) => {
  const orig = window.fetch;
  window.__atraso = ms;
  window.fetch = async (u, o) => {
    const r = await orig(u, o);
    const url = String(u);
    if (url.includes('/api/chart')) {
      const m = url.match(/[?&]tf=([^&]+)/);
      const espera = (window.__atrasoPorTf && window.__atrasoPorTf[m && m[1]]) ?? window.__atraso;
      await new Promise((res) => setTimeout(res, espera));
    }
    return r;
  };
}"""

# O retrato do que a tela AFIRMA num instante: chip ativo, chip pendente, carimbo
# desenhado no gráfico e o stop que está escrito no card. Os três primeiros têm de
# concordar, e o quarto é o número que o usuário leria e operaria.
_RETRATO = """() => {
  const chip = (s) => { const e = document.querySelector(s);
    return e ? e.dataset.tf : null; };
  const cards = document.getElementById('setupCards');
  const txt = cards ? cards.innerText : '';
  const m = txt.match(/stop \\(SL\\)\\s*([\\d.,]+)/);
  return {
    ativo: chip('.tf-btn.is-active'),
    pendente: chip('.tf-btn.is-pendente'),
    carimbo: (document.getElementById('priceChart').dataset.tf || ''),
    stop: m ? m[1] : '',
    tf: _tf, pend: _tfPendente,
  };
}"""

# Os stops de cada frame nas fixtures — a discordância REAL entre planos, que é o
# que torna a incoerência perigosa em vez de feia.
STOP = {"4h": "176,83", "1h": "207,00", "1d": "175,09"}
CARIMBO = {"4h": "4h", "1h": "1h", "1d": "Diário"}


def _coerente(r, frame):
    """O par (o que a tela diz que é) × (o que ela mostra) bate?"""
    return (r["ativo"] == frame
            and CARIMBO[frame].lower() in r["carimbo"].lower()
            and r["stop"] == STOP[frame])


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [DESKTOP, TELEFONE], ids=["desktop", "telefone"])
def test_durante_a_troca_o_chip_ativo_concorda_com_os_niveis(base, viewport):
    """DENTE, o print A: chip no "D" com carimbo "4h" e o stop do 4h na tela. Mede-se
    DENTRO da janela de transição, várias vezes, e em nenhuma amostra o que a tela
    afirma pode discordar do que ela mostra."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre(page, base, viewport=viewport)
        page.evaluate(_ATRASA, 1500)

        antes = page.evaluate(_RETRATO)
        assert _coerente(antes, "4h"), antes

        page.click('.tf-btn[data-tf="1d"]')
        # três amostras DENTRO do atraso — a janela que o print A pegou
        for _ in range(3):
            page.wait_for_timeout(250)
            r = page.evaluate(_RETRATO)
            assert _coerente(r, "4h"), (
                "a tela afirma um frame e mostra os níveis de outro", r)
            assert r["pendente"] == "1d", ("o clique tem de se marcar, e como "
                                           "PENDENTE — não como ativo", r)

        page.wait_for_function("() => _tf === '1d' && _tfPendente === null")
        page.wait_for_timeout(150)
        depois = page.evaluate(_RETRATO)
        assert _coerente(depois, "1d"), depois
        assert depois["pendente"] is None, depois
        # e o número mudou de verdade: são planos diferentes, que é o motivo do P0
        assert antes["stop"] != depois["stop"], (antes, depois)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_clique_nao_se_perde_enquanto_carrega(base):
    """O outro lado: atomicidade não pode virar botão morto. O clicado se marca na
    hora, com o motivo no ``title``, e o gráfico diz que está recalculando."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        page.evaluate(_ATRASA, 1200)
        page.click('.tf-btn[data-tf="1h"]')
        page.wait_for_timeout(200)
        m = page.evaluate("""() => {
          const p = document.querySelector('.tf-btn.is-pendente');
          const cs = p ? getComputedStyle(p) : {};
          const a = document.querySelector('.tf-btn.is-active');
          return {qual: p ? p.dataset.tf : null, title: p ? p.title : '',
                  nota: document.getElementById('chartNote').innerText,
                  decor: cs.textDecorationLine, corPend: cs.color,
                  corAtivo: a ? getComputedStyle(a).color : ''};
        }""")
        assert m["qual"] == "1h", m
        assert "recalculando" in m["title"].lower(), m
        assert "ainda são do frame atual" in m["title"], ("o title diz a verdade "
                                                          "sobre o que está na tela", m)
        assert "recalculando" in m["nota"].lower(), m
        assert "underline" in m["decor"], ("o pendente precisa de marca própria", m)
        assert m["corPend"] != m["corAtivo"], ("pendente não pode se vestir de ativo", m)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_resposta_atrasada_de_troca_superada_nao_pinta_nada(base):
    """A mesma incoerência por outra porta: clicar D (lento) e logo 1h (rápido). Sem
    selo de pedido, a resposta do D chegava depois e pintava o diário por cima do 1h
    já escolhido — e aí sim o chip e os níveis discordariam de forma PERMANENTE."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        page.evaluate("() => { window.__atrasoPorTf = {'1d': 1500, '1h': 50}; }")
        page.evaluate(_ATRASA, 50)

        page.click('.tf-btn[data-tf="1d"]')
        page.wait_for_timeout(120)
        page.click('.tf-btn[data-tf="1h"]')
        page.wait_for_function("() => _tf === '1h'")
        page.wait_for_timeout(1800)     # tempo de sobra pra resposta do D chegar

        r = page.evaluate(_RETRATO)
        assert _coerente(r, "1h"), ("a resposta velha pintou por cima da nova", r)
        assert r["pendente"] is None, r
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_cotacao_nao_some_ao_trocar_de_frame(base):
    """DENTE: "ÚLTIMO FECHAMENTO 465,58 28/08 16:00" aparecia num print e sumia nos
    outros. A cotação é do ATIVO AGORA, não do frame — o ``/api/chart`` não a
    devolve, e passar ``undefined`` apagava a unidade da tira."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        ler = """() => {
          const live = document.querySelector('#headPrice .hp-live');
          return {tem: !!live, txt: live ? live.innerText.replace(/\\s+/g, ' ') : ''};
        }"""
        antes = page.evaluate(ler)
        assert antes["tem"] and "218,40" in antes["txt"], antes
        for tf in ("1h", "1d", "4h"):
            page.click(f'.tf-btn[data-tf="{tf}"]')
            page.wait_for_function(f"() => _tf === '{tf}' && _tfPendente === null")
            page.wait_for_timeout(120)
            agora = page.evaluate(ler)
            assert agora["tem"], (f"a cotação sumiu no {tf}", agora)
            assert agora["txt"] == antes["txt"], (f"e mudou no {tf}", agora, antes)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_falha_no_recalculo_deixa_a_tela_no_frame_que_esta_desenhado(base):
    """Caminho de erro: se o recálculo falha, o realce tem de continuar no frame que
    está DESENHADO — voltar pra um frame que não está na tela é o mesmo defeito."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        page.route("**/api/chart**", lambda route: route.fulfill(
            status=500, content_type="application/json",
            body='{"error": "fonte fora do ar"}'))
        page.click('.tf-btn[data-tf="1d"]')
        page.wait_for_function("() => _tfPendente === null")
        page.wait_for_timeout(150)
        r = page.evaluate(_RETRATO)
        assert _coerente(r, "4h"), ("a falha deixou a tela dizendo outro frame", r)
        # A CAUSA mudou de lugar (DA-118): saiu da nota do gráfico — que descreve o
        # DESENHO e voltou a descrevê-lo — e passou a ter linha própria, junto do
        # "a tela continua no 4h". O que este teste garante é o mesmo: o erro FALA.
        aviso = page.inner_text("#chartFrameAviso")
        assert "fonte fora do ar" in aviso, aviso
        assert "4h" in aviso, ("a tela tem de dizer onde ela continua", aviso)
        assert "Recalculando" not in page.inner_text("#chartNote"), \
            ("a nota ficou afirmando um recálculo que já terminou", page.inner_text("#chartNote"))
        browser.close()


# ── a leitura do Storm sobrevive à troca de frame (o outro lado da mesma task) ──
_STORM = {"opera": True, "qualidade": "boa", "veto": None,
          "motivo": "MME 8 acima da MME 80 — o Éden autoriza a compra",
          "eden": {"disponivel": True, "ema_rapida": 212.4, "ema_lenta": 198.1,
                   "motivo": "rápida acima da lenta"},
          "pattern": {"p1": {"date": "2026-08-20", "price": 210.0},
                      "p2": {"date": "2026-08-21", "price": 201.0},
                      "p3": {"date": "2026-08-22", "price": 206.0},
                      "direction": "compra", "amplitude": 9.0},
          "invalidation": {"price": 201.0, "meaning": "perde o ponto 2"},
          "stop": {"price": 201.0, "basis": "no ponto 2", "slack": 0.0},
          "leituras": [{"entrada": "ponto3", "ordem": "antecipada", "trigger": 206.5,
                        "state": "formando", "state_label": "em formação",
                        "label": "rompimento da máxima do ponto 3",
                        "target": {"price": 215.5, "label": "amplitude projetada"},
                        "risk_reward": {"entry": 206.5, "entry_basis": "gatilho",
                                        "risk": 5.5, "reward": 9.0, "rr": 1.64,
                                        "note": None}}]}


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_card_do_storm_nao_some_ao_trocar_de_frame(base):
    """DENTE, e é o que os prints A e B mostravam sem que ninguém tivesse concluído:
    os DOIS são o 4h, e só um tem "Storm · stop (SL)". O print A veio do render da
    run (o worker anexa `actionable["storm"]`); o B veio do `/api/chart`, que montava
    o plano SEM ele. Trocar de frame numa run do Storm apagava a leitura inteira —
    card do veto do Éden, as duas entradas e as linhas do gráfico.

    O stub segue o contrato do endpoint CORRIGIDO: manda `storm` quando o método é
    storm123, e não manda quando não é.
    """
    import json as _json
    import re as _re

    from tests.test_webui_frame_e_cor_e2e import _ACT_1H, _ACT_4H, _CHART, _snap

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE)
        snap = _snap({**_ACT_4H, "storm": _STORM})
        snap["result"]["setup123"] = False
        snap["result"]["storm123"] = True

        def handler(route):
            url = route.request.url
            if "/api/chart" in url:
                tf = (_re.search(r"[?&]tf=([^&]+)", url) or [None, "1d"])[1]
                metodo = (_re.search(r"[?&]method=([^&]+)", url) or [None, ""])[1]
                plano = dict({"1h": _ACT_1H}.get(tf, _ACT_4H))
                if metodo.startswith("storm"):
                    plano["storm"] = _STORM
                route.fulfill(status=200, content_type="application/json",
                              body=_json.dumps({
                                  "timeframe": tf, "actionable": plano,
                                  "timeframes": ["1w", "1d", "4h", "1h", "15m"],
                                  "price_chart": {**_CHART, "timeframe": tf},
                                  "degraded": []}))
            elif "/api/status/" in url or _re.search(r"/api/run/[^/]+$", url):
                route.fulfill(status=200, content_type="application/json",
                              body=_json.dumps(snap))
            else:
                route.continue_()
        page.route(_re.compile(r"/api/"), handler)
        page.goto(base, wait_until="networkidle")
        page.evaluate("() => watchRun('R-006')")
        page.wait_for_selector("#setupCards:not(.hidden)")
        page.wait_for_timeout(200)

        antes = page.evaluate("""() => ({
          metodo: _openMethod,
          cards: [...document.querySelectorAll('#setupCards .sc-title')].map(e => e.innerText.trim()),
          linhas: planZones(document.getElementById('priceChart')._actionable || {})
                    .map(z => z.tag).filter(t => /Storm/.test(t)),
        })""")
        assert antes["metodo"] == "storm123", antes
        assert any("Storm123" in t for t in antes["cards"]), antes

        page.click('.tf-btn[data-tf="1h"]')
        page.wait_for_function("() => _tf === '1h' && _tfPendente === null")
        page.wait_for_timeout(200)
        depois = page.evaluate("""() => ({
          cards: [...document.querySelectorAll('#setupCards .sc-title')].map(e => e.innerText.trim()),
          linhas: planZones(document.getElementById('priceChart')._actionable || {})
                    .map(z => z.tag).filter(t => /Storm/.test(t)),
          txt: document.getElementById('setupCards').innerText,
        })""")
        assert any("Storm123" in t for t in depois["cards"]), (
            "a leitura do Storm sumiu ao trocar de frame", depois)
        assert depois["linhas"], ("e as linhas do Storm sumiram do gráfico", depois)
        assert "Éden" in depois["txt"], ("o filtro que AUTORIZA tem de continuar à vista",
                                         depois["txt"][:200])
        browser.close()

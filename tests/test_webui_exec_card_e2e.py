"""O CARD DE EXECUÇÃO na tela (task 20260830-012).

O print que abriu a task — CRWD no desktop — mostra nove faixas de três famílias e
**nenhuma frase dizendo o que FAZER com elas**. Este card é a frase.

O que se mede aqui:

  * o VEREDITO de oportunidade como manchete, com a razão escrita;
  * as ordens na SEQUÊNCIA em que se digitam, cada uma com a base do seu preço;
  * a invalidação, a política de saída e o peso — este sempre RELATIVO;
  * BE e trailing visíveis e DESLIGADOS, com o porquê;
  * o índice de confiabilidade com o gate de N: com amostra pequena a tela diz que
    não há amostra, em vez de exibir uma taxa que engana.

Desktop e 390×844 (DA-062) — o card é longo e é no telefone que ele tem de caber.
"""

import json
import re

import pytest

from tests.test_webui_frame_e_cor_e2e import DESKTOP, TELEFONE, sobe_servidor
from tests.test_webui_um_grafico_um_metodo_e2e import _CHART, _snap

pytestmark = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None


@pytest.fixture
def base(tmp_path):
    yield from sobe_servidor(tmp_path)


# O CRWD do print: preço colado no gatilho p3, dois alvos, faixa de recuo publicada.
_PLANO_CRWD = {
    "symbol": "CRWD", "price": 218.40, "as_of": "2026-08-28 17:30",
    "timeframe": "diário (referência)", "horizon": "dias",
    "setup_state": "aguardar_rompimento", "setup_source": "123",
    "pattern": {"p1": {"date": "2026-08-10", "price": 200.0},
                "p2": {"date": "2026-08-18", "price": 228.49},
                "p3": {"date": "2026-08-25", "price": 210.0},
                "trigger": 218.56, "state": "formando", "direction": "compra"},
    "invalidation": {"price": 181.32, "meaning": "o setup morre se perder 181,32"},
    "stop": {"label": "stop (SL)", "price": 210.53, "anchor": 181.32, "atr": 9.8,
             "basis": "invalidação + folga de 0.5·ATR14"},
    "target": {"label": "topo anterior 2026-07-02", "price": 237.11,
               "same_as_realize": False},
    "realize_zone": {"price": 219.35, "role": "alvo", "role_label": "realização parcial",
                     "label": "resistência acima"},
    "buy_zone": {"label": "MMS20", "price": 211.27, "low": 208.0, "high": 214.0,
                 "ma_label": "MMS20", "setup": "recuo_media",
                 "tag": "recuo à média (MMS20)", "active_now": False,
                 "distance_pct": 3.4, "band_basis": "±0.5·ATR14"},
    "pullback_zone": None,
    "risk_reward": {"entry": 218.56, "entry_basis": "gatilho", "risk": 8.03,
                    "reward": 18.55, "rr": 2.31, "note": None},
}


def _card(plano=None, por_setup=None):
    from tradingagents.webui import execucao

    return {"ticker": "CRWD", "date": "2026-08-29", "timeframe": "1d",
            "method": "setup123",
            "card": execucao.card(plano if plano is not None else _PLANO_CRWD,
                                  por_setup or {})}


def _abre(page, base_url, plano=None, por_setup=None):
    """A run e o /api/execucao respondendo o card computado pelo MÓDULO REAL — o que
    se mede aqui é o desenho, e usar o módulo evita um card de mentira que passaria
    num teste e não existiria na tela."""
    snap = _snap("setup123")
    snap["result"]["actionable"] = plano if plano is not None else _PLANO_CRWD
    snap["result"]["price_chart"] = dict(_CHART, timeframe="1d")
    carta = _card(plano, por_setup)

    def handler(route):
        url = route.request.url
        if "/api/execucao" in url:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(carta))
        elif "/api/status/" in url or re.search(r"/api/run/[^/]+$", url):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(snap))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)
    page.goto(base_url, wait_until="networkidle")
    page.evaluate("() => watchRun('R-009')")
    page.wait_for_selector("#execCard:not(.hidden)")
    page.wait_for_timeout(200)


_LE = """() => {
  const el = document.getElementById('execCard');
  return {
    txt: el.innerText,
    veredito: (el.querySelector('.ex-vered') || {}).innerText || '',
    vClass: (el.querySelector('.ex-vered') || {}).className || '',
    motivo: (el.querySelector('.ex-motivo') || {}).innerText || '',
    ordens: [...el.querySelectorAll('.ex-ordem')].map(o => ({
      passo: (o.querySelector('.ex-passo') || {}).innerText || '',
      tipo: (o.querySelector('.ex-tipo') || {}).innerText || '',
      papel: (o.querySelector('.ex-papel') || {}).innerText || '',
      preco: (o.querySelector('.ex-preco') || {}).innerText || '',
      fracao: (o.querySelector('.ex-fracao') || {}).innerText || '',
    })),
    protecao: [...el.querySelectorAll('.ex-prot')].map(p => ({
      nome: (p.querySelector('.ex-k') || {}).innerText || '',
      estado: (p.querySelector('.ex-estado') || {}).innerText || '',
    })),
    conf: [...el.querySelectorAll('.ex-conf')].map(c => c.innerText.replace(/\\s+/g, ' ')),
  };
}"""


# ───────────────────────── o card responde a pergunta ──────────────────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [DESKTOP, TELEFONE], ids=["desktop", "telefone"])
def test_o_card_diz_o_que_fazer_e_nao_so_o_que_o_preco_esta_fazendo(base, viewport):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre(page, base)
        m = page.evaluate(_LE)
        assert "ENTRAR AGORA" in m["veredito"].upper(), m
        assert "ok" in m["vClass"], ("só o 'pode ir' é verde", m["vClass"])
        assert "no ponto de entrada" in m["motivo"], m["motivo"]
        assert "218,56" in m["motivo"] and "218.56" not in m["motivo"], (
            "o card fala pt-BR como o resto da tela", m["motivo"])
        # as ordens, na sequência de digitar
        assert [o["passo"] for o in m["ordens"]] == ["1", "2", "3", "4"], m["ordens"]
        assert m["ordens"][0]["papel"] == "entrada", m["ordens"]
        assert "218,56" in m["ordens"][0]["preco"], m["ordens"]
        assert m["ordens"][1]["papel"] == "stop (SL)" and "210,53" in m["ordens"][1]["preco"]
        assert m["ordens"][2]["fracao"].lower() == "grosso", m["ordens"]
        assert m["ordens"][3]["fracao"].lower() == "resíduo", m["ordens"]
        # invalidação, saída e peso
        assert "181,32" in m["txt"], m["txt"]
        assert "invalida em" in m["txt"], m["txt"]
        assert "inicial" in m["txt"], ("o peso é relativo", m["txt"])
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_esticado_manda_AGUARDAR_RECUO_e_poe_a_ordem_na_faixa(base):
    """A pergunta literal: "ou se é pra aguardar recuo até faixa tal". A faixa é a
    que o painel já imprime, e a ORDEM DE ENTRADA vai pra ela."""
    esticado = {**_PLANO_CRWD, "price": 230.0,
                "pattern": {**_PLANO_CRWD["pattern"], "state": "acionado"},
                "risk_reward": {"entry": 230.0, "entry_basis": "preço atual",
                                "risk": 19.47, "reward": 7.11, "rr": 0.37, "note": None,
                                "no_gatilho": {"entry": 218.56, "risk": 8.03,
                                               "reward": 18.55, "rr": 2.31},
                                "andado_pct": 61.6, "sobra_pct": 38.4}}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE)
        _abre(page, base, esticado)
        m = page.evaluate(_LE)
        assert "AGUARDAR RECUO ATÉ MMS20" in m["veredito"].upper(), m["veredito"]
        assert "espera" in m["vClass"] and "ok" not in m["vClass"], (
            "aguardar não pode se vestir de verde", m["vClass"])
        assert "ESTICADO" in m["motivo"], m["motivo"]
        assert "211,27" in m["ordens"][0]["preco"], ("a ordem vai pra faixa", m["ordens"])
        assert "recuo" in m["txt"], m["txt"]
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_passar_nao_imprime_onde_comprar(base):
    """Um card que diz PASSAR e mesmo assim mostra um preço de entrada se contradiz —
    e o número é o que fica na cabeça de quem lê."""
    morto = {**_PLANO_CRWD, "price": 170.0}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, morto)
        m = page.evaluate(_LE)
        assert "PASSAR" in m["veredito"].upper(), m
        assert not any(o["papel"] == "entrada" for o in m["ordens"]), m["ordens"]
        assert "invalidação" in m["motivo"], m["motivo"]
        browser.close()


# ─────────────────────── BE e trailing: visíveis e off ─────────────────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_be_e_trailing_aparecem_DESLIGADOS_com_o_porque(base):
    """Ele pediu "um trailing stop que pode ser habilitado" — então o estado tem de
    estar VISÍVEL. E desligado por default não é omissão: o método compra o recuo à
    média, e um trailing ligado ejetaria no pullback em que se adiciona."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        m = page.evaluate(_LE)
        nomes = {x["nome"] for x in m["protecao"]}
        assert nomes == {"break-even", "trailing stop"}, m["protecao"]
        assert {x["estado"] for x in m["protecao"]} == {"DESLIGADO"}, m["protecao"]
        assert "recuo à média é ENTRADA" in m["txt"], m["txt"]
        assert "sem evidência" in m["txt"], ("o que o corpus não sustenta fica dito",
                                             m["txt"])
        browser.close()


# ────────────────────── o índice e o gate de amostra ───────────────────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_com_amostra_pequena_a_tela_DIZ_que_nao_ha_e_nao_mostra_taxa(base):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, None, {"123": {"n": 9, "n_fechados": 3, "taxa_acerto": 1.0}})
        m = page.evaluate(_LE)
        conf = " ".join(m["conf"])
        assert "insuficiente" in conf, conf
        assert "n=3" in conf, conf
        assert "%" not in conf, ("nada de taxa com 3 casos", conf)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [DESKTOP, TELEFONE], ids=["desktop", "telefone"])
def test_com_amostra_a_EXPECTATIVA_vem_antes_da_taxa(base, viewport):
    """"70% de acerto com R:R 0,13 perde dinheiro" — a expectativa lidera, e a taxa
    nunca aparece sem o intervalo ao lado."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre(page, base, None, {"123": {"n": 20, "n_fechados": 12, "taxa_acerto": 0.5,
                                         "expectativa_r": -0.1, "rr_medio": 0.8,
                                         "acerto_equilibrio": 0.5556, "n_com_rr": 12}})
        m = page.evaluate(_LE)
        conf = " ".join(m["conf"])
        assert "preliminar" in conf, conf
        assert "E[R]" in conf, conf
        assert conf.index("E[R]") < conf.index("acerto"), ("a expectativa vem primeiro", conf)
        assert "intervalo 95%" in conf, ("a taxa nunca aparece sozinha", conf)
        assert "pra empatar" in conf, ("o acerto de equilíbrio ao lado", conf)
        browser.close()


# ──────────────────────────── nada corta, nada estoura ─────────────────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [DESKTOP, TELEFONE], ids=["desktop", "telefone"])
def test_o_card_nao_corta_nem_rola_de_lado(base, viewport):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre(page, base, None, {"123": {"n": 40, "n_fechados": 25, "taxa_acerto": 0.6,
                                         "expectativa_r": 0.35, "rr_medio": 1.2,
                                         "acerto_equilibrio": 0.4545, "n_com_rr": 25}})
        m = page.evaluate("""() => {
          const el = document.getElementById('execCard');
          return {cortados: [...el.querySelectorAll('*')]
                    .filter(e => e.scrollWidth > e.clientWidth + 1)
                    .map(e => (e.className || '').toString().slice(0, 30)),
                  rola: document.documentElement.scrollWidth >
                        document.documentElement.clientWidth};
        }""")
        assert m["cortados"] == [], m["cortados"]
        assert not m["rola"], m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_sem_setup_o_card_nao_abre_caixa_vazia(base):
    """Mesma regra dos cards de leitura: quem não existe no dado não vira caixa com
    travessão inventado."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        sem = dict(_PLANO_CRWD)
        sem["pattern"] = None
        snap = _snap("setup123")
        snap["result"]["actionable"] = sem
        snap["result"]["price_chart"] = dict(_CHART, timeframe="1d")
        carta = _card(sem, {})

        def handler(route):
            url = route.request.url
            if "/api/execucao" in url:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps(carta))
            elif "/api/status/" in url or re.search(r"/api/run/[^/]+$", url):
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps(snap))
            else:
                route.continue_()
        page.route(re.compile(r"/api/"), handler)
        page.goto(base, wait_until="networkidle")
        page.evaluate("() => watchRun('R-009')")
        page.wait_for_timeout(500)
        escondido = page.evaluate(
            "() => document.getElementById('execCard').classList.contains('hidden')")
        assert escondido, "sem padrão, sem card de execução"
        browser.close()

"""HISTÓRIA DE VITÓRIA ≠ HISTÓRIA DE DERROTA ≠ NUNCA CHEGOU A VALER (DA-130).

A pintura inteira do padrão pendia de UM booleano (`invalidado`), e um booleano só
sabe dizer duas coisas. Com o ciclo de vida (DA-129) são quatro os significados na
mesma tela, e colapsá-los custa nos DOIS sentidos:

* pintar de **cinza** um trade que bateu o alvo diz que ele não existiu;
* pintá-lo com o **azul de um vivo** — que é o que acontecia depois da DA-125, porque
  `invalidado` efetivo virou falso — diz que ainda há o que fazer com ele.

A gramática é a que a tela já ensina (DA-078 regra 3), sem cor nova:
**verde = ganho · vermelho = perda · cinza = nunca chegou a valer · azul/laranja por
direção = ainda vale.** O peso NÃO separa os três primeiros: "já não se opera" é a
mesma informação nos três, e hierarquizar por resultado faria a tela ordenar por
quem ganhou em vez de por o que é acionável.
"""

import contextlib
import json
import re
from pathlib import Path

import pytest

from tests.test_webui_frame_e_cor_e2e import DESKTOP, TELEFONE, sobe_servidor
from tests.test_webui_um_grafico_um_metodo_e2e import _CHART, _PLANO

pytestmark = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None


@pytest.fixture
def base(tmp_path):
    yield from sobe_servidor(tmp_path)


_PAT = _CHART["markers"]["pattern_123"]
_DESF = {"tipo": "alvo", "em": "2026-08-28 15:00", "price": 414.0,
         "entrada_em": "2026-08-28 13:00", "entrada": 440.0,
         "empate_na_barra": False}


def _pat(ciclo, **over):
    p = {**_PAT, "ciclo": ciclo, "invalidado": ciclo.startswith("invalidado"),
         "invalidado_em": "2026-08-28 23:00" if "invalid" in ciclo else None,
         "encerrado": ciclo.startswith("concluido"),
         "desfecho": ({**_DESF, "tipo": "stop", "price": 470.0}
                      if ciclo == "concluido_stop" else
                      _DESF if ciclo == "concluido_alvo" else None),
         "acionado_em": "2026-08-28 13:00"}
    p.update(over)
    return p


def _abre(page, base_url, pattern, viewport=None):
    chart = {**_CHART, "markers": {**_CHART["markers"], "pattern_123": pattern}}
    r = {"verdict": None, "final_decision": "", "timeframe": "1d",
         "as_of_price": 465.58, "actionable": {**_PLANO, "pattern": pattern},
         "live_price": None, "price_chart": chart, "degraded": [],
         "bull": "", "bear": "", "research_manager": "", "investment_plan": "",
         "trader_plan": "", "risk_decision": "", "market_report": "",
         "sentiment_report": "", "news_report": "", "fundamentals_report": "",
         "erick_report": "", "drop_nature": {}, "derivatives_report": "",
         "setup123": True, "storm123": False}
    snap = {"run_id": "R-130", "ticker": "MSFT", "date": "2026-08-29",
            "asset_type": "stock", "status": "done", "elapsed": 2,
            "cost": {"usd": 0.0}, "verdict": None, "verdict_timeframe": "1d",
            "result": r}

    def handler(route):
        u = route.request.url
        if "/api/execucao" in u:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"card": None}))
        elif "/api/status/" in u or re.search(r"/api/run/[^/]+$", u):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(snap))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)
    page.goto(base_url, wait_until="networkidle")
    page.evaluate("() => watchRun('R-130')")
    page.wait_for_selector("#setupCards:not(.hidden)")
    page.wait_for_timeout(300)


_LE = """() => ({
  fase: patFase((document.getElementById('priceChart')._actionable || {}).pattern),
  cor: patColor((document.getElementById('priceChart')._actionable || {}).pattern),
  fantasma: ehFantasma((document.getElementById('priceChart')._actionable || {}).pattern),
  historia: ehHistoria((document.getElementById('priceChart')._actionable || {}).pattern),
  pontos: JSON.parse(document.getElementById('priceChart').dataset.pat123 || '[]'),
  rotulos: JSON.parse(document.getElementById('priceChart').dataset.rotulos123 || '[]'),
  legenda: document.getElementById('chartLegend').innerText.replace(/\\s+/g, ' '),
  nota: document.getElementById('chartNote').innerText.replace(/\\s+/g, ' '),
})"""

_VERDE, _VERMELHO, _CINZA = "#2ecc71", "#ff5c6c", "#6b7280"


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("ciclo,cor,palavra", [
    ("concluido_alvo", _VERDE, "encerrado no alvo"),
    ("concluido_stop", _VERMELHO, "encerrado no stop"),
    ("invalidado_sem_acionar", _CINZA, "invalidado"),
    ("invalidado_operando", _CINZA, "invalidado"),
])
def test_cada_fase_tem_a_SUA_cor_e_a_SUA_palavra(base, ciclo, cor, palavra):
    """As três histórias, distintas na cor E no texto. DENTE: antes desta DA as
    quatro linhas caíam em duas — cinza+invalidado ou azul+silêncio."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, _pat(ciclo))
        m = page.evaluate(_LE)
        assert m["cor"].lower() == cor, (ciclo, m["cor"])
        assert m["historia"] is True, ciclo
        assert palavra in m["legenda"], ("na LEGENDA", m["legenda"])
        assert any(palavra in r for r in m["rotulos"]), ("na VELA", m["rotulos"])
        assert all(d["cor"].lower() == cor for d in m["pontos"]), m["pontos"]
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_ENCERRADO_no_alvo_NAO_e_fantasma_e_o_invalidado_E(base):
    """`ehFantasma` deixou de ser o booleano do backend e passou a ler a FASE — o
    cinza e a palavra "invalidado" continuam sendo só do morto (DA-091)."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, _pat("concluido_alvo"))
        m = page.evaluate(_LE)
        assert m["fase"] == "ganho" and m["fantasma"] is False, m
        assert "invalidado" not in m["legenda"], m["legenda"]
        assert "invalidado" not in m["nota"], m["nota"]
        _abre(page, base, _pat("invalidado_sem_acionar"))
        m = page.evaluate(_LE)
        assert m["fase"] == "morto" and m["fantasma"] is True, m
        assert "invalidado" in m["nota"] and "cinza" in m["nota"], m["nota"]
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_NOTA_do_encerrado_e_OUTRA_e_diz_a_cor_certa(base):
    """A nota do fantasma promete "cinza" — repeti-la sobre um trade que bateu o
    alvo contradiria o verde que está na tela."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, _pat("concluido_alvo"))
        n = page.evaluate(_LE)["nota"]
        assert "encerrado no alvo" in n and "verde" in n, n
        assert "o trade terminou" in n, n
        _abre(page, base, _pat("concluido_stop"))
        n = page.evaluate(_LE)["nota"]
        assert "encerrado no stop" in n and "vermelho" in n, n
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_VIVO_continua_com_a_cor_da_DIRECAO(base):
    """DENTE do exagero oposto: sem isto, gastar verde/vermelho no padrão vivo
    apagaria a gramática de DIREÇÃO (azul compra / laranja venda) que a tela usa."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, _pat("vivo"))
        m = page.evaluate(_LE)
        assert m["fase"] == "vivo" and m["historia"] is False, m
        assert m["cor"].lower() == "#ff9f43", ("venda = laranja", m["cor"])
        assert "invalidado" not in m["legenda"] and "encerrado" not in m["legenda"], m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_GATILHO_sai_do_grafico_no_encerrado(base):
    """O gatilho é o CONVITE A OPERAR. Num trade que terminou ele é ainda mais
    enganoso que num morto: o preço pode estar passando por ali de novo — e foi
    exatamente isso que aconteceu no LINK-USD."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _le = ("() => JSON.parse(document.getElementById('priceChart')"
               ".dataset.gatilho123 || 'null')")
        # a ÂNCORA primeiro: com o padrão vivo a linha É pintada. Sem esta linha o
        # teste passaria mesmo se o gatilho tivesse deixado de existir na tela.
        _abre(page, base, _pat("vivo"))
        vivo = page.evaluate(_le)
        assert vivo and vivo["pedido"] and vivo["pintado"], ("âncora", vivo)
        for ciclo in ("concluido_alvo", "concluido_stop", "invalidado_sem_acionar"):
            _abre(page, base, _pat(ciclo))
            g = page.evaluate(_le)
            assert g and g["pedido"] is True, (ciclo, "o gatilho existe no padrão", g)
            assert g["pintado"] is False, (ciclo, "a linha continuou sendo pintada", g)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [DESKTOP, TELEFONE], ids=["desktop", "telefone"])
def test_a_gramatica_vale_igual_no_TELEFONE(base, viewport):
    """DA-101: no celular encolhe, não muda de significado."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre(page, base, _pat("concluido_alvo"), viewport=viewport)
        m = page.evaluate(_LE)
        assert m["cor"].lower() == _VERDE and "encerrado no alvo" in m["legenda"], m
        browser.close()


# ─────────────────── O CASO REAL, do disco até o pixel ───────────────────────
_REAL = Path("/home/clawd/.tradingagents/logs/webui/runs/20260830-232525-ca31d7.json")


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.skipif(not _REAL.exists(), reason="a run real não está neste disco")
def test_o_LINK_USD_REAL_reaberto_diz_o_desfecho_de_CADA_metodo(tmp_path):
    """O caso que originou a série inteira, do arquivo no disco até o pixel.

    O registro no disco é o ANTIGO (``invalidado: True``, sem desfecho) e não se
    reescreve — quem corrige é a leitura, com a régua de sempre.

    E ele guarda uma coisa que nenhum fixture teria inventado: **os dois métodos
    terminaram em lados opostos, na mesma série, no mesmo dia**.

      * **Storm123** entrou em 29/08 19:00 e bateu o STOP 11,36 em **30/08 04:00**;
      * **Setup123** entrou no gatilho 11,52 em 30/08 13:00 e bateu o ALVO 11,63 em
        **30/08 15:00** — nove horas depois de o Storm já ter perdido;
      * e a invalidação estrutural (o ponto 3 perdido) só veio às **23:00**, com os
        dois trades fechados havia horas.

    É a prova viva de por que veredito de um método não empresta ao outro (DA-081) —
    e de por que "invalidado" não podia ser a última palavra sobre nenhum dos dois.
    """
    rec = json.loads(_REAL.read_text(encoding="utf-8"))
    runs = tmp_path / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{rec['run_id']}.json").write_text(json.dumps(rec), encoding="utf-8")
    it = sobe_servidor(tmp_path)
    base_url = next(it)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport=DESKTOP)
            page.goto(base_url, wait_until="networkidle")
            page.evaluate(f"() => watchRun({rec['run_id']!r})")
            page.wait_for_selector("#setupCards:not(.hidden)")
            page.wait_for_timeout(400)
            m = page.evaluate(r"""() => {
              const c = document.getElementById('priceChart');
              const a = c._actionable || {};
              const mk = (c._chart && c._chart.markers || {}).pattern_123 || null;
              const sp = (a.storm || {}).pattern || null;
              return {
                fase123: patFase(mk), desf123: (mk || {}).desfecho,
                faseStorm: patFase(sp), desfStorm: (sp || {}).desfecho,
                estadoStorm: stormEstado(a.storm),
                corStorm: patColor(sp),
                legenda: document.getElementById('chartLegend').innerText.replace(/\s+/g, ' '),
                card: (document.querySelector('.setup-card') || {}).innerText || '',
              };
            }""")
            # o SETUP123: encerrado no ALVO, com os números do briefing
            assert m["fase123"] == "ganho", ("o veredito veio invertido do disco", m)
            assert m["desf123"]["price"] == 11.63, m["desf123"]
            assert m["desf123"]["em"] == "2026-08-30 15:00", m["desf123"]
            assert m["desf123"]["entrada"] == 11.52, m["desf123"]
            # o STORM123: encerrado no STOP, ANTES — outro método, outro desfecho
            assert m["faseStorm"] == "perda", m
            assert m["desfStorm"]["em"] == "2026-08-30 04:00", m["desfStorm"]
            assert m["desfStorm"]["em"] < m["desf123"]["em"], (
                "o Storm perdeu ANTES de o 1-2-3 ganhar", m)
            # e é o Storm que está na tela (esta run é storm123): vermelho, com a
            # palavra certa, e em lugar nenhum "invalidado"
            assert m["estadoStorm"] == "encerrado", m["estadoStorm"]
            assert m["corStorm"].lower() == _VERMELHO, m["corStorm"]
            assert "encerrado no stop" in m["legenda"], m["legenda"]
            assert "invalidado" not in m["legenda"], m["legenda"]
            card = " ".join(m["card"].split())
            assert "ENCERRADO NO STOP" in card, card[:300]
            assert "INVALIDADO" not in card, card[:300]
            browser.close()
    finally:
        with contextlib.suppress(StopIteration):
            next(it)   # fecha o servidor

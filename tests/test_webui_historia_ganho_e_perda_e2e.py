"""HISTÓRIA É FANTASMA, E O RESULTADO VIVE NA PALAVRA (DA-140 — revisa a DA-130).

A DA-130 tinha dado uma COR a cada desfecho: **verde = ganhou, vermelho = perdeu,
cinza = invalidado**, e azul/laranja por direção no vivo. Resolvia um problema real
(pintar de cinza um trade que bateu o alvo diz que ele não existiu) e criava outro
maior: **o verde passou a significar DIREÇÃO num lugar e DESFECHO noutro.**

Foi assim que um Storm123 de **VENDA** encerrado no alvo apareceu **verde**, e o dono
leu — corretamente, pela regra que a tela ensina em todo o resto — "compra":
*"pq está verde se é 123 de venda?"*. Na pergunta seguinte, leu um trade encerrado em
30/07 como oportunidade ainda aberta. **Cor é lida ANTES da palavra**: enquanto dois
eixos dividirem a paleta, o rótulo chega tarde.

A gramática que fica, com UM eixo na cor:

* **verde/vermelho CHEIOS** — setup VIVO, e a matiz diz a **direção**;
* **cinza (`--dim`)** — fantasma INVALIDADO: nunca chegou a ser um trade, não há lado
  a lembrar;
* **verde/vermelho ESMAECIDOS** — fantasma ENCERRADO: a matiz preserva a direção
  ORIGINAL, a opacidade diz "já foi".

**Alvo e stop têm a MESMA cor, de propósito** — se o desfecho voltasse pra matiz, o
eixo dobrado voltava junto. O que os separa é a PALAVRA, que já está na legenda, no
rótulo da vela e no card.
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


def _pat(ciclo, direcao="venda", **over):
    p = {**_PAT, "direction": direcao,
         "ciclo": ciclo, "invalidado": ciclo.startswith("invalidado"),
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
    plano = {**_PLANO, "pattern": pattern}
    r = {"verdict": None, "final_decision": "", "timeframe": "1d",
         "as_of_price": 465.58, "actionable": plano,
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
# A cor da DIREÇÃO — a única coisa que a matiz diz, viva ou morta.
_DA_DIRECAO = {"compra": _VERDE, "venda": _VERMELHO}


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("direcao", ["compra", "venda"])
@pytest.mark.parametrize("ciclo,palavra", [
    ("vivo", ""),
    ("concluido_alvo", "encerrado no alvo"),
    ("concluido_stop", "encerrado no stop"),
])
def test_a_MATIZ_diz_a_DIRECAO_e_so(base, ciclo, palavra, direcao):
    """O DENTE CENTRAL desta DA, e o caso exato que o dono pegou: um 1-2-3 de VENDA
    encerrado no ALVO **não pode sair verde**. Antes saía — verde era "ganhou" —, e
    a mesma tela ensinava, na faixa do card ao lado, que verde é compra.

    A matiz é a mesma nos três ciclos de propósito: quem diz que o trade terminou é
    a OPACIDADE (e a palavra), não a cor.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, _pat(ciclo, direcao))
        m = page.evaluate(_LE)
        assert m["cor"].lower() == _DA_DIRECAO[direcao], (ciclo, direcao, m["cor"])
        assert all(d["cor"].lower() == _DA_DIRECAO[direcao] for d in m["pontos"]), m["pontos"]
        if palavra:
            assert m["historia"] is True, ciclo
            assert palavra in m["legenda"], ("na LEGENDA", m["legenda"])
            assert any(palavra in r for r in m["rotulos"]), ("na VELA", m["rotulos"])
        else:
            assert m["historia"] is False, ciclo
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("direcao", ["compra", "venda"])
@pytest.mark.parametrize("ciclo", ["invalidado_sem_acionar", "invalidado_operando"])
def test_o_INVALIDADO_e_o_unico_que_larga_a_direcao(base, ciclo, direcao):
    """Ele nunca chegou a ser um trade: não há lado a lembrar. Vai pro cinza do tema
    (`--dim`), que é o primeiro plano REBAIXADO contra o fundo — nunca branco pleno,
    que competiria com o texto vivo (DA-078)."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, _pat(ciclo, direcao))
        m = page.evaluate(_LE)
        assert m["cor"].lower() == _CINZA, (ciclo, direcao, m["cor"])
        assert m["fantasma"] is True and m["historia"] is True, m
        assert "invalidado" in m["legenda"], m["legenda"]
        # cinza de verdade: sem canal dominante
        r, g, b = (int(m["cor"][i:i + 2], 16) for i in (1, 3, 5))
        assert max(r, g, b) - min(r, g, b) < 40, ("cinza, não uma matiz", m["cor"])
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_ALVO_e_STOP_saem_na_MESMA_cor_e_se_separam_pela_PALAVRA(base):
    """DENTE do exagero oposto: devolver o desfecho à matiz (verde ganhou / vermelho
    perdeu) reabre o eixo dobrado que esta DA fechou. Os dois têm de ser
    indistinguíveis na COR e distinguíveis no TEXTO."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        cores, palavras = {}, {}
        for ciclo in ("concluido_alvo", "concluido_stop"):
            _abre(page, base, _pat(ciclo, "compra"))
            m = page.evaluate(_LE)
            cores[ciclo] = m["cor"].lower()
            palavras[ciclo] = m["legenda"]
        assert cores["concluido_alvo"] == cores["concluido_stop"] == _VERDE, cores
        assert "encerrado no alvo" in palavras["concluido_alvo"], palavras
        assert "encerrado no stop" in palavras["concluido_stop"], palavras
        assert "encerrado no stop" not in palavras["concluido_alvo"], palavras
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_ENCERRADO_NAO_e_invalidado_e_nao_recebe_a_palavra_dele(base):
    """`ehFantasma` continua sendo só do MORTO: é ele que carrega o cinza e a palavra
    "invalidado" (DA-091). O encerrado é história — `ehHistoria` —, e chamá-lo de
    invalidado seria a tela mentindo sobre um trade que chegou ao alvo."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, _pat("concluido_alvo"))
        m = page.evaluate(_LE)
        assert m["fase"] == "ganho" and m["fantasma"] is False and m["historia"] is True, m
        assert "invalidado" not in m["legenda"], m["legenda"]
        assert "invalidado" not in m["nota"], m["nota"]
        _abre(page, base, _pat("invalidado_sem_acionar"))
        m = page.evaluate(_LE)
        assert m["fase"] == "morto" and m["fantasma"] is True, m
        assert "invalidado" in m["nota"] and "cinza" in m["nota"], m["nota"]
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_NOTA_do_encerrado_NAO_promete_cor_de_desfecho(base):
    """A nota diz o que a cor significa — e agora tem de dizer o que ela NÃO significa
    mais. Prometer "verde" num trade que terminou reabriria, em palavras, o eixo que a
    DA-140 fechou nos pixels."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        for ciclo, palavra in (("concluido_alvo", "encerrado no alvo"),
                               ("concluido_stop", "encerrado no stop")):
            _abre(page, base, _pat(ciclo))
            n = page.evaluate(_LE)["nota"]
            assert palavra in n and "esmaecidos" in n, (ciclo, n)
            assert "em verde" not in n and "em vermelho" not in n, (ciclo, n)
            assert "não na cor" in n, (ciclo, n)
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
        _abre(page, base, _pat("concluido_alvo", "venda"), viewport=viewport)
        m = page.evaluate(_LE)
        assert m["cor"].lower() == _VERMELHO, ("venda encerrada não vira verde", m)
        assert "encerrado no alvo" in m["legenda"], m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_CARD_da_historia_esmaece_e_so_o_invalidado_larga_a_direcao(base):
    """A mesma gramática um andar acima, que é onde a decisão de operar se lê. O card
    de uma leitura ENCERRADA tinha a cara de um vivo — foi por isso que o dono leu um
    trade fechado em 30/07 como oportunidade aberta ("e pq ainda aparece se já
    atingiu o alvo?")."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _ler = """() => {
          const c = document.querySelector('#setupCards .sc-123');
          if (!c) return null;
          const cs = getComputedStyle(c);
          return {classes: [...c.classList], opacidade: cs.opacity,
                  borda: cs.borderLeftColor,
                  dir: getComputedStyle(c.querySelector('.sc-dir')).color};
        }"""
        _abre(page, base, _pat("vivo", "compra"))
        vivo = page.evaluate(_ler)
        assert "sc-fantasma" not in vivo["classes"], vivo
        assert float(vivo["opacidade"]) == 1.0, vivo

        _abre(page, base, _pat("concluido_alvo", "compra"))
        fim = page.evaluate(_ler)
        assert "sc-fantasma" in fim["classes"], ("encerrado tem de ser fantasma", fim)
        assert "sc-invalidado" not in fim["classes"], fim
        assert float(fim["opacidade"]) < 1.0, fim
        assert fim["borda"] == vivo["borda"] and fim["dir"] == vivo["dir"], (
            "o encerrado guarda a MATIZ da direção — quem o separa é a opacidade",
            vivo, fim)

        _abre(page, base, _pat("invalidado_sem_acionar", "compra"))
        mortoc = page.evaluate(_ler)
        assert {"sc-fantasma", "sc-invalidado"} <= set(mortoc["classes"]), mortoc
        assert mortoc["borda"] != vivo["borda"], ("o invalidado larga a direção", mortoc)
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
                corStorm: patColor(sp), dirStorm: (sp || {}).direction,
                cor123: patColor(mk), dir123: (mk || {}).direction,
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
            # e é o Storm que está na tela (esta run é storm123): a cor é a da
            # DIREÇÃO, a palavra é a do desfecho, e em lugar nenhum "invalidado".
            #
            # ESTE É O CASO REAL QUE PROVA A DA-140 INTEIRA: o Storm é de COMPRA e
            # PERDEU. Pela DA-130 ele saía VERMELHO — a cor de "perdeu" — na mesma
            # tela onde vermelho quer dizer VENDA. Um trade de compra pintado com a
            # cor de venda, e nada dizendo qual dos dois sentidos estava em jogo.
            assert m["estadoStorm"] == "encerrado", m["estadoStorm"]
            assert m["dirStorm"] == "compra" and m["faseStorm"] == "perda", m
            assert m["corStorm"].lower() == _DA_DIRECAO[m["dirStorm"]], (
                "a matiz voltou a dizer o desfecho", m["corStorm"], m["dirStorm"])
            # e o 1-2-3 da MESMA tela, que GANHOU, sai na cor da SUA direção — dois
            # desfechos opostos, e a cor não os separa: quem separa é a palavra
            assert m["cor123"].lower() == _DA_DIRECAO[m["dir123"]], m
            assert "encerrado no stop" in m["legenda"], m["legenda"]
            assert "invalidado" not in m["legenda"], m["legenda"]
            card = " ".join(m["card"].split())
            assert "ENCERRADO NO STOP" in card, card[:300]
            assert "INVALIDADO" not in card, card[:300]
            browser.close()
    finally:
        with contextlib.suppress(StopIteration):
            next(it)   # fecha o servidor

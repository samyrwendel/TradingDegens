"""A FAIXA DE FRAMES no card da lista de observação (DA-133).

Proposta do Samyr: *"|15M | 1H | 4H | D | S|"* no card, pra ler a **confluência de
relance** sem abrir o ativo. Três decisões que a proposta não trazia fechadas, e
cada uma tem teste aqui:

1. **Os frames são os que o scan REALMENTE varre** (hoje 1d/4h/1h), e a lista sai do
   próprio scan. Mostrar 15m ou semanal como "sem setup" seria afirmar o que não se
   mediu — eles existem na análise individual, não na varredura.
2. **A cor não é o único portador**: cada fase tem forma própria, e o `title` diz
   frame, direção e fase em palavras.
3. **A faixa e a tabela do scan CONCORDAM.** O mesmo ativo não pode dizer uma coisa
   no card e outra na lista — é a mesma taxonomia (DA-121), lida da mesma tabela.

E a faixa não busca nada: ela é uma leitura do scan que já está na tela.
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


@pytest.fixture
def base(tmp_path):
    yield from sobe_servidor(tmp_path)


def _linha(frame, estado, direction="compra"):
    return {"frame": frame, "estado": estado, "direction": direction,
            "price": 100.0, "trigger": 101.0, "dist_pct": 0.01,
            "pattern_state": "formando"}


_ATIVOS = [
    # os cinco estados que a varredura produz, um por ativo
    {"ticker": "AAA", "frames": [_linha("1d", "em_gatilho", "compra"),
                                 _linha("4h", "formando", "compra"),
                                 _linha("1h", "em_movimento", "compra")]},
    {"ticker": "BBB", "frames": [_linha("1d", "em_gatilho", "venda"),
                                 _linha("4h", "invalidou", "venda"),
                                 _linha("1h", "concluido", "venda")]},
    {"ticker": "CCC", "frames": [_linha("1d", "sem_setup", None),
                                 _linha("4h", "sem_dado", None),
                                 _linha("1h", "formando", "venda")]},
]
for _a in _ATIVOS:
    _a["melhor"] = _a["frames"][0]

_SCAN = {"date": "2026-08-31", "frames": ["1d", "4h", "1h"],
         "gerado_em": "2026-08-31T22:00:00-04:00",
         "ativos": _ATIVOS, "oportunidades": [],
         "resumo": {"em_gatilho": 2, "formando": 2, "em_movimento": 1,
                    "invalidou": 1, "concluido": 1, "sem_setup": 1, "sem_dado": 1}}

_HIST = [{"run_id": f"R-{a['ticker']}", "ticker": a["ticker"], "date": "2026-08-31",
          "asset_type": "stock", "status": "done", "verdict": None,
          "elapsed": 1, "cost": {"usd": 0.0}, "finished_at": "2026-08-31 20:00"}
         for a in _ATIVOS]


def _abre(page, base_url, scan=None, viewport=None):
    def handler(route):
        u = route.request.url
        if "/api/scan/salvo" in u or "/api/scan" in u:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(scan if scan is not None else _SCAN))
        elif "/api/history" in u:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"runs": _HIST}))
        elif "/api/watchlist" in u:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"tickers": [
                              {"ticker": a["ticker"]} for a in _ATIVOS]}))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)
    # `networkidle` NÃO serve aqui: o poller de preço bate a cada 5s e a rede nunca
    # fica ociosa — sob carga da suíte isso vira um timeout de 30s que se leria como
    # "a faixa não apareceu". Espera-se o que de fato importa: a lista pintada.
    page.goto(base_url, wait_until="domcontentloaded")
    # `state="attached"`: no telefone a lateral nasce RECOLHIDA (fica atrás do
    # botão), então esperar por visibilidade seria esperar por um gesto do usuário.
    page.wait_for_selector(".history li", state="attached", timeout=15000)
    # e a faixa chegando — sem espera cega. Quando o scan não tem ativos ela NÃO
    # deve aparecer, e aí o que se espera é o contrário (nenhuma).
    if (scan if scan is not None else _SCAN).get("ativos"):
        page.wait_for_selector(".h-faixa", state="attached", timeout=15000)
    else:
        page.wait_for_timeout(600)


_FAIXA = """(tk) => {
  const li = [...document.querySelectorAll('.history li')]
    .find(e => e.dataset.ticker === tk);
  if (!li) return null;
  const f = li.querySelector('.h-faixa');
  if (!f) return {marcas: []};
  return {marcas: [...f.querySelectorAll('.fx-m')].map(m => ({
    tf: m.textContent, cls: m.className, title: m.title,
    rotulo: m.getAttribute('aria-label'),
  }))};
}"""


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_faixa_tem_UM_marcador_por_frame_VARRIDO(base):
    """DENTE: a proposta pedia cinco frames; o scan varre três. Um marcador para um
    frame que não foi lido afirmaria o que não se mediu."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        m = page.evaluate(_FAIXA, "AAA")
        assert m and len(m["marcas"]) == 3, m
        assert [x["tf"] for x in m["marcas"]] == ["D", "4h", "1h"], m["marcas"]
        # e nenhum 15m/semanal inventado
        assert not any(x["tf"] in ("15m", "S") for x in m["marcas"]), m["marcas"]
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_FASE_e_a_DIRECAO_saem_na_classe_e_por_extenso_no_title(base):
    """A cor nunca é o único portador: a fase vai na CLASSE (que carrega a forma) e
    o title diz frame, fase e direção em palavras — pra quem não distingue as cores
    e pra quem usa leitor de tela."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        m = page.evaluate(_FAIXA, "AAA")["marcas"]
        assert "fx-agora" in m[0]["cls"] and "compra" in m[0]["cls"], m[0]
        assert "fx-esperando" in m[1]["cls"], m[1]
        assert "fx-andou" in m[2]["cls"], m[2]
        assert "na entrada" in m[0]["title"].lower(), m[0]["title"]
        assert "de compra" in m[0]["title"], m[0]["title"]
        assert "diário" in m[0]["title"].lower() or "1d" in m[0]["title"], m[0]["title"]
        # o rótulo acessível existe e é o mesmo texto
        assert m[0]["rotulo"] == m[0]["title"], m[0]
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_HISTORIA_nao_leva_cor_de_direcao(base):
    """Encerrado e invalidado são "já não se opera" — pintar a direção neles seria o
    dado errado em destaque. É a mesma razão do chip cinza na lista (DA-125)."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        m = page.evaluate(_FAIXA, "BBB")["marcas"]
        assert "fx-agora" in m[0]["cls"] and "venda" in m[0]["cls"], m[0]
        assert "fx-morreu" in m[1]["cls"], m[1]
        assert "fx-encerrado" in m[2]["cls"], m[2]
        for x in (m[1], m[2]):
            assert "compra" not in x["cls"] and "venda" not in x["cls"], x
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_sem_leitura_NAO_se_veste_de_sem_setup_colorido(base):
    """"Não há leitura" não pode ter a mesma cara de "li e não achei"."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        m = page.evaluate(_FAIXA, "CCC")["marcas"]
        assert "fx-sem" in m[0]["cls"] and "fx-sem" in m[1]["cls"], m
        assert "sem leitura" in m[0]["title"].lower(), m[0]["title"]
        assert "fx-esperando" in m[2]["cls"] and "venda" in m[2]["cls"], m[2]
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_faixa_e_a_TABELA_do_scan_dizem_a_MESMA_fase(base):
    """O critério central: o mesmo ativo não pode dizer uma coisa no card e outra na
    lista. As duas leem a mesma tabela (DA-121) — este teste é o que impede que
    alguém "melhore" uma das duas sozinha."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        m = page.evaluate("""() => {
          const out = {};
          for (const a of faixaFonte().ativos) {
            out[a.ticker] = (a.frames || []).map(f => ({
              tf: f.frame,
              faseTabela: FASE_DO_SCAN_ESTADO[f.estado] || 'sem_leitura',
            }));
          }
          const cards = {};
          for (const li of document.querySelectorAll('.history li')) {
            const f = li.querySelector('.h-faixa');
            if (!f) continue;
            cards[li.dataset.ticker] = [...f.querySelectorAll('.fx-m')].map(x => x.className);
          }
          return {tabela: out, cards};
        }""")
        cls_de = {"agora": "fx-agora", "esperando": "fx-esperando",
                  "andou": "fx-andou", "encerrado": "fx-encerrado",
                  "morreu": "fx-morreu", "sem_leitura": "fx-sem"}
        for tk, linhas in m["tabela"].items():
            assert tk in m["cards"], (tk, "o card ficou sem faixa")
            for i, linha in enumerate(linhas):
                esperado = cls_de[linha["faseTabela"]]
                assert esperado in m["cards"][tk][i], (
                    tk, linha["tf"], "faixa e tabela discordam",
                    linha["faseTabela"], m["cards"][tk][i])
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_SEM_scan_a_faixa_NAO_aparece(base):
    """DENTE: marcadores mudos se leriam como "varri e não achei nada", que é
    diferente de "ainda não varri". Sem scan, o card fica como era."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, scan={"ativos": [], "frames": []})
        m = page.evaluate(_FAIXA, "AAA")
        assert m is None or not m["marcas"], m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_faixa_NAO_dispara_busca_por_card(base):
    """Ela é leitura do scan que já está na tela. Uma chamada por card seria 20
    requisições numa watchlist de 20."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        pedidos = []
        page.on("request", lambda r: pedidos.append(r.url))
        _abre(page, base)
        assert page.evaluate(_FAIXA, "AAA")["marcas"], "a faixa nem apareceu"
        scan_calls = [u for u in pedidos if "/api/scan" in u]
        assert len(scan_calls) <= 2, ("uma chamada por card", scan_calls)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_no_TELEFONE_a_faixa_encolhe_e_NAO_some(base):
    """DA-101: no celular encolhe. Ela é o motivo do card — sumir seria devolver a
    tela ao estado que esta entrega veio resolver."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE)
        _abre(page, base, viewport=TELEFONE)
        m = page.evaluate("""() => {
          const li = [...document.querySelectorAll('.history li')]
            .find(e => e.dataset.ticker === 'AAA');
          const f = li && li.querySelector('.h-faixa');
          if (!f) return null;
          const cs = getComputedStyle(f);
          const mm = f.querySelector('.fx-m');
          const cm = mm && getComputedStyle(mm);
          return {n: f.querySelectorAll('.fx-m').length, display: cs.display,
                  minW: cm && cm.minWidth, h: cm && cm.height};
        }""")
        assert m and m["n"] == 3, m
        # ENCOLHE, não some: a lateral inteira nasce recolhida no telefone (é um
        # gesto do usuário, não uma regra de CSS), mas a faixa em si continua
        # `flex` e com medidas MENORES que as do desktop — nunca `display: none`.
        assert m["display"] != "none", m
        assert m["minW"] == "20px" and m["h"] == "12px", ("devia encolher", m)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_zero_emoji_na_faixa(base):
    """DA-076: nenhum pictograma. O marcador é forma e cor, o frame vai em letras."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        txt = page.evaluate("""() => [...document.querySelectorAll('.h-faixa')]
          .map(f => f.textContent + ' ' + [...f.querySelectorAll('.fx-m')]
            .map(m => m.title).join(' ')).join(' ')""")
        assert txt.strip(), "faixa vazia"
        assert not re.search(r"[\U0001F300-\U0001FAFF☀-➿]", txt), txt
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_cada_fase_se_distingue_SEM_a_cor(base):
    """O requisito de acessibilidade, MEDIDO no estilo computado e não na classe.

    Se as seis fases só diferissem em cor, a faixa seria ilegível pra quem não as
    distingue — e um erro de CSS (uma regra que não pega) passaria batido num teste
    que só olha `className`. Cada fase tem de diferir de todas as outras em pelo
    menos um eixo que NÃO é cor: preenchimento, espessura da base, estilo da borda
    ou risco.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        estilos = page.evaluate("""() => {
          const out = {};
          for (const li of document.querySelectorAll('.history li')) {
            const f = li.querySelector('.h-faixa'); if (!f) continue;
            for (const m of f.querySelectorAll('.fx-m')) {
              const fase = [...m.classList].find(c => c.startsWith('fx-') && c !== 'fx-m');
              const cs = getComputedStyle(m);
              out[fase] = {
                preenchido: cs.backgroundColor !== 'rgba(0, 0, 0, 0)',
                base: cs.borderBottomWidth,
                estilo: cs.borderTopStyle,
                risco: cs.textDecorationLine,
              };
            }
          }
          return out;
        }""")
        assert len(estilos) == 6, ("as seis fases têm de aparecer", sorted(estilos))
        # a assinatura SEM COR de cada fase tem de ser única
        assinaturas = {k: tuple(sorted(v.items())) for k, v in estilos.items()}
        vistos = {}
        for fase, ass in assinaturas.items():
            assert ass not in vistos, (
                "duas fases indistinguíveis sem a cor", fase, vistos.get(ass), ass)
            vistos[ass] = fase
        # e as âncoras do que cada forma DIZ
        assert estilos["fx-agora"]["preenchido"] is True, estilos["fx-agora"]
        assert estilos["fx-andou"]["base"] == "4px", estilos["fx-andou"]
        assert estilos["fx-esperando"]["preenchido"] is False, estilos["fx-esperando"]
        assert estilos["fx-morreu"]["risco"] == "line-through", estilos["fx-morreu"]
        assert estilos["fx-sem"]["estilo"] == "dotted", estilos["fx-sem"]
        browser.close()

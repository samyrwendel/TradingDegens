"""Um gráfico, um método — a tela para de misturar as leituras (task 20260830-009).

"Percebo tbm que mistura tudo em um gráfico só, Storm123, Setup123 e Padrão com
Erick." Eram TRÊS misturas empilhadas:

  1. **as médias** — as duas famílias sempre desenhadas, pra todo método: MMS
     20/50/200 (Padrão) + EMA 8/21/50 (Erick), mais a EMA 80 do Éden nas runs do
     Storm. Sete linhas numa tela onde o método aberto usa três;
  2. **os níveis** — numa run do Storm o gráfico traçava os do Storm E os do plano,
     porque a única condição era o Storm ter opinião. Daí "Storm · stop (SL) 497,98"
     e "stop (SL) 497,59" empilhados a 0,39 um do outro, sem dono;
  3. **os pontos numerados** — os círculos 1-2-3 vinham do detector de SWINGS mesmo
     numa run do Storm, cujo 1-2-3 é outro padrão (três candles consecutivos). Mesma
     numeração, pontos diferentes — a colisão que o próprio módulo já declarava.

A regra que estes testes travam: **o gráfico desenha a leitura que dá NOME ao método
aberto**. As outras continuam inteiras nos cards; no gráfico só entram se pedidas, e
aí toda etiqueta passa a dizer de qual método é.
"""

import json
import re

import pytest

from tests.test_webui_frame_e_cor_e2e import DESKTOP, TELEFONE, sobe_servidor
from tradingagents.webui import timeutil

pytestmark = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None

_HOJE = timeutil.today()


@pytest.fixture
def base(tmp_path):
    yield from sobe_servidor(tmp_path)


# Uma série com as duas leituras EXISTINDO ao mesmo tempo — é o caso do print, e é o
# único em que a mistura aparece.
_PONTOS = [{"date": "2026-08-24", "price": 470.0},
           {"date": "2026-08-25", "price": 440.0},
           {"date": "2026-08-26", "price": 462.0}]

_STORM = {
    "opera": True, "qualidade": "boa", "veto": None,
    "motivo": "MME 8 abaixo da MME 80 — o Éden autoriza a venda",
    "eden": {"disponivel": True, "ema_rapida": 468.0, "ema_lenta": 492.0,
             "motivo": "rápida abaixo da lenta"},
    "pattern": {"p1": {"date": "2026-08-24", "price": 474.0, "high": 474.0, "low": 466.0},
                "p2": {"date": "2026-08-25", "price": 436.0, "high": 452.0, "low": 436.0},
                "p3": {"date": "2026-08-26", "price": 466.0, "high": 466.0, "low": 449.0},
                "direction": "venda", "amplitude": 38.0},
    "invalidation": {"price": 466.0, "meaning": "retomada do ponto 2"},
    "stop": {"price": 497.98, "basis": "no ponto 2", "slack": 0.0},
    "leituras": [{"entrada": "ponto2", "ordem": "antecipada", "trigger": 452.0,
                  "state": "formando", "state_label": "em formação",
                  "label": "perda da mínima do ponto 2",
                  "target": {"price": 414.0, "label": "amplitude projetada"},
                  "risk_reward": {"entry": 452.0, "entry_basis": "gatilho", "risk": 45.98,
                                  "reward": 38.0, "rr": 0.83, "note": None}}],
}

_CANDLES = [{"d": f"2026-08-{d:02d}", "o": 460.0 + d, "h": 476.0 + d,
             "l": 436.0 + d, "c": 465.0 + d} for d in range(1, 29)]

_CHART = {
    "symbol": "MSFT", "timeframe": "1d", "candles": _CANDLES,
    "ma": {"20": [462.0] * 28, "50": [470.0] * 28, "200": [430.0] * 28},
    "ma_windows": [20, 50, 200],
    "ema": {"8": [464.0] * 28, "21": [468.0] * 28, "50": [472.0] * 28,
            "80": [492.0] * 28},
    "ema_windows": [8, 21, 50, 80],
    "markers": {"buy_regions": [], "active_region": None,
                "pattern_123": {"p1": _PONTOS[0], "p2": _PONTOS[1], "p3": _PONTOS[2],
                                "trigger": 440.0, "state": "formando",
                                "direction": "venda"}},
}

_PLANO = {
    "symbol": "MSFT", "price": 465.58, "as_of": "2026-08-28 17:30",
    "timeframe": "diário (referência)", "horizon": "dias",
    "setup_state": "aguardar_rompimento", "setup_source": "123",
    "buy_zone": {"label": "MMS50 — preço abaixo da média", "price": 470.0,
                 "low": 466.0, "high": 474.0, "band_basis": "±0.5·ATR14",
                 "ma_label": "MMS50", "setup": "recuo_media",
                 "tag": "recuo à média (MMS50)", "active_now": False,
                 "distance_pct": -1.0},
    "realize_zone": None, "pullback_zone": None,
    "pattern": {"p1": _PONTOS[0], "p2": _PONTOS[1], "p3": _PONTOS[2],
                "trigger": 440.0, "state": "formando", "direction": "venda"},
    "invalidation": {"price": 462.0, "meaning": "retomada do ponto 3"},
    "stop": {"label": "stop (SL)", "price": 497.59, "anchor": 462.0, "atr": 9.8,
             "basis": "invalidação + folga de 0.5·ATR14"},
    "target": {"label": "fundo anterior", "price": 414.0, "same_as_realize": False},
    "risk_reward": {"entry": 440.0, "entry_basis": "gatilho", "risk": 57.59,
                    "reward": 26.0, "rr": 0.45, "note": None},
}


def _snap(metodo):
    r = {
        "verdict": None, "final_decision": "", "timeframe": "1d",
        "as_of_price": 465.58, "actionable": dict(_PLANO),
        "live_price": {"price": 465.58, "change_pct": -1.0, "currency": "USD",
                       "sessao": "fechado", "rotulo": "último fechamento",
                       "as_of": "29/08 16:00", "regular_price": 465.58,
                       "fuso": "America/New_York", "em": _HOJE},
        "price_chart": _CHART, "degraded": [],
        "bull": "", "bear": "", "research_manager": "", "investment_plan": "",
        "trader_plan": "", "risk_decision": "", "market_report": "",
        "sentiment_report": "", "news_report": "", "fundamentals_report": "",
        "erick_report": "Leitura do Erick." if metodo == "erick" else "",
        "drop_nature": {}, "derivatives_report": "",
        "setup123": metodo == "setup123", "storm123": metodo == "storm123",
    }
    # o Storm só viaja na run do método dele — é assim que o worker monta
    if metodo == "storm123":
        r["actionable"] = {**_PLANO, "storm": _STORM}
    return {"run_id": "R-009", "ticker": "MSFT", "date": "2026-08-29",
            "asset_type": "stock", "status": "done", "elapsed": 2,
            "cost": {"usd": 0.0}, "verdict": None, "verdict_timeframe": "1d",
            "result": r}


def _abre(page, base_url, metodo):
    snap = _snap(metodo)

    def handler(route):
        url = route.request.url
        if "/api/status/" in url or re.search(r"/api/run/[^/]+$", url):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(snap))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)
    page.goto(base_url, wait_until="networkidle")
    page.evaluate("() => watchRun('R-009')")
    page.wait_for_selector("#setupCards:not(.hidden)")
    page.wait_for_timeout(200)


_LE = """() => ({
  metodo: _openMethod,
  camada: camadaDoMetodo(),
  zonas: planZones(document.getElementById('priceChart')._actionable || {}).map(z => z.tag),
  medias: (() => { const m = mediasVisiveis(document.getElementById('priceChart')._actionable || {});
    return {ma: [...m.ma].sort(), ema: [...m.ema].sort()}; })(),
  legenda: document.getElementById('chartLegend').innerText.replace(/\\s+/g, ' ').trim(),
  camadasBtn: [...document.querySelectorAll('.camada-btn')].map(b => b.innerText.trim()),
  camadasVisivel: !document.getElementById('camadasSelector').classList.contains('hidden'),
  cards: [...document.querySelectorAll('#setupCards .sc-title')].map(e => e.innerText.trim()),
})"""


# ───────────────────── cada método carrega só o que é dele ────────────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("metodo,ma,ema", [
    ("padrao", ["20", "200", "50"], []),
    ("setup123", ["20", "200", "50"], []),
    ("erick", [], ["21", "50", "8"]),
    ("storm123", [], ["8", "80"]),
])
def test_as_medias_seguem_o_metodo_aberto(base, metodo, ma, ema):
    """DENTE: sete médias na tela pra todo método — MMS 20/50/200 (Padrão) + EMA
    8/21/50 (Erick) + EMA 80 (Éden). A média é parte da LEITURA: o Éden É a MME 8 ×
    MME 80, e o recuo do Padrão é a MMS."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, metodo)
        m = page.evaluate(_LE)
        assert m["metodo"] == metodo, m
        assert m["medias"]["ma"] == sorted(ma), m
        assert m["medias"]["ema"] == sorted(ema), m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_legenda_descreve_o_desenho_e_nao_o_payload(base):
    """Listar sete médias com três traçadas é pior que não ter legenda: ela vira uma
    lista do que o backend sabe, não do que está na tela."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, "padrao")
        m = page.evaluate(_LE)
        for nome in ("MMS20", "MMS50", "MMS200"):
            assert nome in m["legenda"], (nome, m["legenda"])
        for nome in ("EMA8", "EMA21", "EMA50", "EMA80"):
            assert nome not in m["legenda"], (nome, m["legenda"])
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_na_run_do_storm_o_grafico_e_do_storm(base):
    """DENTE, o print: "Storm · stop (SL) 497,98" e "stop (SL) 497,59" empilhados a
    0,39 um do outro. São de FAMÍLIAS diferentes e nada dizia isso — agora o gráfico
    é do método aberto, e os níveis do plano continuam inteiros no card dele."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, "storm123")
        m = page.evaluate(_LE)
        assert m["camada"] == "storm", m
        assert all("Storm" in z for z in m["zonas"]), ("nível do plano no gráfico do "
                                                       "Storm", m["zonas"])
        # dois stops NUNCA — e o do plano não sumiu do mundo: está no card dele
        stops = [z for z in m["zonas"] if "stop" in z]
        assert len(stops) == 1, m["zonas"]
        assert any("Setup123" in c for c in m["cards"]), ("o card do plano continua "
                                                          "inteiro", m["cards"])
        assert any("Storm123" in c for c in m["cards"]), m["cards"]
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_na_run_do_setup123_o_storm_nao_aparece_no_grafico(base):
    """O outro lado: numa run do plano não há Storm nenhum no payload, e o gráfico
    não inventa camada — nem o seletor a oferece."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, "setup123")
        m = page.evaluate(_LE)
        assert m["camada"] == "plano", m
        assert not any("Storm" in z for z in m["zonas"]), m["zonas"]
        assert m["camadasVisivel"] is False, ("controle que não faz nada é ruído", m)
        browser.close()


# ─────────────── a camada extra existe, é pedida, e vem NOMEADA ───────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [DESKTOP, TELEFONE], ids=["desktop", "telefone"])
def test_ligar_a_outra_camada_nomeia_TODOS_os_rotulos(base, viewport):
    """A regra que fecha o buraco: com duas famílias na tela, nenhum rótulo pode
    ficar anônimo. "stop (SL)" ao lado de "Storm123 · stop (SL)" continuaria sendo
    dois stops sem dono — o que muda é que agora os DOIS se identificam."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre(page, base, "storm123")
        antes = page.evaluate(_LE)
        assert antes["camadasVisivel"] is True, antes
        assert antes["camadasBtn"] == ["Setup123"], antes

        page.click('.camada-btn[data-camada="plano"]')
        page.wait_for_timeout(250)
        m = page.evaluate(_LE)
        stops = [z for z in m["zonas"] if "stop" in z]
        assert len(stops) == 2, ("as duas camadas na tela", m["zonas"])
        for z in m["zonas"]:
            assert z.startswith("Setup123 · ") or z.startswith("Storm123 "), (
                "rótulo anônimo com duas famílias na tela", z, m["zonas"])
        # e as médias das DUAS leituras entram junto: a faixa do recuo sem a linha
        # da média que ela nomeia seria uma faixa flutuando
        assert "50" in m["medias"]["ma"], m["medias"]
        assert "80" in m["medias"]["ema"], m["medias"]
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_desligar_a_camada_volta_ao_rotulo_limpo(base):
    """Prefixo repetido em cada linha de um gráfico que só tem uma família é ruído,
    não informação — some junto com a camada."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, "storm123")
        page.click('.camada-btn[data-camada="plano"]')
        page.wait_for_timeout(200)
        page.click('.camada-btn[data-camada="plano"]')
        page.wait_for_timeout(200)
        m = page.evaluate(_LE)
        assert all(z.startswith("Storm · ") or z.startswith("Storm p") for z in m["zonas"]), m["zonas"]
        assert len([z for z in m["zonas"] if "stop" in z]) == 1, m["zonas"]
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_camada_extra_nao_vaza_pra_outra_analise(base):
    """Camada extra é escolha DAQUELA tela. Carregá-la pra outra análise mostraria
    níveis de um método que talvez nem exista ali, nomeados sem ninguém ter pedido."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, "storm123")
        page.click('.camada-btn[data-camada="plano"]')
        page.wait_for_timeout(200)
        assert page.evaluate("() => _camadas.size") == 1
        _abre(page, base, "storm123")          # reabre: estado zerado
        assert page.evaluate("() => _camadas.size") == 0
        browser.close()


# ───────────────── a numeração 1-2-3 é a do método aberto ────────────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_os_pontos_numerados_sao_os_da_camada_aberta(base):
    """A colisão que o módulo já declarava: o 1-2-3 deste projeto e o 1-2-3 Storm
    usam a MESMA numeração pra pontos DIFERENTES. Na run do Storm os círculos eram
    do detector de swings — do outro método. Cada camada desenha o SEU."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, "storm123")
        so_storm = page.evaluate(
            """() => document.getElementById('chartLegend').innerText.replace(/\\s+/g,' ')""")
        # a legenda do padrão de swings (do plano) não está lá; a do Storm está
        assert so_storm.count("1-2-3") == 1, so_storm
        page.click('.camada-btn[data-camada="plano"]')
        page.wait_for_timeout(250)
        duas = page.evaluate(
            """() => document.getElementById('chartLegend').innerText.replace(/\\s+/g,' ')""")
        assert "Setup123 1-2-3" in duas, duas
        assert "Storm123 1-2-3" in duas, duas
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_carimbo_de_RR_do_grafico_e_o_da_leitura_desenhada(base):
    """A mesma mistura, num carimbo em vez de numa linha: o chip saía sempre de
    ``a.risk_reward`` — o do plano —, então na run do Storm ele mostrava o número de
    uma leitura que não estava traçada em lugar nenhum da tela."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        chip = "() => document.getElementById('priceChart').dataset.rr || ''"

        _abre(page, base, "setup123")
        assert "0,45" in page.evaluate(chip), page.evaluate(chip)

        _abre(page, base, "storm123")
        so_storm = page.evaluate(chip)
        assert "0,83" in so_storm, ("o chip tem de ser o do Storm", so_storm)
        assert "p2" in so_storm, ("e dizer de QUAL das duas entradas", so_storm)
        assert "0,45" not in so_storm, ("número de leitura não desenhada", so_storm)

        # com as duas camadas na tela, o número ganha DONO
        page.click('.camada-btn[data-camada="plano"]')
        page.wait_for_timeout(250)
        duas = page.evaluate(chip)
        assert "Setup123" in duas and "0,45" in duas, duas
        browser.close()

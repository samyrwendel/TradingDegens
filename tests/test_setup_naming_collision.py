"""Dois setups, dois nomes — a tela não pode chamar os dois de "compra".

O caso que levantou isto (ZEC-USD, 4h, 29/08, série congelada em
``tests/data/zec_usd_4h_2026-08-29.json``): o card dizia **COMPRA** e "Setup
ativo agora" com o preço em ~834, enquanto o gráfico desenhava uma faixa verde
rotulada ``compra 803,09`` indo de 790,3 a 815,9. O preço estava visivelmente
FORA da faixa, e a pergunta do Samyr — "não está fora da zona de compra no 4h?"
— era legítima.

Nenhum dos dois estava errado. São **dois setups independentes**:

* **recuo à média** — o preço volta a uma média ascendente e reage (a faixa);
* **1-2-3** — gatilho no rompimento da máxima do ponto 2 (o COMPRA do card).

O dado JÁ distinguia e a tela colapsava. Pior: o rótulo da faixa AFIRMAVA "preço
na média agora" enquanto o número ao lado dizia que o preço estava alguns por
cento fora dela — porque "está tocando" era medido com a tolerância do detector
(``_TOUCH_TOL`` = 8%) e a faixa desenhada é ±0,5·ATR, ordens de grandeza mais
estreita. Duas réguas, uma frase.

Estes testes travam o CONTRATO DE DADO que a tela consome. O limiar do detector
NÃO é assunto daqui: ele continua onde está (mudá-lo mudaria a detecção); o que
se exige é que o payload pare de afirmar o que o próprio número desmente.
"""

import json
import pathlib
import re
import threading

import pandas as pd
import pytest

from tradingagents.dataflows import price_structure as ps

_FIX = pathlib.Path(__file__).parent / "data" / "zec_usd_4h_2026-08-29.json"


@pytest.fixture
def zec(monkeypatch):
    """A série 4h REAL do dia, congelada — o caso do print, offline."""
    raw = json.loads(_FIX.read_text())
    df = pd.DataFrame(raw["rows"])
    df["Date"] = pd.to_datetime(df["Date"])
    monkeypatch.setattr(ps, "_load_frame", lambda symbol, curr_date, timeframe: df.copy())
    return raw


@pytest.fixture
def plano(zec):
    return ps.build_actionable_plan_dict("ZEC-USD", "2026-08-29", timeframe="4h")


@pytest.mark.unit
def test_o_caso_do_print_continua_sendo_o_caso_do_print(plano):
    """Âncora: se a série mudar de forma, os testes abaixo perdem o sentido."""
    bz = plano["buy_zone"]
    assert plano["price"] > bz["high"], (
        "o caso é o preço FORA da faixa por cima", plano["price"], bz)
    assert plano["pattern"]["direction"] == "compra"
    # Na série congelada o último fechamento (833,88) ficou logo ABAIXO do gatilho
    # (834,82): o 1-2-3 está em "rompeu e retraçou". Isso deixa o caso ainda mais
    # afiado — os dois setups convivem E DISCORDAM (a média diz "ativo", o 1-2-3
    # diz "não confirmado"), que é exatamente por que cada um precisa de nome.
    assert plano["pattern"]["state"] == "rompeu_retracou"
    assert plano["setup_state"] == "ativo"


@pytest.mark.unit
def test_a_zona_da_media_tem_nome_proprio_e_nunca_e_so_compra(plano):
    bz = plano["buy_zone"]
    assert bz["setup"] == "recuo_media"
    assert bz["tag"] == "recuo à média (MMS50)", bz
    # DENTE: "compra" sozinho é justamente o nome que colidia com o 1-2-3.
    assert bz["tag"].strip().lower() != "compra"
    assert "recuo à média" in bz["tag"]


@pytest.mark.unit
def test_faixa_com_o_preco_fora_declara_que_nao_esta_ativa(plano):
    bz = plano["buy_zone"]
    assert bz["active_now"] is False, bz
    # a distância publicada é a do PREÇO até a média, medida na mesma régua que o
    # leitor usa quando olha o gráfico — não a tolerância interna do detector
    esperado = round((plano["price"] / bz["price"] - 1) * 100, 1)
    assert bz["distance_pct"] == esperado, bz
    assert bz["distance_pct"] > 0, bz


@pytest.mark.unit
def test_o_rotulo_para_de_afirmar_o_que_o_numero_desmente(plano):
    """DENTE do defeito original: o label dizia "preço na média agora" com o preço
    3,8% fora da faixa."""
    label = plano["buy_zone"]["label"]
    assert "preço na média agora" not in label, label
    assert "fora da faixa" in label, label
    assert "MMS50" in label, label


@pytest.mark.unit
def test_o_veredito_declara_de_qual_setup_veio(plano):
    """"Setup ativo agora" sozinho não dizia de quem falava — e aqui os DOIS
    setups existem ao mesmo tempo (média ativa + 1-2-3 acionado)."""
    assert plano["setup_source"] == "recuo_media", plano["setup_source"]
    # e o outro setup está vivo e DISCORDANDO no mesmo instante
    assert plano["pattern"]["state"] == "rompeu_retracou"


@pytest.mark.unit
def test_o_relatorio_em_prosa_nomeia_o_setup_e_nao_se_contradiz(zec):
    md = ps.build_price_structure_section("ZEC-USD", "2026-08-29", timeframe="4h")
    assert "Setup ativo agora — recuo à média" in md, md[:600]
    assert "o preço está na MMS50" not in md, "a frase que afirmava e desmentia junto"
    assert "Regiões de recuo à média" in md
    assert "Regiões de compra na média" not in md


@pytest.mark.unit
def test_o_grounding_do_ask_nao_entrega_a_ambiguidade_pro_modelo(plano):
    """O pior lugar da colisão: o contexto do Ask chamava a faixa da média de
    "Zona de compra", logo acima de "Padrão 1-2-3 de compra"."""
    from tradingagents.webui import ask
    texto = "\n".join(ask.price_facts(plano, {}))
    assert "Zona de recuo à média" in texto, texto
    assert "Zona de compra" not in texto, texto


@pytest.mark.unit
def test_preco_dentro_da_faixa_continua_dizendo_que_esta_na_media(zec, monkeypatch):
    """O contrário do caso do print: com o preço DENTRO da faixa o rótulo antigo
    era verdadeiro, e tem que continuar sendo dito — a correção é sobre honestidade,
    não sobre apagar a afirmação."""
    raw = json.loads(_FIX.read_text())
    df = pd.DataFrame(raw["rows"])
    df["Date"] = pd.to_datetime(df["Date"])
    # puxa o último fechamento pra cima da média (o resto da série fica intacto)
    plano_ref = ps.build_actionable_plan_dict("ZEC-USD", "2026-08-29", timeframe="4h")
    alvo = plano_ref["buy_zone"]["price"]
    df.loc[df.index[-1], "Close"] = alvo
    monkeypatch.setattr(ps, "_load_frame", lambda symbol, curr_date, timeframe: df.copy())
    # A série preparada é cacheada por 60s (task 023) e este teste TROCA A FONTE no
    # meio — a chamada de referência acima já povoou a chave. Quem muda a fonte por
    # baixo limpa o cache; é o contrato declarado em ``clear_prep_cache``.
    ps.clear_prep_cache()
    p = ps.build_actionable_plan_dict("ZEC-USD", "2026-08-29", timeframe="4h")
    bz = p["buy_zone"]
    assert bz["active_now"] is True, bz
    assert bz["label"] == "MMS50 — preço na média agora", bz
    assert bz["tag"] == "recuo à média (MMS50)", bz   # o nome do setup não muda


# ------------------------------------------------- a TELA, não só o payload ----
# O payload distinguir não basta: o defeito era a tela colapsar o que o dado já
# separava. Aqui se mede o que o navegador REALMENTE escreve, com o plano do
# ZEC-USD servido pela API.

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None


@pytest.fixture
def webui(tmp_path):
    from tradingagents.webui.runner import AnalysisRunner
    from tradingagents.webui.server import make_server
    from tradingagents.webui.store import HistoryStore
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "openai",
                     "deep_think_llm": "x", "quick_think_llm": "y"},
        store=HistoryStore(tmp_path))
    httpd = make_server("127.0.0.1", 0, runner=runner)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


def _abre_resultado(page, base, plano):
    """Injeta o plano do ZEC na view aberta — o mesmo caminho que uma run 1-2-3."""
    page.goto(base, wait_until="networkidle")
    page.evaluate("""(a) => {
      renderSetupCards(a);
      renderChartCard({candles: [
        {d: '2026-08-27', o: 800, h: 840, l: 790, c: 830},
        {d: '2026-08-28', o: 830, h: 845, l: 800, c: 810},
        {d: '2026-08-29', o: 810, h: 847, l: 804, c: 833.88}],
        ma_windows: [50], ema_windows: [],
        markers: {buy_regions: [], pattern_123: a.pattern,
                  active_region: {ma_label: 'MMS50', ma_value: a.buy_zone.price,
                                  distance_pct: 2.8, low: 825.91, date: '2026-08-29'}}},
        'ZEC-USD', a);
    }""", plano)
    page.wait_for_timeout(150)


@pytest.mark.integration
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_na_tela_a_faixa_da_media_nao_se_chama_compra(webui, plano):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_resultado(page, webui, plano)
        m = page.evaluate("""() => ({
          legenda: document.querySelector('#chartLegend').textContent,
          setup: document.querySelector('#setupCards .sc-recuo').innerText,
          nota: document.querySelector('.chart-note') ? document.querySelector('.chart-note').textContent : '',
        })""")
        # DENTE: antes a legenda trazia a faixa rotulada só "compra", encostada no
        # "1-2-3 de compra" do próprio padrão — duas coisas, um nome.
        assert "recuo à média (MMS50)" in m["legenda"], m
        assert not re.search(r"(^|[^a-zà-ú])compra([^a-zà-ú]|$)",
                             m["legenda"].replace("1-2-3 de compra", "")
                                          .replace("recuo à média", "")), m
        # o veredito diz DE QUAL setup veio — desde a 021 ele mora DENTRO do card
        # da leitura que o produziu, e o título do card é o nome dela
        assert "Setup ativo agora" in m["setup"] and "Recuo à média" in m["setup"], m
        browser.close()


@pytest.mark.integration
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_na_tela_a_faixa_fora_do_preco_declara_que_nao_esta_ativa(webui, plano):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        _abre_resultado(page, webui, plano)
        m = page.evaluate("""() => ({
          legenda: document.querySelector('#chartLegend').textContent,
          card: document.querySelector('#setupCards .sc-recuo').innerText,
          nota: [...document.querySelectorAll('.chart-note')].map(e => e.textContent).join(' '),
        })""")
        assert "não ativa agora" in m["legenda"], m
        # A frase parou de afirmar e desmentir na mesma linha. Desde a 021 ela mora no
        # card do RECUO À MÉDIA (a leitura de quem ela fala), e não na nota do gráfico
        # — que voltou a ser só sobre o desenho e não repete mais nível nenhum.
        assert "fora da faixa" in m["card"], m
        assert "preço na MMS50" not in m["card"], m
        assert "Recuo à média" in m["card"], m
        assert "MMS50" not in m["nota"], ("a nota não fala mais de nível", m)
        browser.close()

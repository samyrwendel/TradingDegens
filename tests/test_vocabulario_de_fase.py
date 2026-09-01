"""UM eixo temporal para a tela inteira (task 20260831-021).

O Samyr leu *"Ativo"* como *"em movimento para o alvo"* — invertido: `ativo` quer
dizer que o preço está TOCANDO a entrada agora. O dono do produto supor errado é
a prova do defeito, e o defeito não era a palavra solta: eram TRÊS taxonomias
convivendo sem tradução (lateral/card, scan, e — desde a DA-117 — sinais).

O que estes testes travam:

* **a palavra "Ativo" não volta.** É o único rótulo do produto cuja leitura
  natural em português aponta para a fase errada;
* **o espelho JavaScript não diverge da autoridade Python.** Sem esta solda, a
  próxima palavra nova nasce de um lado só — que é exatamente como as três
  taxonomias apareceram;
* **toda superfície fala as MESMAS quatro palavras**, e o mecanismo continua
  existindo ao lado (nada some — invariante da DA-078);
* **o mesmo ativo, no mesmo momento, conta a MESMA história** na lateral e no
  scan: o teste percorre os estados e compara as FASES, não os rótulos.
"""

import json
import re
import threading
from pathlib import Path

import pytest

from tradingagents.webui import fases

pytestmark = pytest.mark.unit

_APP = (Path(__file__).resolve().parents[1] / "tradingagents" / "webui" / "static"
        / "app.js").read_text(encoding="utf-8")


def _tabela_js(nome: str) -> dict:
    """Lê um objeto literal simples do app.js — `chave: "valor"` ou `chave: null`.

    Linhas de comentário (`//`) são descartadas antes: elas contêm exemplos que a
    regex leria como entradas da tabela.
    """
    m = re.search(rf"const {nome} = \{{(.*?)\n\}};", _APP, re.S)
    assert m, f"{nome} não encontrada no app.js"
    corpo = "\n".join(L for L in m.group(1).splitlines()
                      if not L.strip().startswith("//"))
    out = {}
    for chave, aspas, nulo in re.findall(r'(\w+)\s*:\s*(?:"([^"]*)"|(null))', corpo):
        out[chave] = None if nulo else aspas
    return out


# ------------------------------------------------ a palavra que induzia ao erro --
def test_a_palavra_Ativo_nao_volta_para_a_tela():
    """DENTE: "Ativo" e "Setup ativo agora" apontavam para a fase ERRADA.

    A busca é pelo RÓTULO (o que o usuário lê), não pela chave `ativo` do dado —
    essa continua existindo e deve continuar: o estado não mudou, mudou o nome
    dele na tela.
    """
    # Só o CÓDIGO — comentários citam a palavra velha de propósito, pra explicar
    # por que ela saiu (a mesma armadilha do portão de pictograma da DA-076).
    codigo = "\n".join(L for L in _APP.splitlines() if not L.strip().startswith("//"))
    for rotulo in ('ativo: "Ativo"', '"Setup ativo agora"'):
        assert rotulo not in codigo, f"{rotulo} voltou para a tela"
    assert 'ativo: "Na entrada agora"' in codigo
    assert 'ativo: "Na entrada"' in codigo


def test_a_chave_do_DADO_continua_intacta():
    """Só o rótulo mudou. Trocar a chave quebraria o histórico já gravado."""
    assert fases.DO_SETUP_STATE["ativo"] == "agora"
    assert _tabela_js("FASE_DO_SETUP_STATE")["ativo"] == "agora"


# --------------------------------------------- a solda entre Python e JavaScript --
@pytest.mark.parametrize("nome_js,tabela_py", [
    ("FASE_PT", fases.FASE_PT),
    ("FASE_AJUDA", fases.FASE_AJUDA),
    ("FASE_DO_SETUP_STATE", fases.DO_SETUP_STATE),
    ("FASE_DO_SCAN_ESTADO", fases.DO_SCAN_ESTADO),
    ("MECANISMO_PT", fases.MECANISMO_PT),
])
def test_o_espelho_JS_e_igual_a_autoridade_PYTHON(nome_js, tabela_py):
    """DENTE: sem isto, a palavra nova nasce de um lado só e a tela volta a ter
    duas taxonomias — que é a origem do defeito desta task."""
    assert _tabela_js(nome_js) == tabela_py, nome_js


def test_a_oportunidade_tambem_espelha_e_o_conflito_NAO_tem_fase():
    js = _tabela_js("FASE_DA_OPORTUNIDADE")
    assert js == fases.DA_OPORTUNIDADE
    assert fases.da_oportunidade("conflito") is None, (
        "conflito não é fase: não há um momento do trade a apontar, há dois lados")


# ------------------------------------------------------------- o eixo temporal --
def test_as_fases_cobrem_o_eixo_e_nada_mais():
    assert list(fases.FASE_PT) == list(fases.ORDEM)
    assert set(fases.FASE_PT) == set(fases.FASE_AJUDA)
    # A ordem é a do TEMPO. `encerrado` entrou na DA-125 e vem depois de `andou` e
    # ANTES de `morreu`, porque é isso que ele é: o trade chegou ao fim — e um
    # trade encerrado não se invalida depois.
    assert fases.ORDEM[:5] == ("agora", "esperando", "andou", "encerrado", "morreu")


def test_ENCERRADO_e_uma_fase_e_nao_um_tipo_de_invalidacao():
    """DENTE da DA-125: um trade que chegou ao alvo saía "INVALIDADO"."""
    assert fases.de_scan_estado("concluido") == "encerrado"
    assert fases.rotulo("encerrado") == "ENCERRADO"
    assert fases.de_scan_estado("concluido") != fases.de_scan_estado("invalidou")


@pytest.mark.parametrize("tabela", [fases.DO_SETUP_STATE, fases.DO_SCAN_ESTADO])
def test_toda_traducao_aponta_para_uma_fase_que_existe(tabela):
    for estado, fase in tabela.items():
        assert fase in fases.FASE_PT, (estado, fase)


# ---- A MESMA HISTÓRIA na lateral e no scan, para o mesmo momento --------------
#
# É o teste que o critério pede. Ele compara FASES, não rótulos: a lateral fala do
# PLANO e o scan fala da LEITURA de um frame — sujeitos diferentes —, mas quando
# descrevem o mesmo momento do trade têm de cair na mesma fase.
MESMO_MOMENTO = [
    pytest.param("ativo", "em_gatilho", "agora", id="preco_no_ponto_de_entrar"),
    pytest.param("aguardar_rompimento", "formando", "esperando", id="gatilho_ainda_nao_veio"),
    pytest.param("aguardar_pullback", "formando", "esperando", id="esperando_o_recuo"),
    pytest.param("sem_setup", "sem_setup", "sem_leitura", id="sem_leitura"),
    pytest.param("sem_dado", "sem_dado", "sem_leitura", id="sem_dado"),
]


@pytest.mark.parametrize("setup_state,scan_estado,fase", MESMO_MOMENTO)
def test_lateral_e_scan_contam_a_MESMA_historia(setup_state, scan_estado, fase):
    assert fases.de_setup_state(setup_state) == fase
    assert fases.de_scan_estado(scan_estado) == fase
    assert fases.rotulo(fases.de_setup_state(setup_state)) == \
        fases.rotulo(fases.de_scan_estado(scan_estado))


def test_o_que_NAO_tem_par_do_outro_lado_continua_existindo():
    """As taxonomias não se fundem — e é isso que preserva a distinção.

    O `em_movimento` e o `invalidou` do scan não têm equivalente no `setup_state`
    (o plano não descreve "já andou"); o `aguardar_pullback` e o
    `aguardar_rompimento` caem na MESMA fase mas esperam coisas diferentes. É por
    isso que o mecanismo continua ao lado da fase em vez de ser substituído por ela.
    """
    assert fases.de_scan_estado("em_movimento") == "andou"
    assert fases.de_scan_estado("invalidou") == "morreu"
    assert "andou" not in fases.DO_SETUP_STATE.values()
    esperando = [e for e, f in fases.DO_SETUP_STATE.items() if f == "esperando"]
    assert set(esperando) == {"aguardar_pullback", "aguardar_rompimento"}
    mecanismos = {fases.mecanismo(e) for e in esperando}
    assert len(mecanismos) == 2, ("mesma fase, mecanismos DIFERENTES — se colapsarem, "
                                  "a tela perde o que se está esperando")


def test_o_STORM_tem_estados_que_nao_sao_fase_do_trade():
    """Veto do Éden e zona neutra são o FILTRO falando, não o trade andando."""
    assert fases.de_scan_estado("vetado") == "sem_leitura"
    assert fases.de_scan_estado("zona_neutra") == "agora"


def test_estado_desconhecido_cai_em_sem_leitura_e_nao_explode():
    assert fases.de_scan_estado("coisa_nova_que_alguem_inventou") == "sem_leitura"
    assert fases.de_setup_state(None) == "sem_leitura"
    assert fases.rotulo(None) == "" and fases.mecanismo(None) == ""


# ============ O MESMO MOMENTO, NAS TRÊS SUPERFÍCIES, NA TELA ==================
#
# Os testes acima provam a TABELA. Este prova a TELA: o mesmo ativo, no mesmo
# instante, lido na lateral, no scan e nos sinais tem de contar a mesma história.
# É o que o critério pede, e é o que faltava — a tabela pode estar certa e a tela
# continuar mostrando um vocabulário próprio em algum canto.

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None


def _frame_scan(frame, estado, preco=100.0, direcao="compra"):
    """Uma linha do scan. O PREÇO é o que decide a fase da oportunidade:
    gatilho 100, SL 90, TP 130 → o limite da janela (R:R 1:1) cai em 110.
    Preço 100 = na entrada · 120 = janela fechada (já andou) · 95 = ainda não abriu.
    """
    return {"frame": frame, "estado": estado, "direction": direcao, "price": preco,
            "trigger": 100.0, "sl": 90.0, "tp": 130.0, "dist_pct": 0.001,
            "dist_txt": "0.10%", "storm": {"estado": "sem_setup"}}


@pytest.fixture
def servidor(tmp_path):
    from tradingagents.webui.runner import AnalysisRunner
    from tradingagents.webui.server import make_server
    from tradingagents.webui.store import HistoryStore
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "openai",
                     "deep_think_llm": "x", "quick_think_llm": "y"},
        store=HistoryStore(tmp_path))
    httpd = make_server("127.0.0.1", 0, runner=runner)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()


@pytest.mark.integration
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_na_TELA_o_scan_e_os_sinais_falam_as_MESMAS_palavras(servidor):
    """DENTE: antes o scan dizia "EM MOVIMENTO"/"FORMANDO" e os sinais diziam
    "Fora da janela"/"A caminho" — quatro palavras para dois momentos."""
    scan = {"date": "2026-08-31", "frames": ["1d", "4h", "1h"],
            "gerado_em": "2026-08-31T10:00:00-04:00",
            "resumo": {"em_gatilho": 1, "em_movimento": 1, "formando": 1, "invalidou": 1},
            "ativos": [
                {"ticker": "AAA", "melhor": _frame_scan("1d", "em_gatilho"),
                 "frames": [_frame_scan("1d", "em_gatilho")]},
                {"ticker": "BBB", "melhor": _frame_scan("1d", "em_movimento", 120.0),
                 "frames": [_frame_scan("1d", "em_movimento", 120.0)]},
                {"ticker": "CCC", "melhor": _frame_scan("1d", "formando", 95.0),
                 "frames": [_frame_scan("1d", "formando", 95.0)]},
                {"ticker": "DDD", "melhor": _frame_scan("1d", "invalidou", 80.0),
                 "frames": [_frame_scan("1d", "invalidou", 80.0)]},
            ]}
    # As oportunidades vêm do MESMO motor que o servidor usa (a visão de Sinais
    # lê `data.oportunidades`); calculá-las aqui mantém o mock fiel à rota real.
    from tradingagents.webui import sinais as _sinais
    scan["oportunidades"] = _sinais.oportunidades(scan)

    def handler(route):
        url = route.request.url
        if "/api/scan" in url and "verdicts" not in url and "/salvo" not in url:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(scan))
        else:
            route.continue_()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.route(re.compile(r"/api/"), handler)
        page.goto(servidor, wait_until="networkidle")
        page.evaluate("() => localStorage.clear()")
        page.reload(wait_until="networkidle")
        page.click("#scanOpenBtn")
        page.wait_for_selector("#scanList .sn-card", timeout=10000)

        secoes = page.evaluate(
            "() => [...document.querySelectorAll('.sn-secao-tit')]"
            ".map(function (e) { return e.textContent.toUpperCase(); })")
        resumo = page.evaluate(
            "() => document.getElementById('scanSummary').textContent.toUpperCase()")
        filtros = page.evaluate(
            "() => [...document.querySelectorAll('.scan-filter')]"
            ".map(function (e) { return e.textContent.trim().toUpperCase(); })")
        page.click('.scan-view[data-view="cards"]')
        page.wait_for_selector("#scanList .scan-tk", timeout=5000)
        chips = page.evaluate(
            "() => [...document.querySelectorAll('#scanList .scan-chip')]"
            ".map(function (e) { return e.textContent.trim().toUpperCase(); })")

        # As palavras do EIXO aparecem nos DOIS lugares, escritas igual.
        assert "JÁ ANDOU" in chips and "AGUARDANDO" in chips, chips
        assert "INVALIDADO" in chips, chips
        assert "JÁ ANDOU" in secoes and "AGUARDANDO" in secoes, secoes
        assert "NA ENTRADA" in secoes, secoes
        # e o vocabulário antigo não sobrou em canto nenhum
        # e os CHIPS DE FILTRO também — "EM GATILHO" era a última sobra de um
        # quinto jeito de nomear o mesmo momento
        assert any(f.startswith("NA ENTRADA") for f in filtros), filtros
        # o RESUMO é a linha mais visível da tela — foi a última a manter o
        # vocabulário antigo, e é onde a incoerência salta primeiro
        assert "NA ENTRADA" in resumo and "JÁ ANDOU" in resumo, resumo
        assert "AGUARDANDO" in resumo and "INVALIDADO" in resumo, resumo
        todos = chips + secoes + filtros + [resumo]
        for velha in ("EM MOVIMENTO", "FORMANDO", "INVALIDOU", "A CAMINHO",
                      "FORA DA JANELA", "ENTRADA AGORA", "EM GATILHO"):
            assert not any(velha in t for t in todos), (velha, todos)
        browser.close()

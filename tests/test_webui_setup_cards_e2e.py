"""E2E: UM CARD POR ANÁLISE (task de UI 021).

Pedido do Samyr, olhando o print do ZEC-USD: "são duas análises, uma do 123 e outra
do veredito, e podem ficar em cards específicos".

O que a tela mostrava: uma tira no cabeçalho com "PLANO gatilho 834,82 SL 764,76 TP
856,72 R:R 0,31" e, mais abaixo, numa caixa solta e sem vínculo visual nenhum,
"Setup ativo agora · recuo à média" com Horizonte e Timeframe. Não havia como saber
que a tira de cima e a caixa de baixo falavam de coisas DIFERENTES — e o R:R saía
nas duas, mais uma terceira vez na nota do gráfico.

O código já sabia disso: o comentário da task 015 diz "São dois independentes,
desenhados na mesma tela, que podem coexistir e discordar: o RECUO À MÉDIA (faixa
verde) e o 1-2-3". A 015 resolveu o NOME de cada um (``setup_source``); a 021
resolve a ORGANIZAÇÃO.

A DIVISÃO SAI DO DADO — cada campo do ``actionable`` tem um dono determinado por
como ``price_structure.build_actionable_plan`` o produz:

  • 1-2-3         → pattern, invalidation, stop, target, risk_reward (os cinco saem
                    de ``_pattern_levels(struct.pattern, …)`` e são None JUNTOS);
  • recuo à média → buy_zone (e o pullback que é recuo);
  • de ninguém    → price/as_of/timeframe e a cotação — o chão comum;
  • setup_state + horizon → vão pro card que ``setup_source`` NOMEAR.

Os três invariantes que este arquivo trava:
  (a) nenhum número aparece em DUAS superfícies (cabeçalho × card × card × nota);
  (b) discordância entre as duas leituras lê como DUAS LEITURAS — cada uma no seu
      card, com o carimbo do veredito na que de fato decidiu;
  (c) leitura que não existe no dado não abre card vazio nem inventa "—".
"""

import json
import re
import threading

import pytest

from tradingagents.webui import timeutil
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.server import make_server
from tradingagents.webui.store import HistoryStore

# A cotação só vale como ATUAL no dia em que foi tirada (DA-073), e o front confere
# isso contra o dia de HOJE. Carimbar uma data FIXA na fixture faz o teste passar o
# dia todo e quebrar sozinho à meia-noite — foi o que aconteceu na virada de 29 pra
# 30/08. O carimbo acompanha o relógio do servidor, que é a mesma fonte que o front lê.
_HOJE = timeutil.today()

pytestmark = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None


# ---------------------------------------------------------------------------
# O caso do print: as duas leituras EXISTEM e DISCORDAM. O recuo à média diz
# "setup ativo agora" (preço dentro da faixa da MMS50, e é dele que o veredito
# saiu — setup_source), enquanto o 1-2-3 diz "rompeu e retraçou (não confirmado)".
# ---------------------------------------------------------------------------
_ACT = {
    "symbol": "ZEC-USD", "price": 834.74, "as_of": "2026-08-29 20:00",
    "timeframe": "4h", "horizon": "dias", "setup_state": "ativo",
    "setup_source": "recuo_media",
    "buy_zone": {"label": "MMS50 — preço na média agora", "price": 806.67,
                 "low": 790.32, "high": 815.92, "band_basis": "±0.5·ATR14",
                 "ma_label": "MMS50", "setup": "recuo_media",
                 "tag": "recuo à média (MMS50)", "active_now": True,
                 "distance_pct": 3.5},
    "realize_zone": None, "pullback_zone": None,
    "pattern": {"p1": {"date": "2026-06-10", "price": 690.0},
                "p2": {"date": "2026-07-02", "price": 834.82},
                "p3": {"date": "2026-07-20", "price": 790.29},
                "trigger": 834.82, "state": "rompeu_retracou", "direction": "compra"},
    "invalidation": {"label": "perda do ponto 3 (2026-07-20)", "price": 790.29,
                     "meaning": "o setup morre se perder 790,29 — abaixo do ponto 3 "
                                "o fundo ascendente deixa de ser ascendente"},
    "stop": {"label": "stop (SL)", "price": 764.76, "anchor": 790.29, "atr": 51.06,
             "basis": "invalidação + folga de 0.5·ATR14"},
    "target": {"label": "topo anterior 2026-07-02", "price": 856.72,
               "same_as_realize": False},
    "risk_reward": {"entry": 834.82, "entry_basis": "gatilho", "risk": 70.06,
                    "reward": 21.9, "rr": 0.31, "note": None},
}


def _snap(actionable):
    return {
        "run_id": "R-021", "ticker": "ZEC-USD", "date": "2026-08-29",
        "asset_type": "crypto", "status": "done", "elapsed": 1, "cost": {"usd": 0.0},
        "verdict": "HOLD", "verdict_timeframe": "4h",
        "result": {
            "setup123": True, "verdict": "HOLD", "final_decision": "MANTER",
            "timeframe": "4h", "as_of_price": 834.74, "actionable": actionable,
            "live_price": {"price": 835.37, "change_pct": 4.32, "currency": "USD",
                           "sessao": "24h", "rotulo": "cotação agora · 24h",
                           "as_of": "29/08 20:42", "regular_price": 835.37,
                           "fuso": "UTC", "em": _HOJE},
            "price_chart": {}, "degraded": [],
            "bull": "", "bear": "", "research_manager": "", "investment_plan": "",
            "trader_plan": "", "risk_decision": "", "market_report": "",
            "sentiment_report": "", "news_report": "", "fundamentals_report": "",
            "erick_report": "", "drop_nature": {}, "derivatives_report": "",
        },
    }


def _sem(campos, **over):
    """Cópia do plano sem os campos pedidos (para os casos de leitura ausente)."""
    a = json.loads(json.dumps(_ACT))
    for c in campos:
        a[c] = None
    a.update(over)
    return a


@pytest.fixture
def base(tmp_path):
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


def _abre(page, base_url, actionable, largura=1500):
    snap = _snap(actionable)

    def handler(route):
        url = route.request.url
        if "/api/status/" in url or re.search(r"/api/run/[^/]+$", url):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(snap))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)
    page.goto(base_url, wait_until="networkidle")
    page.evaluate("() => watchRun('R-021')")
    page.wait_for_selector("#setupCards:not(.hidden)")
    page.wait_for_timeout(120)


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_cada_analise_ganha_card_proprio_com_titulo_dizendo_qual_e(base):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1100})
        _abre(page, base, _ACT)
        m = page.evaluate("""() => ({
          titulos: [...document.querySelectorAll('#setupCards .setup-card .sc-title')]
            .map(e => e.innerText.trim()),
          classes: [...document.querySelectorAll('#setupCards .setup-card')]
            .map(e => e.className),
          // DA-070: card quadrado (o raio único do projeto) e ZERO degradê
          estilo: (() => { const c = getComputedStyle(document.querySelector('.sc-123'));
            return {raio: c.borderTopLeftRadius, fundo: c.backgroundImage}; })(),
        })""")
        assert len(m["titulos"]) == 2, m
        assert "Setup123" in m["titulos"][0], m
        assert "Recuo à média" in m["titulos"][1], m
        assert any("sc-123" in c for c in m["classes"]), m
        assert any("sc-recuo" in c for c in m["classes"]), m
        assert m["estilo"]["fundo"] == "none", ("DA-070: nada de degradê", m)
        assert m["estilo"]["raio"] == "2px", ("DA-078 regra 1: raio máximo 2px", m)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_discordancia_le_como_duas_leituras_e_nao_como_contradicao(base):
    """Invariante (b). O recuo diz "ativo agora"; o 1-2-3 diz "rompeu e retraçou".
    Amontoados num bloco só, isso lia como a tela se contradizendo. Cada um no seu
    card — e o carimbo do VEREDITO na leitura que de fato decidiu — vira o que é:
    duas leituras independentes, e dá pra ver qual delas o plano seguiu."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1100})
        _abre(page, base, _ACT)
        m = page.evaluate("""() => {
          const t = (s) => { const e = document.querySelector(s); return e ? e.innerText : ""; };
          return {c123: t('#setupCards .sc-123'), recuo: t('#setupCards .sc-recuo'),
                  carimbos: [...document.querySelectorAll('#setupCards .sc-verdict')]
                    .map(e => e.closest('.setup-card, .sc-foot').className)};
        }""")
        # cada leitura declara o SEU estado, dentro da SUA caixa
        assert "rompeu e retraçou" in m["c123"], m
        assert "rompeu e retraçou" not in m["recuo"], m
        assert "preço na faixa" in m["recuo"], m
        # o veredito é UM só, e mora no card da leitura que o produziu (setup_source)
        assert len(m["carimbos"]) == 1, m
        assert "sc-recuo" in m["carimbos"][0], ("setup_source = recuo_media", m)
        # DA-121: a fase substituiu "Setup ativo agora" — mesmo card, palavra do eixo.
        assert "Na entrada agora" in m["recuo"], m
        assert "VEREDITO DO PLANO" in m["recuo"].upper(), m
        assert "veredito" not in m["c123"].lower(), ("o 1-2-3 não decidiu este plano", m)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_carimbo_do_veredito_segue_o_setup_source_e_nao_a_posicao_na_tela(base):
    """O mesmo plano com ``setup_source`` no 1-2-3: o carimbo TROCA de card. Se ele
    ficasse fixo no primeiro card, o dado deixaria de mandar na tela."""
    plano = json.loads(json.dumps(_ACT))
    plano["setup_source"] = "123"
    plano["setup_state"] = "aguardar_rompimento"
    plano["horizon"] = "dias a semanas"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1100})
        _abre(page, base, plano)
        m = page.evaluate("""() => ({
          c123: document.querySelector('#setupCards .sc-123').innerText,
          recuo: document.querySelector('#setupCards .sc-recuo').innerText,
        })""")
        assert "VEREDITO DO PLANO" in m["c123"].upper(), m
        assert "Aguardar rompimento" in m["c123"], m
        assert "dias a semanas" in m["c123"], ("o horizonte é do veredito", m)
        assert "VEREDITO" not in m["recuo"].upper(), m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize(
    "campos,presente,ausente",
    [(["buy_zone"], ".sc-123", ".sc-recuo"),
     (["pattern", "invalidation", "stop", "target", "risk_reward"], ".sc-recuo", ".sc-123")],
)
def test_leitura_que_nao_existe_nao_abre_card_vazio(base, campos, presente, ausente):
    """Invariante (c): sem padrão não há card de 1-2-3; sem faixa não há card de
    recuo. Nada de caixa vazia com travessão inventado."""
    plano = _sem(campos, setup_source=("123" if "buy_zone" in campos else "recuo_media"))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1100})
        _abre(page, base, plano)
        m = page.evaluate("""(sel) => ({
          presente: !!document.querySelector('#setupCards ' + sel[0]),
          ausente: !!document.querySelector('#setupCards ' + sel[1]),
          n: document.querySelectorAll('#setupCards .setup-card').length,
          // travessão INVENTADO é o que ocupa o lugar de um valor; o travessão
          // dentro de uma frase de invalidação é pontuação, não buraco
          travessoes: [...document.querySelectorAll('#setupCards .sc-v')]
            .filter(e => e.innerText.trim() === '—').length,
        })""", [presente, ausente])
        assert m["presente"] and not m["ausente"], m
        assert m["n"] == 1, m
        assert m["travessoes"] == 0, ("nada de travessão no lugar de um valor", m)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_sem_nenhuma_das_duas_sai_um_card_honesto_e_nao_dois_vazios(base):
    plano = _sem(["buy_zone", "pattern", "invalidation", "stop", "target", "risk_reward"],
                 setup_state="sem_setup", setup_source=None,
                 horizon="sem horizonte operável")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1100})
        _abre(page, base, plano)
        m = page.evaluate("""() => ({
          n: document.querySelectorAll('#setupCards .setup-card').length,
          txt: document.querySelector('#setupCards').innerText,
        })""")
        assert m["n"] == 1, m
        assert "Sem setup de preço definido" in m["txt"], m
        assert "Nem o padrão 1-2-3 nem o recuo à média" in m["txt"], m
        assert "—" not in m["txt"], ("aqui não há nem frase com travessão", m)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_veredito_sem_dono_cai_no_rodape_compartilhado_e_nao_num_card(base):
    """O backend pode não eleger nenhuma das duas leituras (``setup_source`` nulo —
    é o caso do 1-2-3 já ACIONADO sem média ativa). Enfiar o carimbo num card seria
    atribuir a uma leitura um estado que ela não produziu."""
    plano = _sem(["buy_zone"], setup_state="sem_setup", setup_source=None,
                 horizon="sem horizonte operável")
    plano["pattern"]["state"] = "acionado"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1100})
        _abre(page, base, plano)
        m = page.evaluate("""() => ({
          noCard: !!document.querySelector('#setupCards .setup-card .sc-verdict'),
          noRodape: !!document.querySelector('#setupCards .sc-foot .sc-verdict'),
          rodape: document.querySelector('#setupCards .sc-foot').innerText,
          c123: document.querySelector('#setupCards .sc-123').innerText,
        })""")
        assert not m["noCard"] and m["noRodape"], m
        assert "Sem setup de preço definido" in m["rodape"], m
        assert "acionado" in m["c123"], ("o padrão continua dizendo o que ele é", m)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_frame_e_chao_comum_e_sai_uma_vez_so_NO_TOPO(base):
    """O timeframe é de NINGUÉM: as leituras foram calculadas nele. Repetir em cada
    card seria a mesma duplicata que a 021 veio matar — mas ele SUBIU pro topo do
    bloco (task 029): no rodapé, quem lia o card do meio não sabia em que frame
    aquele stop valia sem rolar até o fim."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1100})
        _abre(page, base, _ACT)
        m = page.evaluate("""() => {
          const el = document.getElementById('setupCards');
          const topo = el.querySelector('.sc-frame-topo');
          const cards = el.querySelector('.setup-card');
          return {topo: topo ? topo.innerText : '',
                  topoAntes: topo && cards
                    ? topo.getBoundingClientRect().top < cards.getBoundingClientRect().top
                    : false,
                  c123: el.querySelector('.sc-123').innerText,
                  recuo: el.querySelector('.sc-recuo').innerText};
        }""")
        # o rótulo é caixa alta por CSS e o valor cai em linha própria: compara-se o
        # texto normalizado, não a caixa nem a quebra
        topo = " ".join(m["topo"].split()).lower()
        assert "as leituras no 4h" in topo, m
        assert m["topoAntes"], ("o carimbo vem ANTES dos cards, não depois", m)
        assert "4h" not in m["c123"] and "4h" not in m["recuo"], ("uma vez só", m)
        browser.close()


# ---------------------------------------------------------------------------
# Invariante (a): nenhum número em DUAS superfícies.
#
# É o defeito que o dia inteiro foi passado matando — o TP que empatava com o
# gatilho, o "compra" com dois sentidos, a zona que afirmava com uma régua e
# desenhava com outra. Aqui a régua é literal: para cada número do plano, quantas
# das quatro superfícies de TEXTO o contêm. Tem que ser exatamente uma.
#
# O canvas fica de fora de propósito: ele DESENHA o nível (rótulo pintado na
# linha), não escreve texto no DOM — e o card existe justamente para dizer a BASE
# que o desenho não cabe.
# ---------------------------------------------------------------------------
_SUPERFICIES = """() => {
  const alvo = {cabecalho: '#headLevels', card123: '#setupCards .sc-123',
                cardRecuo: '#setupCards .sc-recuo', notaGrafico: '#chartNote'};
  const out = {};
  for (const [nome, sel] of Object.entries(alvo)) {
    const e = document.querySelector(sel);
    out[nome] = e && !e.classList.contains('hidden') ? e.innerText : '';
  }
  return out;
}"""


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("numero,dono", [
    ("834,82", "card123"),    # gatilho
    ("790,29", "card123"),    # invalidação
    ("764,76", "card123"),    # stop
    ("856,72", "card123"),    # alvo
    ("0,31", "card123"),      # R:R — saía DUAS vezes antes da 021
    ("835,37", "cabecalho"),  # cotação de mercado: chão comum, de leitura nenhuma
    ("834,74", "cabecalho"),  # preço da análise
])
def test_nenhum_numero_aparece_em_duas_superficies(base, numero, dono):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1100})
        _abre(page, base, _ACT)
        sup = page.evaluate(_SUPERFICIES)
        onde = [k for k, txt in sup.items() if numero in txt]
        assert onde == [dono], (numero, onde, sup)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_cada_card_leva_os_niveis_DELE_e_nao_herda_os_do_outro(base):
    """A regra que vale (correção do Samyr sobre a redação da 021): cada análise
    exibe os SEUS níveis — não é duplicação, são leituras diferentes com números
    diferentes por construção. O que é proibido é o MESMO número da MESMA análise em
    dois lugares, e HERDAR nível de outra leitura (pior que omitir: daria ao leitor
    um stop que esta análise não calculou).

    O 1-2-3 tem gatilho, invalidação, stop, alvo e R:R. O recuo à média tem entrada
    na faixa da média e a região de realização — e não tem stop nem invalidação,
    então nem linha eles ganham aqui."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1100})
        _abre(page, base, _ACT)
        m = page.evaluate("""() => ({
          c123: document.querySelector('#setupCards .sc-123').innerText,
          recuo: document.querySelector('#setupCards .sc-recuo').innerText,
          chaves123: [...document.querySelectorAll('#setupCards .sc-123 .sc-k')]
            .map(e => e.innerText.trim()),
          chavesRecuo: [...document.querySelectorAll('#setupCards .sc-recuo .sc-k')]
            .map(e => e.innerText.trim()),
        })""")
        # o 1-2-3 leva o conjunto COMPLETO dele
        assert m["chaves123"] == ["gatilho", "invalidação", "stop (SL)", "alvo (TP)",
                                  "risco/retorno"], m
        for num in ("834,82", "790,29", "764,76", "856,72", "0,31"):
            assert num in m["c123"], (num, m["c123"])
        # o recuo leva os DELE — entrada na faixa da média e a distância
        assert "entrada na MMS50" in m["chavesRecuo"], m
        assert "806,67" in m["recuo"] and "790,32–815,92" in m["recuo"], m
        assert "3,5% acima" in m["recuo"] and "dentro da faixa" in m["recuo"], m
        # e NÃO herda o que é do 1-2-3: nível que esta leitura não calcula não aparece
        assert not any(k in ("stop (SL)", "invalidação", "gatilho", "risco/retorno")
                       for k in m["chavesRecuo"]), m
        for num in ("764,76", "790,29", "834,82", "0,31"):
            assert num not in m["recuo"], (num, m["recuo"])
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_quando_as_duas_leituras_convergem_num_nivel_as_duas_dizem_isso(base):
    """O único caso em que o mesmo número sai nos dois cards: o backend carimba
    ``same_as_realize`` porque o alvo do 1-2-3 É a região de realização. Não é
    duplicata pra reconciliar — é convergência, e cada card declara que é."""
    plano = json.loads(json.dumps(_ACT))
    plano["realize_zone"] = {"label": "topo anterior 2026-07-02", "price": 856.72,
                             "low": 850.0, "high": 863.0, "band_basis": "±0.5·ATR14",
                             "role": "alvo", "role_label": "realização parcial"}
    plano["target"]["same_as_realize"] = True
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1100})
        _abre(page, base, plano)
        m = page.evaluate("""() => ({
          c123: document.querySelector('#setupCards .sc-123').innerText,
          recuo: document.querySelector('#setupCards .sc-recuo').innerText,
        })""")
        assert "856,72" in m["c123"] and "856,72" in m["recuo"], m
        assert "mesmo nível da região de realização" in m["c123"], m
        assert "as duas leituras convergem neste nível" in m["recuo"], m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_regiao_que_e_o_proprio_gatilho_nao_sai_nos_dois_cards(base):
    """Quando a região de realização É o gatilho do 1-2-3 (``role: gatilho``), o
    número já pertence ao outro card — e aí ele NÃO sai aqui. É a mesma regra que o
    gráfico usa pra não traçar a linha duas vezes."""
    plano = json.loads(json.dumps(_ACT))
    plano["realize_zone"] = {"label": "topo anterior", "price": 834.82,
                             "role": "gatilho", "role_label": "realização = gatilho do 1-2-3"}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1100})
        _abre(page, base, plano)
        m = page.evaluate("""() => ({
          c123: document.querySelector('#setupCards .sc-123').innerText,
          recuo: document.querySelector('#setupCards .sc-recuo').innerText,
        })""")
        assert "834,82" in m["c123"], m
        assert "834,82" not in m["recuo"], m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_os_cards_nao_usam_pictograma(base):
    """DA-076 ("tira todos os emojis"): nesta superfície nova a regra já entra
    valendo. O que o pictograma marcava — qual leitura é, compra × venda, estado —
    passa a ser COR + PALAVRA: a borda do card sai na cor que o gráfico usa pra
    aquela marcação, e o nome vai escrito."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1100})
        _abre(page, base, _ACT)
        m = page.evaluate("""() => ({
          txt: document.querySelector('#setupCards').innerText,
          borda123: getComputedStyle(document.querySelector('.sc-123')).borderLeftColor,
          bordaRecuo: getComputedStyle(document.querySelector('.sc-recuo')).borderLeftColor,
          dir: document.querySelector('.sc-123 .sc-dir').innerText,
        })""")
        pictos = [c for c in m["txt"] if ord(c) >= 0x2190]
        assert pictos == [], ("pictograma no card (DA-076)", pictos, m["txt"])
        # a cor é a MESMA que o gráfico usa — e desde a DA-140 ela diz DIREÇÃO:
        # verde na compra (era o azul do método), verde também no recuo à média,
        # que é uma faixa de COMPRA.
        assert m["borda123"] == "rgb(46, 204, 113)", m
        assert m["bordaRecuo"] == "rgb(46, 204, 113)", m
        assert m["dir"].strip() == "de compra", ("a direção vai por escrito", m)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_venda_muda_a_cor_do_card_porque_o_emoji_nao_esta_mais_la(base):
    """Consequência que a DA-076 manda cuidar: sem pictograma, tirar sem substituto
    APAGARIA a distinção compra × venda. Ela migra pra cor + palavra."""
    plano = json.loads(json.dumps(_ACT))
    plano["pattern"]["direction"] = "venda"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1100})
        _abre(page, base, plano)
        m = page.evaluate("""() => ({
          borda: getComputedStyle(document.querySelector('.sc-123')).borderLeftColor,
          dir: document.querySelector('.sc-123 .sc-dir').innerText,
          base: [...document.querySelectorAll('.sc-123 .sc-basis')].map(e => e.innerText),
        })""")
        assert m["borda"] == "rgb(255, 92, 108)", ("vermelho do 1-2-3 de venda", m)
        assert m["dir"].strip() == "de venda", m
        assert any("perda da mínima do ponto 2" in b for b in m["base"]), m
        browser.close()


# ───────────── o frame não depende do rodapé (task 20260830-029) ──────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("w,h", [(390, 844), (1500, 1100)])
def test_o_carimbo_de_frame_NAO_SAI_DA_VISTA_ao_rolar_os_cards(base, w, h):
    """O pedido, literal: "identificar qual timeframe pertence a análise". No print,
    três cards com gatilho, stop e alvo — e o frame só no rodapé, em cinza, DEPOIS de
    todos. Quem lê o card do meio não sabe em que frame aquele stop vale.

    O carimbo subiu pro topo e é GRUDADO: no celular os cards passam de uma tela, e
    um carimbo que sai de vista volta a ser rodapé."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": w, "height": h})
        _abre(page, base, _ACT, largura=w)
        m = page.evaluate("""() => {
          const el = document.getElementById('setupCards');
          const c = el.querySelector('.sc-frame-topo');
          // rola até o ÚLTIMO card — o instante em que o rodapé ainda não apareceu
          const ultimo = [...el.querySelectorAll('.setup-card')].pop();
          ultimo.scrollIntoView({block: 'center'});
          const r = c.getBoundingClientRect();
          return {grudado: getComputedStyle(c).position,
                  topo: Math.round(r.top), altura: Math.round(r.height),
                  naTela: r.top >= -1 && r.bottom <= window.innerHeight + 1,
                  txt: " ".join ? '' : '',
                  texto: c.innerText.replace(/\\s+/g, ' ').trim()};
        }""")
        assert m["grudado"] == "sticky", ("o carimbo tem de acompanhar a rolagem", m)
        assert m["altura"] > 0, m
        assert m["naTela"], ("com os cards rolados, o frame continua visível", m)
        assert "4h" in m["texto"], m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_card_de_frame_DIFERENTE_do_bloco_salta(base):
    """O outro lado: se um card for de outro timeframe, isso não pode ficar escondido
    sob o carimbo do bloco — um card lido sob o frame errado é um stop lido no frame
    errado."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1100})
        _abre(page, base, _ACT)
        m = page.evaluate("""(base) => {
          // o mesmo plano, com o Storm vindo de OUTRO frame
          const a = JSON.parse(JSON.stringify(base));
          a.storm = {opera: true, qualidade: "boa", veto: null, motivo: "",
                     timeframe: "1 hora (intradiário)",
                     eden: {disponivel: true, ema_rapida: 1, ema_lenta: 2, motivo: ""},
                     pattern: {p1: {date: "2026-08-24", price: 1},
                               p2: {date: "2026-08-25", price: 2},
                               p3: {date: "2026-08-26", price: 3},
                               direction: "compra", amplitude: 1},
                     invalidation: {price: 1, meaning: ""}, stop: {price: 1, basis: ""},
                     leituras: []};
          renderSetupCards(a);
          const st = document.querySelector('#setupCards .sc-storm');
          const outros = document.querySelector('#setupCards .sc-123');
          return {storm: st ? st.innerText : '',
                  marca: st ? !!st.querySelector('.sc-frame-card') : false,
                  outroMarcado: outros ? !!outros.querySelector('.sc-frame-card') : false};
        }""", _ACT)
        assert m["marca"], ("o card de outro frame tem de se identificar", m)
        assert "1 hora" in m["storm"].lower(), ("o rótulo é caixa alta por CSS",
                                                 m["storm"])
        assert not m["outroMarcado"], ("quem é do frame do bloco NÃO repete", m)
        browser.close()

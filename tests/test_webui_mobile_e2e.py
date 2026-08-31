"""A tela no CELULAR (task de UI 020) — "tá bem zoado pra celular essas informações".

Print do Samyr (ZEC-USD, 1-2-3, Chrome Android). Cinco defeitos, todos medidos em
viewport de telefone de verdade (390×844 e 360×800), não em desktop encolhido:

1. a tira do PLANO quebrava deixando o R:R órfão numa segunda fileira — quebra por
   ACIDENTE, e justo no número que decide o trade;
2. os dois preços (cotação × análise) viravam sopa de número e data à direita;
3. a legenda do gráfico, com 11 itens, comia 4 linhas ANTES do gráfico aparecer;
4. os rótulos das faixas eram longos demais pro viewport e saíam CORTADOS;
5. e por isso mesmo colidiam com as pílulas de preço da régua direita.

Mais um ponto de leitura, que é semântica e não layout: R:R 0,31 arrisca ~3x o que
pretende ganhar, e a tela mostrava o número em cor neutra. O gráfico já pintava de
âmbar dentro do canvas quando rr < 1 (task 029) — o texto passa a usar a mesma
gramática.

O que este arquivo NÃO deixa acontecer de novo: qualquer um dos cinco voltar, e o
desktop mudar de forma pra pagar por eles.
"""

import json
import re
import threading
from pathlib import Path

import pytest

from tradingagents.webui import timeutil
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.server import make_server
from tradingagents.webui.store import HistoryStore

pytestmark = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None

# A run REAL do print (ZEC-USD 4h, 29/08) — 260 candles, quatro faixas de plano e
# R:R 0,31. Fixture congelada: é o caso exato da queixa, não um sintético parecido.
_FIXTURE = Path(__file__).with_name("data") / "run_zec_usd_4h_20260829.json"

CELULARES = [(390, 844), (360, 800)]


@pytest.fixture
def snap():
    d = json.loads(_FIXTURE.read_text())
    d["run_id"] = "R-ZEC"
    # A fixture é uma run REAL congelada (29/08). A cotação só é tratada como ATUAL
    # no dia em que foi tirada (DA-073), então o carimbo tem de acompanhar o relógio:
    # com a data fixa, estes testes passavam o dia todo e quebravam à meia-noite.
    live = (d.get("result") or {}).get("live_price")
    if isinstance(live, dict):
        live["em"] = timeutil.today()
    return d


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


def _abre(page, base, snap):
    def handler(route):
        url = route.request.url
        if "/api/status/" in url or re.search(r"/api/run/[^/]+$", url):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(snap))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)
    page.goto(base, wait_until="networkidle")
    page.evaluate("() => watchRun('R-ZEC')")
    page.wait_for_selector("#chartCard:not(.hidden)")
    page.wait_for_timeout(400)


def _celular(browser, w, h):
    return browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=3,
                            is_mobile=True, has_touch=True)


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("w,h", CELULARES)
def test_no_telefone_os_niveis_do_plano_ficam_um_por_linha_e_nada_vaza(base, snap, w, h):
    """Item 1 na forma da 021. O defeito era a tira do PLANO quebrar por ACIDENTE e
    deixar o R:R órfão numa segunda fileira; a 020 respondeu com uma grade 2×2. Os
    níveis agora são LINHAS do card do 1-2-3 — a quebra deixou de ser possível: cada
    nível ocupa a sua linha, o número encosta à direita e nada sai da caixa."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = _celular(browser, w, h)
        _abre(page, base, snap)
        m = page.evaluate("""() => {
          const card = document.querySelector('#setupCards .sc-123');
          const c = card.getBoundingClientRect();
          const rows = [...card.querySelectorAll('.sc-row')];
          const tops = rows.map(e => Math.round(e.getBoundingClientRect().top));
          return {
            n: rows.length,
            fileiras: [...new Set(tops)].length,
            // o NÚMERO de cada linha alinha com o dos outros: coluna, não fila
            direitas: [...new Set(rows.filter(e => e.querySelector('.sc-v'))
              .map(e => Math.round(e.querySelector('.sc-v').getBoundingClientRect().right)))],
            fora: rows.filter(e => { const r = e.getBoundingClientRect();
              return r.left < c.left - 1 || r.right > c.right + 1; }).map(e => e.innerText),
            rola: document.documentElement.scrollWidth > document.documentElement.clientWidth,
            txt: card.innerText,
          };
        }""")
        # cada nível na SUA linha — nenhum divide fileira com outro
        assert m["n"] >= 4 and m["fileiras"] == m["n"], m
        # DENTE: era o R:R sobrando desalinhado; aqui todos os números alinham na
        # mesma coluna, então "ficar órfão" deixou de existir como estado
        assert len(m["direitas"]) == 1, ("os números numa coluna só", m)
        assert m["fora"] == [] and not m["rola"], m
        for chave in ("gatilho", "stop (SL)", "risco/retorno"):
            assert chave in m["txt"], (chave, m["txt"])
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("w,h", CELULARES)
def test_no_telefone_cada_preco_tem_a_sua_linha_e_o_rotulo_vem_na_frente(base, snap, w, h):
    """Item 2: "cotação agora" e "análise" viravam sopa. No telefone o que se procura
    primeiro é QUAL é o de agora — então o rótulo lidera, uma unidade por linha, e as
    duas encostam na ESQUERDA (à direita, cada linha começava num lugar).

    TRÊS unidades desde o commit 185ba75 (task 037), não duas: a DISTÂNCIA entre
    cotação e análise — antes implícita, o usuário tinha que subtrair de cabeça —
    ganhou linha própria ENTRE as outras duas. É a mesma unidade de informação que
    a 020 já tratava (número · o que é · alinhado à esquerda), só que agora são três
    ao invés de duas; o contrato original (cada uma na sua linha, rótulo primeiro,
    sem sopa) continua valendo e é o que este teste segue medindo."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = _celular(browser, w, h)
        _abre(page, base, snap)
        m = page.evaluate("""() => {
          const un = [...document.querySelectorAll('#headPrice .hp-unit')];
          const r = (e) => e.getBoundingClientRect();
          const live = document.querySelector('.hp-live');
          return {
            tops: un.map(e => Math.round(r(e).top)),
            esquerdas: [...new Set(un.map(e => Math.round(r(e).left)))],
            rotuloAntesDoNumero: r(live.querySelector('.hp-tag')).left < r(live.querySelector('b')).left,
            regua: getComputedStyle(document.querySelector('.hp-ref')).borderLeftWidth,
            textos: un.map(e => e.innerText.replace(/\\n/g, ' ')),
          };
        }""")
        assert len(set(m["tops"])) == 3, ("uma unidade por linha (cotação/distância/análise)", m)
        assert len(m["esquerdas"]) == 1, ("as três alinhadas pela esquerda", m)
        assert m["rotuloAntesDoNumero"], ("no telefone o rótulo lidera", m)
        # a régua separava as unidades LADO A LADO; empilhadas, ela não separa nada
        assert m["regua"] == "0px", m
        assert "COTAÇÃO AGORA" in m["textos"][0].upper() and "835,37" in m["textos"][0], m
        # a unidade do meio é a distância explícita entre as duas (task 037): sem
        # ela o usuário tinha que subtrair 835,37 − 834,74 de cabeça
        assert "DISTÂNCIA" in m["textos"][1].upper() and "0,63" in m["textos"][1], m
        assert "ANÁLISE" in m["textos"][2].upper() and "834,74" in m["textos"][2], m
        # cada carimbo continua com o SEU preço (nada foi amputado pra caber)
        assert "29/08 20:42" in m["textos"][0] and "29/08 20:00" in m["textos"][2], m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("w,h", CELULARES)
def test_a_legenda_desce_pra_depois_do_grafico_sem_perder_item(base, snap, w, h):
    """Item 3: 11 itens × 4 linhas ANTES do gráfico é meia tela de rolagem pra chegar
    no que interessa. Ela desce — e continua INTEIRA (esconder seria amputar)."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = _celular(browser, w, h)
        _abre(page, base, snap)
        m = page.evaluate("""() => {
          const leg = document.querySelector('#chartLegend').getBoundingClientRect();
          const gr = document.querySelector('.chart-wrap').getBoundingClientRect();
          return {legTop: Math.round(leg.top), grTop: Math.round(gr.top),
                  grBottom: Math.round(gr.bottom),
                  itens: document.querySelectorAll('#chartLegend .lg').length,
                  visivel: getComputedStyle(document.querySelector('#chartLegend')).display};
        }""")
        # DENTE: a legenda vinha ANTES (legTop < grTop)
        assert m["legTop"] >= m["grBottom"], ("a legenda tem que ficar DEPOIS do gráfico", m)
        # NENHUM ITEM SUME POR CAUSA DA LARGURA. O número deixou de ser fixo em 11
        # quando a DA-088 fez a legenda descrever o DESENHO em vez do payload: o
        # método aberto não traça as sete médias das duas famílias. O invariante é o
        # mesmo — a legenda do telefone tem de ser IGUAL à do desktop, item por item;
        # o que ela não pode é encolher pra caber.
        largo = browser.new_page(viewport={"width": 1500, "height": 1000})
        _abre(largo, base, snap)
        ref = largo.evaluate(
            """() => [...document.querySelectorAll('#chartLegend .lg')]
                     .map(e => e.innerText.trim())""")
        largo.close()
        itens = page.evaluate(
            """() => [...document.querySelectorAll('#chartLegend .lg')]
                     .map(e => e.innerText.trim())""")
        assert itens == ref, ("a legenda perdeu item no telefone", itens, ref)
        assert m["itens"] == len(ref) and m["itens"] > 0, (m, ref)
        assert m["visivel"] != "none", m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("w,h", CELULARES)
def test_rotulo_de_faixa_cabe_no_grafico_e_nao_invade_a_regua(base, snap, w, h):
    """Itens 4 e 5: "recuo à média (MMS50) — não ativa agora 806,67" era mais largo
    que a área de plotagem do telefone — saía cortado e por cima das pílulas de preço
    da régua direita. Escada: texto inteiro → texto curto → só o preço."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = _celular(browser, w, h)
        _abre(page, base, snap)
        m = page.evaluate("""() => {
          const cv = document.querySelector('#priceChart');
          const ctx = cv.getContext('2d');
          ctx.font = "bold 10px ui-monospace, Menlo, monospace";
          const labels = JSON.parse(cv.dataset.levelLabels || '[]');
          const plotW = cv.clientWidth - 8 - 58;   // PAD_L / PAD_R
          return {labels, plotW,
                  larguras: labels.map(t => Math.round(ctx.measureText(t).width + 14))};
        }""")
        assert m["labels"], m
        for txt, larg in zip(m["labels"], m["larguras"], strict=True):
            # DENTE: o rótulo inteiro pedia mais que a área de plotagem e vazava
            assert larg <= m["plotW"] - 10, (f"rótulo largo demais: {txt} ({larg}px)", m)
        # o preço NUNCA se perde na escada — é o dado, o nome está na legenda
        assert any("806,67" in t for t in m["labels"]), m
        assert any("recuo" in t for t in m["labels"]), ("a forma curta ainda nomeia", m)
        assert not any("não ativa agora" in t for t in m["labels"]), (
            "no telefone o rótulo longo não cabe — tem que ter virado o curto", m)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_no_desktop_o_rotulo_longo_continua_inteiro(base, snap):
    """A forma curta é pra quando não cabe. Onde cabe, o rótulo completo fica: encurtar
    de graça seria perder informação por nada."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1200})
        _abre(page, base, snap)
        labels = page.evaluate(
            "() => JSON.parse(document.querySelector('#priceChart').dataset.levelLabels || '[]')")
        assert any("recuo à média (MMS50) — não ativa agora" in t for t in labels), labels
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("w,h", CELULARES)
def test_rr_abaixo_de_um_e_estado_visual_nao_um_numero_qualquer(base, snap, w, h):
    """R:R 0,31 arrisca 3,2x o que pretende ganhar. O gráfico já dizia isso em âmbar
    dentro do canvas; o texto mostrava em cor neutra, igual a um R:R 2,50."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = _celular(browser, w, h)
        _abre(page, base, snap)
        m = page.evaluate("""() => {
          const rows = [...document.querySelectorAll('#setupCards .sc-row')];
          const rr = rows.find(e => e.innerText.includes('risco/retorno'));
          const sl = rows.find(e => e.innerText.includes('stop (SL)'));
          return {classe: rr.className, cor: getComputedStyle(rr).color,
                  txt: rr.innerText, title: rr.getAttribute('title'),
                  corNeutra: getComputedStyle(sl).color,
                  // quantas vezes o R:R aparece na TELA inteira
                  vezes: (document.querySelector('#resultPanel').innerText
                    .match(/0,31/g) || []).length};
        }""")
        assert "rr-ruim" in m["classe"], m
        # A COR saiu (DA-078 regra 3: âmbar fora da paleta — aviso é palavra). O que
        # não pode é a informação sumir junto: entra a PALAVRA, na linha da base.
        assert "risco > retorno" in m["txt"], ("sem a cor, o aviso tem de estar escrito", m)
        assert "3.2x" in m["txt"] or "3,2x" in m["txt"], m
        # e diz POR QUE é ruim, com a conta feita
        assert "risco MAIOR que o retorno" in (m["title"] or ""), m
        assert "3.2x" in (m["title"] or "") or "3,2x" in (m["title"] or ""), m
        # DENTE da 021: o R:R saía DUAS vezes (tira do cabeçalho + bloco do setup).
        # Ele pertence ao 1-2-3 — entrada é o gatilho, risco é o stop, retorno é o
        # alvo — e aparece uma vez só, no card dele.
        assert m["vezes"] == 1, ("o mesmo número em duas caixas era o defeito", m)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_rr_maior_que_um_nao_vira_alarme(base, snap):
    """O estado é do R:R RUIM. Um setup com retorno maior que o risco continua neutro
    — senão a cor deixa de querer dizer alguma coisa."""
    bom = json.loads(json.dumps(snap))
    bom["result"]["actionable"]["risk_reward"]["rr"] = 2.5
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        _abre(page, base, bom)
        m = page.evaluate("""() => {
          const rr = [...document.querySelectorAll('#setupCards .sc-row')]
            .find(e => e.innerText.includes('risco/retorno'));
          return {classe: rr.className, cor: getComputedStyle(rr).color,
                  ruins: document.querySelectorAll('.sc-row.rr-ruim').length};
        }""")
        assert "rr-ruim" not in m["classe"], m
        assert m["cor"] != "rgb(245, 180, 69)", m
        assert m["ruins"] == 0, m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("largura", [1500, 1280])
def test_o_desktop_nao_pagou_a_conta_do_telefone(base, snap, largura):
    """Nada do arranjo de telefone pode vazar pro desktop: a legenda continua ACIMA do
    gráfico, a régua entre as unidades de preço fica e elas seguem lado a lado — hoje
    TRÊS (cotação, distância, análise) desde o commit 185ba75/task 037, que deu à
    distância uma unidade própria em vez de deixar o usuário subtrair de cabeça;
    o empilhamento é coisa de telefone, no desktop as três continuam numa fileira só.
    (O "encostada à direita" saiu com a regra 11 da DA-078 — a tira agora flui junto
    da meta e a sobra de espaço fica no fim da fileira, não num buraco no meio.)"""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": largura, "height": 1200})
        _abre(page, base, snap)
        m = page.evaluate("""() => {
          const r = (s) => document.querySelector(s).getBoundingClientRect();
          return {
            legendaAcima: r('#chartLegend').top < r('.chart-wrap').top,
            // regra 11 da DA-078: a tira DEIXOU de ser pinçada na borda direita — ela
            // flui depois da meta, na mesma fileira, e o que sobra fica no FIM. O que
            // continua valendo é ela começar onde a informação começa (à esquerda do
            // painel), nunca com um buraco atrás.
            naEsquerda: Math.round(r('#headPrice').left) - Math.round(r('.result-info').left) < 8,
            regua: getComputedStyle(document.querySelector('.hp-ref')).borderLeftWidth,
            // `align-items: baseline` alinha a LINHA DE BASE, não o topo: com o
            // preço em 16px e a referência em 14px os topos diferem mesmo lado a
            // lado. Quem responde "estão na mesma linha?" é uma estar à ESQUERDA da
            // outra, não o topo coincidir.
            unidadesLadoALado: (() => {
              const u = [...document.querySelectorAll('#headPrice .hp-unit')];
              return u.length === 3 && u.every((el, i) => i === 0 ||
                u[i - 1].getBoundingClientRect().right <= el.getBoundingClientRect().left + 1);
            })(),
          };
        }""")
        assert m["legendaAcima"], m
        assert m["naEsquerda"], ("a tira flui com a meta, sem buraco atrás (DA-078 r.11)", m)
        assert m["regua"] != "0px", ("a régua entre cotação e análise fica no desktop", m)
        assert m["unidadesLadoALado"], ("no desktop as duas ficam lado a lado", m)
        browser.close()

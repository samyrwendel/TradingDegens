"""O PAINEL da carteira do Erick na tela — e a invisibilidade dele (DA-148).

O teste do servidor (`test_webui_erick_carteira`) prova que a ROTA recusa. Este
prova a outra metade, que é a que o visitante vê: **ele não descobre que a feature
existe**. Botão escondido, painel escondido, nada na tela para clicar.

E prova o gancho que torna o painel útil em vez de um espelho: clicar em "analisar"
num ativo dele abre a análise do NOSSO sistema sobre aquele papel.
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


_CARTEIRA = {
    "carteira": {
        "atualizado": "27/08/2026",
        "ativos": [],
        "feed": [{"id": 13, "tipo": "venda", "titulo": "Saída de IREN antes do resultado",
                  "data": "27/08/2026", "resumo": "Zerei a posição em IREN."}],
        "relatorios": [{"id": 6, "titulo": "Saída de IREN", "data": "27/08/2026",
                        "conteudo": ":::resumo\n- saiu antes do earnings\n:::\n\nO motivo é timing, não tese."}],
    },
    "historico": [100.0, 104.0, 112.0, 119.0],
    "lido_em": 1_788_000_000.0,
    "idade_horas": 3.0,
    "degradado": False,
    "fonte": "https://exemplo",
    "composicao": [
        {"ticker": "MSFT", "nome": "Microsoft", "classe": "Acao", "qtd": 22.108,
         "precoMedio": 381.93, "entrada": "jul/2026", "participacao": 0.20,
         "simbolo_produto": "MSFT", "preco_agora": 492.0, "variacao_pm": 0.288},
        {"ticker": "BTC", "nome": "Bitcoin", "classe": "Cripto", "qtd": 0.0084,
         "precoMedio": 62485.88, "entrada": "jul/2026", "participacao": 0.09,
         "simbolo_produto": "BTC-USD", "preco_agora": 58000.0, "variacao_pm": -0.072},
        {"ticker": "CASH", "nome": "Caixa", "classe": "Caixa", "qtd": 84829.22,
         "precoMedio": 1, "entrada": "-", "participacao": 0.71,
         "tese": "Caixa elevado por escolha. Reserva de oportunidade.",
         "simbolo_produto": None, "preco_agora": None, "variacao_pm": None},
    ],
}


def _abre(page, base_url, *, dono: bool, viewport=None):
    def handler(route):
        u = route.request.url
        if "/api/erick/carteira" in u:
            if not dono:
                route.fulfill(status=403, content_type="application/json",
                              body=json.dumps({"error": "acesso restrito ao dono",
                                               "error_code": "owner_only"}))
            else:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps(_CARTEIRA))
        elif "/api/config" in u:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"owner": dono, "owner_login_enabled": True}))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)
    page.goto(base_url, wait_until="domcontentloaded")
    # No telefone a lista de observação nasce RECOLHIDA e o botão mora dentro dela:
    # abrir o painel é o gesto do usuário, não uma concessão do teste.
    if not page.evaluate("() => document.getElementById('historyPanel').open"):
        page.click("#historyPanel > summary")
    page.wait_for_timeout(900)


_ESTADO = """() => ({
  botao: !!document.getElementById('erickOpenBtn')
         && !document.getElementById('erickOpenBtn').classList.contains('hidden'),
  painel: !!document.getElementById('erickPanel')
          && !document.getElementById('erickPanel').classList.contains('hidden'),
  texto: (document.getElementById('erickCorpo') || {}).innerText || '',
})"""


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [DESKTOP, TELEFONE], ids=["desktop", "telefone"])
def test_DENTE_o_visitante_nao_descobre_que_a_feature_existe(base, viewport):
    """Recusar no clique não basta: o botão não pode estar lá. Uma feature que se
    anuncia e depois nega é um convite a insistir — e o conteúdo é de assinatura
    paga de outra pessoa."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre(page, base, dono=False, viewport=viewport)
        m = page.evaluate(_ESTADO)
        assert m["botao"] is False, m
        assert m["painel"] is False, m
        # e nem o nome do autor aparece em lugar nenhum da tela do visitante
        corpo = page.inner_text("body").lower()
        assert "erick sekiama" not in corpo, corpo[:300]
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_DONO_ve_o_botao_e_o_painel_abre_com_o_carimbo_do_dado(base):
    """DE QUANDO É O DADO, sempre (DA-114): fonte de terceiro atualizada à mão."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, dono=True)
        assert page.evaluate(_ESTADO)["botao"] is True
        page.click("#erickOpenBtn")
        page.wait_for_selector("#erickPanel:not(.hidden)", timeout=10000)
        page.wait_for_timeout(400)
        carimbo = page.inner_text("#erickCarimbo")
        assert "27/08/2026" in carimbo, carimbo
        assert "há 3h" in carimbo or "agora há pouco" in carimbo, carimbo
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [DESKTOP, TELEFONE], ids=["desktop", "telefone"])
def test_o_CAIXA_e_manchete_e_a_variacao_usa_a_paleta_de_direcao(base, viewport):
    """71% em caixa é POSTURA declarada, não sobra — numa grade de composição sairia
    como só mais uma linha. E a variação desde o preço médio usa verde/vermelho
    porque é exatamente o que a DA-140 reserva pra eles: direção."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre(page, base, dono=True, viewport=viewport)
        page.click("#erickOpenBtn")
        page.wait_for_selector("#erickPanel:not(.hidden)", timeout=10000)
        page.wait_for_timeout(400)
        m = page.evaluate("""() => {
          const c = document.querySelector('.ek-caixa');
          const vars = [...document.querySelectorAll('.ek-var')].map((e) => ({
            t: e.textContent.trim(), cor: getComputedStyle(e).color }));
          return {caixa: c ? c.innerText.replace(/\\s+/g, ' ') : null, vars,
                  linhas: document.querySelectorAll('.ek-linha:not(.ek-cab)').length};
        }""")
        assert m["caixa"] and "71" in m["caixa"], m["caixa"]
        assert m["linhas"] == 2, ("o caixa sai da grade e vira manchete", m)
        cores = {v["t"]: v["cor"] for v in m["vars"]}
        assert cores.get("28,8%") == "rgb(46, 204, 113)", cores      # --green
        assert cores.get("-7,2%") == "rgb(255, 92, 108)", cores      # --red
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_O_GANCHO_analisar_abre_o_ativo_dele_pelo_NOSSO_sistema(base):
    """É o que torna o painel útil em vez de um espelho: a posição REAL dele e o
    veredito do nosso sistema, lado a lado. E o CAIXA não é clicável — não há o que
    analisar num saldo."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, dono=True)
        page.click("#erickOpenBtn")
        page.wait_for_selector("#erickPanel:not(.hidden)", timeout=10000)
        page.wait_for_timeout(400)
        page.evaluate("""() => {
          window.__pedido = null;
          const o = window.fetch;
          window.fetch = (u, x) => {
            if (String(u).includes('/api/analyze') && x && x.body) {
              window.__pedido = JSON.parse(x.body);
              return new Promise(() => {});
            }
            return o(u, x);
          };
        }""")
        alvos = page.evaluate("""() => [...document.querySelectorAll('[data-erick-go]')]
                                     .map((b) => b.dataset.erickGo)""")
        assert alvos == ["MSFT", "BTC-USD"], ("o caixa não pode ser clicável", alvos)
        page.click('[data-erick-go="BTC-USD"]')
        page.wait_for_function("() => window.__pedido !== null", timeout=10000)
        pedido = page.evaluate("() => window.__pedido")
        assert pedido["ticker"] == "BTC-USD" and pedido["method"] == "setup123", pedido
        # e o painel sai da frente, porque a análise passa a ser o assunto
        assert page.evaluate(_ESTADO)["painel"] is False
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_racional_dele_esta_la_e_RECOLHIDO(base):
    """Os relatórios são o que menos se acha em outro lugar — e são longos. Ficam
    presentes sem tomar a tela (mesma disciplina da legenda que recolhe, DA-135)."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, dono=True)
        page.click("#erickOpenBtn")
        page.wait_for_selector("#erickPanel:not(.hidden)", timeout=10000)
        page.wait_for_timeout(400)
        m = page.evaluate("""() => {
          const d = document.querySelector('.ek-rel');
          return {existe: !!d, aberto: d ? d.open : null,
                  titulo: d ? d.querySelector('summary').innerText : '',
                  movs: document.querySelectorAll('.ek-mov').length,
                  curva: !!document.querySelector('.ek-curva svg path')};
        }""")
        assert m["existe"] and m["aberto"] is False, m
        assert "Saída de IREN" in m["titulo"], m
        assert m["movs"] == 1, m
        assert m["curva"] is True, ("a curva de patrimônio some sem série, não fica vazia", m)
        browser.close()

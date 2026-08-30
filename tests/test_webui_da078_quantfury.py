"""DA-078 — a linguagem visual da Quantfury, verificada regra a regra.

"Modela bem a Quantfury, pq nosso design não está seguindo as regras que mandei."

A DA-070 (card quadrado, zero degradê) e a DA-076 (sem emoji) foram cumpridas ao pé
da letra e o resultado ainda não parecia com a referência — porque regra vaga não
segura implementação. A DA-078 destilou a referência em regras VERIFICÁVEIS, e é isso
que este arquivo faz: cada uma vira portão, por grep no CSS ou por medida no navegador.

O invariante que atravessa tudo: **nenhuma informação some para caber na estética**.
Onde a cor era o único portador de estado, o estado virou palavra — nunca vazio. Os
testes de baixo medem exatamente isso.
"""

import pathlib
import re
import threading

import pytest

from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.store import HistoryStore

_CSS = (pathlib.Path(__file__).resolve().parents[1]
        / "tradingagents" / "webui" / "static" / "style.css")


def _regras(texto):
    """Só as DECLARAÇÕES — comentário não é CSS aplicado."""
    return re.sub(r"/\*.*?\*/", "", texto, flags=re.S)


# ───────────────────────────────── regra 1 — geometria: retângulo ─────────────
def test_regra1_zero_pilula():
    """22 elementos em `border-radius: 999px` num design declarado quadrado era o
    contorno mais visível do que a DA-070 já proibia em espírito."""
    assert "999px" not in _regras(_CSS.read_text())


def test_regra1_nenhum_raio_acima_de_2px():
    css = _regras(_CSS.read_text())
    raios = set(re.findall(r"border-radius:\s*([0-9.]+)px", css))
    grandes = sorted(r for r in raios if float(r) > 2)
    assert grandes == [], (f"raios acima de 2px: {grandes}px", raios)
    # e o token único do projeto acompanha
    assert re.search(r"--radius:\s*2px", css), "o --radius do projeto tem de ser 2px"


# ───────────────────────── regra 3 — cor é semântica, nunca decorativa ────────
@pytest.mark.parametrize("token", ["--amber", "--yellow", "--yellow-dim",
                                   "--yellow-border", "--orange"])
def test_regra3_paleta_sem_ambar_amarelo_laranja(token):
    """Verde = ganho/alta, vermelho = perda/baixa, o resto é branco e cinza. Aviso se
    resolve com palavra e hierarquia, não com cor nova."""
    css = _regras(_CSS.read_text())
    assert f"var({token})" not in css, f"{token} ainda é usado como cor de interface"
    assert not re.search(rf"^\s*{re.escape(token)}\s*:", css, re.M), f"{token} ainda é declarado"


# ─────────────────── regras 9 e 10 — escolha é texto, ação é botão ────────────
_SELETORES = [
    (".tf-btn", "timeframe do gráfico"),
    ("button.lb-tf", "timeframe da barra"),
    ("button.lb-method", "método da barra"),
    (".scan-view", "modo de apresentação do scan"),
    (".scan-filter", "filtro de estado do scan"),
]


@pytest.mark.parametrize("seletor,nome", _SELETORES)
def test_regra9_seletor_de_segmento_e_texto(seletor, nome):
    """Na referência a barra de timeframe é `5m 15m 30m 1h 4h D S` em TEXTO puro —
    sem caixa, sem borda, sem fundo. Escolher não é agir."""
    css = _regras(_CSS.read_text())
    m = re.search(rf"(?m)^{re.escape(seletor)}\s*\{{(.*?)\}}", css, re.S)
    assert m, f"{seletor} sumiu do CSS"
    bloco = m.group(1)
    assert "border: none" in bloco, (nome, "seletor de segmento não tem borda", bloco)
    assert "background: none" in bloco, (nome, "nem fundo", bloco)


@pytest.mark.parametrize("seletor", [".tf-btn.is-active", "button.lb-tf.is-active",
                                     "button.lb-method.is-active",
                                     ".scan-view.is-active", ".scan-filter.is-active"])
def test_regra9_o_ativo_se_distingue_por_cor_e_peso(seletor):
    """O que estava dentro de uma caixa com anel de destaque passa a se distinguir
    por COR (verde) e PESO — que é como a referência marca o frame ativo."""
    css = _regras(_CSS.read_text())
    m = re.search(rf"(?m)^{re.escape(seletor)}\s*\{{(.*?)\}}", css, re.S)
    assert m, f"{seletor} sumiu"
    bloco = m.group(1)
    assert "var(--green)" in bloco, (seletor, bloco)
    assert "fw-bold" in bloco, (seletor, bloco)


def test_regra10_o_botao_de_ACAO_mantem_a_caixa():
    """O outro lado da regra: o que EXECUTA algo continua com superfície sólida —
    senão "escolha é texto" viraria "nada tem forma"."""
    css = _regras(_CSS.read_text())
    m = re.search(r"(?m)^#runBtn\s*,?[^{]*\{(.*?)\}", css, re.S)
    assert m, "#runBtn sumiu do CSS"
    assert "background" in m.group(1), ("o botão Analisar é AÇÃO: tem caixa", m.group(1))


# ───────────── o invariante: nada de informação sumindo pela estética ─────────
pytestmark_integration = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None


@pytest.fixture
def base(tmp_path):
    from tradingagents.webui.server import make_server

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


@pytest.mark.integration
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_compra_e_venda_continuam_distinguiveis(base):
    """A cor que a DA-078 MANTÉM é justamente esta — verde/vermelho com sentido
    estrito. O teste existe pra provar que a limpeza não levou junto o que importa."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.goto(base, wait_until="networkidle")
        m = page.evaluate("""() => {
          const host = document.createElement('div');
          host.innerHTML = '<span class="scan-chip compra">COMPRA</span>' +
                           '<span class="scan-chip venda">VENDA</span>' +
                           '<span class="scan-chip ok">ok</span>' +
                           '<span class="scan-chip bad">falhou</span>';
          document.body.appendChild(host);
          const cor = (s) => getComputedStyle(host.querySelector(s)).color;
          return {compra: cor('.compra'), venda: cor('.venda'),
                  ok: cor('.ok'), bad: cor('.bad'),
                  texto: host.innerText};
        }""")
        assert m["compra"] != m["venda"], m          # alta × baixa
        assert m["ok"] != m["bad"], m                # passou × falhou
        assert m["compra"] == m["ok"], ("verde é ganho/alta nos dois", m)
        assert m["venda"] == m["bad"], ("vermelho é perda/baixa nos dois", m)
        # e a PALAVRA está lá — a cor reforça, não substitui
        for palavra in ("COMPRA", "VENDA", "ok", "falhou"):
            assert palavra.lower() in m["texto"].lower(), (palavra, m["texto"])
        browser.close()


@pytest.mark.integration
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_atencao_sobreviveu_a_saida_do_ambar(base):
    """O caso mais delicado da limpeza: o âmbar era o único portador de "atenção" em
    três lugares (R:R ruim, alvo recusado, série vencida). Sem ele, o aviso tem de
    estar ESCRITO — é o invariante da DA-078, e é isto que se mede."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.goto(base, wait_until="networkidle")
        m = page.evaluate("""() => ({
          ruim: rrMarca(0.31), bom: rrMarca(2.5),
          aviso: rrAviso(0.31),
        })""")
        assert "risco > retorno" in m["ruim"], m
        assert "3,2x" in m["ruim"], ("a conta vem junto, e em pt-BR como o resto", m)
        assert m["bom"] == "", ("R:R bom não vira alarme", m)
        assert "risco MAIOR que o retorno" in m["aviso"], m
        browser.close()

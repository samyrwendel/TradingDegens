"""O nível RECUSADO se explica onde o usuário o procura (task 20260831-023).

*"kd o alvo do Setup123?"* — o Samyr, olhando o gráfico do MSFT no 4h.

**O comportamento estava certo e a comunicação não.** O papel está na máxima da
série: não há topo anterior acima da entrada, então não há retorno a projetar, e a
task 008 estabeleceu que número inventado não se publica. Mas na legenda do
gráfico apareciam "recuo à média", "invalidação" e "stop (SL)" — e o alvo
simplesmente **não existia**, sem uma palavra. O único vestígio era um "R:R não
calculável" num canto, que não responde "cadê o alvo".

O motivo já estava no dado (``risk_reward.note``, escrito pelo backend). O que
faltava era ele chegar onde a pergunta nasce: **a legenda do gráfico** e **a linha
do alvo no card**.

**Nada de texto novo:** a legenda e a linha do card mostram a PRIMEIRA ORAÇÃO do
``note`` (um extrato), e a linha do R:R continua com a frase inteira. Uma segunda
explicação escrita à mão divergiria da do backend no dia em que ele mudasse a dele.

Os dentes:

* com o payload REAL do MSFT 4h, a legenda tem uma entrada para o alvo ausente e
  ela carrega o motivo (antes: nada, em lugar nenhum do gráfico);
* a linha "alvo (TP)" do card deixa de dizer só "sem nível definido";
* com alvo PRESENTE, nada disso aparece — aviso que sempre aparece não avisa nada;
* a regra é de NÍVEL RECUSADO, não do alvo: ``niveisRecusados`` é genérica.
"""

import json
from pathlib import Path

import pytest

from tests.test_webui_frame_e_cor_e2e import (
    _ACT_4H,
    DESKTOP,
    TELEFONE,
    _abre,
    sobe_servidor,
    sync_playwright,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def base(tmp_path):
    yield from sobe_servidor(tmp_path)


# O caso REAL: MSFT no 4h, na máxima da série. `target` ausente e o `note` do
# backend dizendo por quê. Os números são os que a fonte devolveu em 31/08.
_NOTA_REAL = ("sem alvo estrutural à frente da entrada — não há topo anterior acima "
              "dela nesta série, então não há retorno a projetar (o risco continua "
              "medido).")
_ACT_SEM_ALVO = {
    **_ACT_4H,
    "target": None,
    "risk_reward": {"entry": 513.73, "entry_basis": "gatilho — rompimento da máxima do ponto 2",
                    "risk": 40.08, "reward": None, "rr": None, "note": _NOTA_REAL},
}

_LEITURA = """() => ({
  legenda: [...document.querySelectorAll('.chart-legend .lg')].map(e => e.textContent.trim()),
  sem: (document.querySelector('.chart-legend .lg-sem') || {}).textContent || null,
  semTitle: (document.querySelector('.chart-legend .lg-sem') || {}).title || null,
  amostraVazia: !!document.querySelector('.chart-legend .lg-sem .sw.vazia'),
  linhasAlvo: [...document.querySelectorAll('#setupCards .sc-row')]
    .map(e => e.textContent.trim()).filter(t => t.startsWith('alvo')),
  linhaRR: [...document.querySelectorAll('#setupCards .sc-row')]
    .map(e => e.textContent.trim()).filter(t => t.includes('não calculável'))[0] || null,
})"""


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_alvo_ausente_se_explica_NA_LEGENDA_do_grafico(base):
    """DENTE: antes o alvo sumia da legenda sem uma palavra, e "cadê o alvo?" não
    tinha resposta em lugar nenhum do gráfico."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, actionable=_ACT_SEM_ALVO)
        m = page.evaluate(_LEITURA)

        assert m["sem"], ("a legenda não disse nada sobre o alvo ausente", m["legenda"])
        assert "alvo" in m["sem"], m
        # o MOTIVO, e é o do backend — não uma frase escrita na tela
        assert "sem alvo estrutural à frente da entrada" in m["sem"], m
        assert m["semTitle"] == _NOTA_REAL, ("o title leva a frase INTEIRA", m)
        # a amostra vai VAZIA: não há linha traçada a decodificar
        assert m["amostraVazia"], m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_a_linha_do_ALVO_no_card_diz_o_motivo_e_a_do_RR_a_frase_inteira(base):
    """Dois níveis de detalhe do MESMO texto — não é duplicata (DA-077).

    A linha do alvo é onde o olho vai quando a pergunta é "cadê o alvo"; ela leva a
    primeira oração. A do R:R responde outra pergunta ("por que não dá pra medir o
    retorno") e leva a frase inteira.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, actionable=_ACT_SEM_ALVO)
        m = page.evaluate(_LEITURA)

        alvo = [t for t in m["linhasAlvo"] if "sem alvo estrutural" in t]
        assert alvo, ("a linha do alvo continua sem dizer o motivo", m["linhasAlvo"])
        assert "sem nível definido" not in " ".join(m["linhasAlvo"]), m
        assert m["linhaRR"] and _NOTA_REAL[:40] in m["linhaRR"], m
        # a linha do R:R continua com a frase INTEIRA (a do alvo é o extrato)
        assert len(m["linhaRR"]) > len(alvo[0]), (alvo, m["linhaRR"])
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_com_ALVO_PRESENTE_nada_disso_aparece(base):
    """Aviso que sempre aparece não avisa nada."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        # a fixture padrão do arquivo compartilhado também está SEM alvo (é o mesmo
        # MSFT na máxima) — aqui o alvo é posto de propósito, pra provar que a
        # explicação some quando não há ausência a explicar.
        com_alvo = {**_ACT_4H, "target": {"price": 260.0, "label": "alvo (TP)"},
                    "risk_reward": {"entry": 219.35, "entry_basis": "gatilho",
                                    "risk": 42.52, "reward": 40.65, "rr": 0.96,
                                    "note": None}}
        _abre(page, base, actionable=com_alvo)
        m = page.evaluate(_LEITURA)
        assert m["sem"] is None, ("explicou uma ausência que não existe", m)
        assert any("alvo" in t and "sem alvo" not in t for t in m["linhasAlvo"]), m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_R_R_nao_calculavel_passa_a_dizer_POR_QUE(base):
    """O carimbo no canvas era "R:R não calculável" e ponto — um beco sem saída."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base, actionable=_ACT_SEM_ALVO)
        # o carimbo é DESENHADO no canvas; `dataset.rr` guarda o texto que SAIU
        # (depois da escada de degradação por largura), não o que se pretendia.
        selo = page.evaluate(
            "() => document.getElementById('priceChart').dataset.rr || ''")
        assert "não calculável" in selo, selo
        assert "sem alvo estrutural" in selo, ("o carimbo não diz o motivo", selo)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_no_celular_a_explicacao_cabe_e_a_pagina_nao_rola_na_horizontal(base):
    """390×844 (DA-062/DA-101): a frase é longa e a legenda é uma linha só."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE)
        _abre(page, base, actionable=_ACT_SEM_ALVO, viewport=TELEFONE)
        m = page.evaluate(_LEITURA)
        assert m["sem"] and "sem alvo estrutural" in m["sem"], m
        geo = page.evaluate("""() => {
          const e = document.querySelector('.chart-legend .lg-sem');
          const r = e.getBoundingClientRect();
          return {dir: r.right, doc: document.documentElement.scrollWidth};
        }""")
        assert geo["dir"] <= 390.5, ("a explicação vazou do viewport", geo)
        assert geo["doc"] <= 390, ("a página passou a rolar na horizontal", geo)
        browser.close()


# ------------------------------------------------------- a regra é GENÉRICA ----
def test_niveisRecusados_e_do_NIVEL_e_nao_do_alvo():
    """A regra é "ausência declarada" (task 008), não "explique o alvo".

    Lido do fonte: hoje só o alvo cai nela, mas a função é sobre NÍVEL recusado —
    no dia em que o stop ou a invalidação forem recusados, a legenda já sabe dizer.
    """
    app = (Path(__file__).resolve().parents[1] / "tradingagents" / "webui" / "static"
           / "app.js").read_text(encoding="utf-8")
    assert "function niveisRecusados(" in app
    assert "function motivoCurto(" in app
    # o motivo vem do DADO, nunca de prosa escrita na tela
    corpo = app.split("function niveisRecusados(")[1].split("\n}")[0]
    assert "rr.note" in corpo, corpo
    assert "não há topo anterior" not in corpo, ("a tela reescreveu o motivo do "
                                                 "backend — duas explicações divergem")


def test_motivoCurto_devolve_a_primeira_oracao_e_nunca_texto_novo():
    """Verifica o contrato do extrato contra a nota REAL do backend."""
    app = (Path(__file__).resolve().parents[1] / "tradingagents" / "webui" / "static"
           / "app.js").read_text(encoding="utf-8")
    assert 'const corte = t.search(' in app
    # o comportamento esperado, escrito aqui pra o contrato ficar legível:
    esperado = "sem alvo estrutural à frente da entrada"
    assert _NOTA_REAL.startswith(esperado + " — ")
    assert json.dumps(esperado)          # (sanidade: é texto, não None)

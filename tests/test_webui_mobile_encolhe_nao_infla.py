"""NO CELULAR SE ENCOLHE PRA CABER — nunca se infla pra tocar (task 20260830-035).

Palavra do Samyr (30/08): *"pra vc melhorar a responsividade no celular vc pode reduzir
pra caber, não precisa aumentar o tamanho dos itens, o gráfico pra mim está bom como
está, só alinha e ajusta os textos, legendas etc"*.

Por que a regra é essa, e não o "alvo de toque de 44px" do manual: ele lê MUITO DADO
numa tela pequena — níveis, R:R, três cards de leitura empilhados. Item grande não
melhora o toque de graça: empurra informação pra fora da dobra. O que ele perde ao
inflar é DADO; o que ele perde ao encolher é folga.

As três regras, e as três viram portão aqui:

1. **encolher é permitido, inflar não** — dentro de um ``@media (max-width: …)`` nenhum
   seletor pode crescer em relação ao que ele é fora dali;
2. **nada de alvo de toque inflado** — ``min-height``/``min-width`` de 40px+ dentro de
   bloco de celular é exatamente a regra do manual que ele dispensou;
3. **encolher ≠ sumir** — ``display: none`` no celular só para elemento que não carrega
   dado, e cada exceção declarada aqui com o motivo.

(O CANVAS do gráfico está APROVADO como está — proporção, altura e densidade não se
mexem. O trabalho de responsivo é em texto, legenda, rótulo e alinhamento ao redor.)
"""

import pathlib
import re

import pytest

pytestmark = pytest.mark.unit

_CSS = (pathlib.Path(__file__).resolve().parents[1]
        / "tradingagents" / "webui" / "static" / "style.css")

# Elementos que PODEM sumir no celular — e o motivo. Nenhum deles carrega dado da
# análise: são auxiliares de uma interação que o telefone nem tem.
_PODE_SUMIR = {
    ".col-resizer": "alça de redimensionar colunas — no telefone é coluna única",
    # A dica virou uma CAIXA (texto + botão de recolher, DA-128) e some inteira no
    # telefone: o controle sozinho, sem o texto que ele esconde, seria um botão que
    # não faz nada visível. O seletor é composto porque a regra base vem DEPOIS no
    # arquivo e define `display: flex` — com a mesma especificidade ela venceria.
    ".chart-wrap .chart-hint-box": "dica de roda/arrasto do mouse (e o botão que a "
                                   "recolhe) — o telefone usa toque",
}

# Propriedades que, se aparecerem no celular, têm de ser MENORES ou iguais à base.
_MEDIDAS = ("font-size", "height", "min-height", "min-width", "line-height")


def _sem_comentario(t):
    return re.sub(r"/\*.*?\*/", "", t, flags=re.S)


def _tokens(css):
    """``--fs-12: 12px`` → ``{"--fs-12": 12.0}`` — pra comparar token com literal."""
    return {n: float(v) for n, v in re.findall(r"(--[\w-]+):\s*([0-9.]+)px", css)}


def _px(valor, tok):
    """Valor em px, ou ``None`` quando não é comparável (%, vh, calc, keyword)."""
    v = valor.strip().rstrip(";").strip()
    m = re.fullmatch(r"var\((--[\w-]+)\)", v)
    if m:
        return tok.get(m.group(1))
    m = re.fullmatch(r"([0-9.]+)px", v)
    return float(m.group(1)) if m else None


def _blocos_media(css):
    """(prelúdio, corpo) de cada ``@media``, com aninhamento respeitado."""
    out, i = [], 0
    while True:
        m = re.compile(r"@media([^{]*)\{").search(css, i)
        if not m:
            return out
        prof, j = 1, m.end()
        while prof and j < len(css):
            prof += 1 if css[j] == "{" else -1 if css[j] == "}" else 0
            j += 1
        out.append((m.group(1).strip(), css[m.end():j - 1]))
        i = j


def _regras(corpo):
    """(seletor, declarações) de um corpo CSS plano."""
    return [(s.strip(), d) for s, d in re.findall(r"([^{}]+)\{([^{}]*)\}", corpo)]


def _celular(css):
    return [(p, c) for p, c in _blocos_media(css) if "max-width" in p]


def _base(css):
    """Maior valor de cada medida por seletor FORA de media query — a régua da base.

    O maior, e não o último, porque um seletor pode ser declarado em vários lugares:
    comparar contra o menor acusaria como "inflado" um celular que só repete a base.
    """
    fora = css
    for pre, corpo in _blocos_media(css):
        fora = fora.replace(f"@media{pre}{{{corpo}}}", "", 1)
    tok = _tokens(css)
    mapa: dict[tuple[str, str], float] = {}
    for sel, decl in _regras(fora):
        for parte in sel.split(","):
            p = parte.strip()
            for prop in _MEDIDAS:
                for v in re.findall(rf"(?<![-\w]){prop}:\s*([^;]+)", decl):
                    px = _px(v, tok)
                    if px is not None:
                        mapa[(p, prop)] = max(mapa.get((p, prop), 0.0), px)
    return mapa


# ─────────────────── regra 1 — encolher é permitido, inflar não ───────────────
def test_nenhum_seletor_CRESCE_no_celular():
    """DENTE: a saída natural pro apertado é aumentar "pra caber o dedo". Aqui é o
    contrário — quem manda é o dado que precisa caber na dobra."""
    css = _sem_comentario(_CSS.read_text())
    tok, base = _tokens(css), _base(css)
    inflados = []
    for pre, corpo in _celular(css):
        for sel, decl in _regras(corpo):
            for parte in sel.split(","):
                p = parte.strip()
                for prop in _MEDIDAS:
                    for v in re.findall(rf"(?<![-\w]){prop}:\s*([^;]+)", decl):
                        px, ref = _px(v, tok), base.get((p, prop))
                        if px is not None and ref is not None and px > ref:
                            inflados.append(f"{pre} · {p} · {prop}: {px}px > {ref}px")
    assert inflados == [], ("no celular se encolhe, não se infla:", inflados)


# ─────────────────── regra 2 — nada de alvo de toque inflado ──────────────────
# "Alvo de toque" é coisa de elemento CLICÁVEL. Um `min-height` de 240px no canvas é
# altura de gráfico, não polegar — medir os dois com a mesma régua só produziria ruído.
_CLICAVEL = re.compile(r"(?:^|[\s.#>])(?:button|a|input|select|label)\b|btn|-tab\b|"
                       r"\[role=[\"']?button")


def test_nenhum_alvo_de_toque_de_40px_no_celular():
    """"não precisa aumentar o tamanho dos itens" — o mínimo de 44px do manual é
    exatamente o que ele dispensou, porque empurra dado pra fora da tela."""
    css = _sem_comentario(_CSS.read_text())
    tok = _tokens(css)
    grandes = []
    for pre, corpo in _celular(css):
        for sel, decl in _regras(corpo):
            for parte in sel.split(","):
                p = parte.strip()
                if not _CLICAVEL.search(p):
                    continue
                for prop in ("min-height", "min-width", "height"):
                    for v in re.findall(rf"(?<![-\w]){prop}:\s*([^;]+)", decl):
                        px = _px(v, tok)
                        if px is not None and px >= 40:
                            grandes.append(f"{pre} · {p} · {prop}: {px}px")
    assert grandes == [], ("alvo de toque inflado no celular:", grandes)


# ───────────────────────── regra 3 — encolher ≠ sumir ─────────────────────────
def test_no_celular_nada_com_DADO_some():
    """Encolher é permitido; esconder informação atrás da estética, não. Cada exceção
    é um elemento auxiliar, declarado com o motivo em ``_PODE_SUMIR``."""
    css = _sem_comentario(_CSS.read_text())
    sumindo = []
    for pre, corpo in _celular(css):
        for sel, decl in _regras(corpo):
            if not re.search(r"display:\s*none", decl):
                continue
            for parte in sel.split(","):
                p = parte.strip()
                if p not in _PODE_SUMIR:
                    sumindo.append(f"{pre} · {p}")
    assert sumindo == [], (
        "some com dado no celular (se for auxiliar, declare em _PODE_SUMIR "
        "com o motivo):", sumindo)


def test_a_lista_de_excecoes_nao_apodrece():
    """Exceção que já não existe no CSS vira licença esquecida pra próxima."""
    css = _sem_comentario(_CSS.read_text())
    vivos = {p.strip()
             for _, corpo in _celular(css)
             for sel, decl in _regras(corpo) if re.search(r"display:\s*none", decl)
             for p in sel.split(",")}
    orfas = sorted(set(_PODE_SUMIR) - vivos)
    assert orfas == [], ("exceções que não existem mais no CSS:", orfas)


# ───────────────── o gráfico está APROVADO — o canvas não se mexe ─────────────
# A geometria APROVADA do canvas no telefone, congelada. Não é "não pode ter regra" —
# é "esta regra é a que ele aprovou". Mudar exige a palavra dele, e o teste é onde essa
# conversa acontece em vez de acontecer no print seguinte.
_CANVAS_APROVADO = {"height": "46vh", "min-height": "240px", "max-height": "340px"}


def test_a_geometria_aprovada_do_canvas_no_celular_esta_congelada():
    """"o gráfico pra mim está bom como está" — proporção, altura e densidade do canvas
    saem do escopo de responsivo. O trabalho é em texto, legenda, rótulo e alinhamento
    ao redor dele."""
    css = _sem_comentario(_CSS.read_text())
    achado = {}
    for _pre, corpo in _celular(css):
        for sel, decl in _regras(corpo):
            if "#priceChart" not in sel:
                continue
            for prop, v in re.findall(r"([\w-]+):\s*([^;]+)", decl):
                achado[prop] = v.strip()
    assert achado == _CANVAS_APROVADO, (
        "a altura do gráfico no telefone é a que o Samyr aprovou — mudar precisa da "
        "palavra dele", achado)

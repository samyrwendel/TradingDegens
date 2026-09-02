"""Camada de EXECUÇÃO: o que fazer com os níveis que o painel já desenha.

O pedido do Samyr, textual: *"quero um card explicando as entradas alvos como inserir
as ordens e onde colocar SL, TPS e onde invalida, e se ainda vale a pena entrar, ou se
é pra aguardar recuo até faixa tal"* + *"um índice de confiabilidade comparando com
ordens anteriores que bateram os TPs, SL e se devemos proteger com BE e um trailing
stop que pode ser habilitado"*.

O print que abriu a task mostra por que isso importa: **nove faixas de três famílias
na tela e nenhuma frase dizendo o que FAZER com elas**.

Este módulo NÃO inventa nível nenhum. Os níveis saem de
:func:`price_structure._pattern_levels` (invalidação, stop, alvo, R:R) e o percurso
sai da task 008 (:func:`price_structure._com_percurso`). O que ele acrescenta é a
POLÍTICA — a ordem em que se digita, a fração a realizar, a proteção e o veredito de
oportunidade — modelada em ``~/brain/trading-ops/erick-camada-de-execucao-e-saida-spec.md``
pelo degenbot a partir do corpus do Erick.

**O que é `sem evidência` continua declarado como tal**, e é a maior parte do valor
aqui: a fração exata de cada alvo, o break-even como regra do Erick e o ATR como
referência de trailing NÃO estão no corpus, e o card diz isso em vez de fabricar um
número que parece autoridade.
"""

from __future__ import annotations

import math
from typing import Any

# ── constantes emprestadas do scanner, pra o card decidir igual à lista ──────────
# Importadas de lá de propósito: dois limiares com o mesmo nome e valores diferentes
# seria a lista dizendo "em gatilho" e o card dizendo "aguardar" sobre o mesmo preço.
# ``_N_MINIMO``/``_N_OPERAVEL`` moraram aqui até a DA-154: o PnL de paper do track
# record precisava do MESMO gate, e uma segunda cópia é a porta pra divergirem.
from tradingagents.webui.scanner import (
    _GATILHO_TOL,
    _N_MINIMO,
    _N_OPERAVEL,
    _RR_RESIDUAL,
    SETUPS_DO_LEDGER,
)

# Piso de R:R-de-agora pra o card dizer ENTRAR. **`a calibrar por backtest`** — a spec
# é explícita de que este número é convenção, não corpus. 1,0 é o mínimo defensável
# (retorno pelo menos igual ao risco); mexer nele é decisão do Samyr, não do código.
_PISO_RR_ENTRAR = 1.0

_Z_95 = 1.959963984540054


def _n(v: float, casas: int = 2) -> str:
    """Número em pt-BR — a tela inteira escreve "218,56", e o card não pode ser a
    única superfície com ponto decimal."""
    txt = f"{v:,.{casas}f}"
    return txt.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


# ─────────────────────────── veredito de oportunidade ───────────────────────────
def veredito_entrada(plan: dict[str, Any] | None) -> dict[str, Any]:
    """**Ainda vale a pena entrar AGORA?** — a pergunta que ele fez olhando o gráfico.

    A resposta é aritmética e sai dos DOIS R:R da task 008: o que o setup oferecia no
    gatilho e o que RESTA de agora. Três estados, cada um com o motivo escrito:

    * ``entrar`` — o preço está NO ponto de entrada (a ≤ :data:`_GATILHO_TOL` do
      gatilho) e o R:R de agora paga o risco;
    * ``aguardar`` — o setup está vivo mas ESTICADO: o R:R de agora foi corroído pelo
      percurso, então a entrada é no RECUO, num nível que já existe na tela (a faixa
      da média) — nunca um preço inventado pra parecer resposta;
    * ``passar`` — invalidou, ou o que resta do movimento não paga o risco de entrar
      agora (``rr`` abaixo de :data:`_RR_RESIDUAL`).

    Sem padrão, sem veredito: ``estado="sem_setup"``. Um card que responde "entrar"
    sobre um plano que não existe é pior que um card em branco.
    """
    pat = (plan or {}).get("pattern") or {}
    rr = (plan or {}).get("risk_reward") or {}
    preco = (plan or {}).get("price")
    gatilho = pat.get("trigger")
    if not pat or gatilho is None or preco is None:
        return {"estado": "sem_setup", "rotulo": "sem setup para operar",
                "motivo": "nenhum padrão 1-2-3 detectado nesta leitura.",
                "nivel": None, "rr_agora": None, "rr_gatilho": None}

    compra = pat.get("direction") != "venda"
    rr_agora = rr.get("rr")
    rr_gat = (rr.get("no_gatilho") or {}).get("rr", rr_agora if pat.get("state") != "acionado" else None)
    dist = abs(float(preco) / float(gatilho) - 1.0) if gatilho else None
    base = {"rr_agora": rr_agora, "rr_gatilho": rr_gat,
            "andado_pct": rr.get("andado_pct"), "dist_pct": dist,
            "direcao": "compra" if compra else "venda"}

    # 1) MORREU: o preço passou da invalidação. Nada depois disto importa.
    inval = ((plan or {}).get("invalidation") or {}).get("price")
    if inval is not None:
        morreu = (float(preco) > float(inval)) if not compra else (float(preco) < float(inval))
        if morreu:
            return {**base, "estado": "passar", "rotulo": "passar",
                    "nivel": float(inval),
                    "motivo": (f"o preço passou da invalidação ({_n(float(inval))}) — o setup "
                               "deixou de existir, não há trade a montar aqui.")}

    # 2) RETORNO RESIDUAL: o trade já andou. É a task 008 vista do outro lado.
    if rr_agora is not None and 0 <= float(rr_agora) < _RR_RESIDUAL:
        andado = rr.get("andado_pct")
        quanto = f" (o percurso já andou {andado:.0f}%)" if andado is not None else ""
        return {**base, "estado": "passar", "rotulo": "passar",
                "motivo": (f"retorno residual — o que resta do movimento não paga o "
                           f"risco de entrar agora{quanto}."), "nivel": None}

    # 3) NO PONTO: preço no gatilho e a conta fecha.
    no_ponto = dist is not None and dist <= _GATILHO_TOL
    if no_ponto and rr_agora is not None and float(rr_agora) >= _PISO_RR_ENTRAR:
        return {**base, "estado": "entrar", "rotulo": "entrar agora",
                "nivel": float(gatilho),
                "motivo": (f"o preço está no ponto de entrada (a {_n(dist * 100)}% do "
                           f"gatilho {_n(float(gatilho))}) e o risco/retorno de agora "
                           f"({_n(float(rr_agora))}:1) paga o risco.")}

    # 4) ESTICADO ou ainda longe: a entrada é no RECUO, num nível que já está na tela.
    alvo_recuo = _nivel_de_recuo(plan)
    if alvo_recuo is not None:
        onde, preco_recuo = alvo_recuo
        return {**base, "estado": "aguardar", "rotulo": f"aguardar recuo até {onde}",
                "nivel": preco_recuo,
                "motivo": _motivo_aguardar(rr, dist, onde, preco_recuo)}
    # Sem faixa de recuo publicada, o que resta é o próprio gatilho — que é um nível
    # REAL do plano, não uma invenção.
    return {**base, "estado": "aguardar", "rotulo": "aguardar o gatilho",
            "nivel": float(gatilho),
            "motivo": _motivo_aguardar(rr, dist, "o gatilho", float(gatilho))}


def _motivo_aguardar(rr: dict, dist: float | None, onde: str, nivel: float) -> str:
    andado = rr.get("andado_pct")
    rr_agora, rr_gat = rr.get("rr"), (rr.get("no_gatilho") or {}).get("rr")
    if andado is not None and rr_gat is not None and rr_agora is not None:
        return (f"o setup está vivo, mas ESTICADO: já andou {andado:.0f}% do caminho e "
                f"o risco/retorno caiu de {_n(float(rr_gat))}:1 no gatilho para "
                f"{_n(float(rr_agora))}:1 agora. A entrada é no recuo, em {onde} "
                f"({_n(nivel)}).")
    if dist is not None:
        return (f"o preço está a {_n(dist * 100)}% do gatilho — a entrada do método é a "
                f"LIMITE no recuo, então se espera {onde} ({_n(nivel)}).")
    return f"a entrada é a limite em {onde} ({_n(nivel)})."


def _nivel_de_recuo(plan: dict[str, Any]) -> tuple[str, float] | None:
    """A faixa de recuo que a tela JÁ nomeia — a "faixa X" que ele pediu.

    Sai da ``buy_zone`` (o recuo à média com o nome da média dentro). Nada de nível
    novo: se o plano não publicou faixa, este módulo não fabrica uma.
    """
    bz = plan.get("buy_zone") or {}
    if bz.get("price") is None:
        return None
    return (bz.get("ma_label") or "a média"), float(bz["price"])


# ────────────────────────── as ordens, na ordem de digitar ──────────────────────
def ordens(plan: dict[str, Any] | None, vered: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """As ordens NA SEQUÊNCIA em que se digitam na corretora.

    A regra dura do método é **ordem a limite no recuo, nunca mercado no esticado**
    (*"tem que esperar realmente um recuo, senão quem comprar aqui vai sentar na
    graxa"*). Então a entrada sai sempre como LIMITE, e o preço dela é o do veredito:
    o gatilho quando se entra agora, a faixa de recuo quando se aguarda.

    Cada ordem carrega a BASE do seu preço — o card não pode ter número sem origem.
    """
    plan = plan or {}
    vered = vered or veredito_entrada(plan)
    if vered["estado"] == "sem_setup":
        return []
    pat = plan.get("pattern") or {}
    compra = pat.get("direction") != "venda"
    lado = "compra" if compra else "venda"
    out: list[dict[str, Any]] = []

    if vered.get("nivel") is not None and vered["estado"] != "passar":
        out.append({
            "passo": 1, "tipo": "limite", "lado": lado, "papel": "entrada",
            "price": round(float(vered["nivel"]), 2),
            "base": ("no gatilho — rompimento confirmado" if vered["estado"] == "entrar"
                     else "no recuo — o método entra a limite, nunca a mercado no esticado"),
        })

    st = plan.get("stop") or {}
    if st.get("price") is not None:
        out.append({"passo": len(out) + 1, "tipo": "stop",
                    "lado": "venda" if compra else "compra", "papel": "stop (SL)",
                    "price": float(st["price"]),
                    "base": st.get("basis") or "invalidação com folga de volatilidade"})

    for i, alvo in enumerate(_alvos(plan)):
        out.append({"passo": len(out) + 1, "tipo": "limite",
                    "lado": "venda" if compra else "compra",
                    "papel": f"T{i + 1} — {alvo['papel']}", "price": alvo["price"],
                    "base": alvo["base"], "fracao": alvo["fracao"]})
    return out


def _alvos(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Os pontos de realização — **estruturais, nunca percentuais chutados**.

    O painel já produz até dois: a região de realização (a resistência mais próxima)
    e o alvo do padrão (o swing seguinte). A ESTRUTURA é sustentada pelo corpus
    (grosso pro caixa + resíduo correndo); a FRAÇÃO exata **não é** — o corpus dá um
    único número (BTC 19/08: 95/5, N=1) e o resto é qualitativo. Por isso a fração sai
    como PALAVRA ("grosso" / "resíduo") com o rótulo `a calibrar`, e não como 70/30
    inventado com cara de regra.
    """
    tg = plan.get("target") or {}
    rz = plan.get("realize_zone") or {}
    saida = []
    if rz.get("price") is not None and rz.get("role") != "gatilho" and \
            not (tg.get("price") is not None and tg.get("same_as_realize")):
        saida.append({"price": float(rz["price"]), "papel": rz.get("role_label") or "realização",
                      "base": rz.get("label") or "região de realização"})
    if tg.get("price") is not None:
        saida.append({"price": float(tg["price"]), "papel": "alvo do padrão",
                      "base": tg.get("label") or "swing anterior à frente da entrada"})
    saida.sort(key=lambda a: a["price"],
               reverse=(plan.get("pattern") or {}).get("direction") == "venda")
    for i, a in enumerate(saida):
        a["fracao"] = "grosso" if i == 0 and len(saida) > 1 else (
            "resíduo" if len(saida) > 1 else "grosso (alvo único desta leitura)")
    return saida


def saida(plan: dict[str, Any] | None) -> dict[str, Any]:
    """A política de saída, com o que é doutrina separado do que é `a calibrar`."""
    alvos = _alvos(plan or {})
    if not alvos:
        return {"forma": "por_exaustao", "alvos": [],
                "texto": ("sem alvo estrutural à frente: a realização é por EXAUSTÃO — "
                          "pegar a maior parte do movimento e sair antes de reverter."),
                "calibrar": "a fração exata não vem do corpus."}
    if len(alvos) == 1:
        return {"forma": "alvo_unico", "alvos": alvos,
                "texto": ("realizar o grosso no alvo e manter um resíduo acompanhando "
                          "enquanto a estrutura não quebra."),
                "calibrar": "a fração do resíduo é `a calibrar por backtest`."}
    return {"forma": "grosso_e_residuo", "alvos": alvos,
            "texto": (f"realizar o GROSSO em T1 ({_n(alvos[0]['price'])}) e deixar o "
                      f"RESÍDUO correr até T2 ({_n(alvos[1]['price'])}) ou até a exaustão."),
            "calibrar": ("a fração de cada ponto é `a calibrar por backtest` — o corpus "
                         "traz um único caso com número (95/5) e o resto qualitativo.")}


def protecao() -> dict[str, Any]:
    """BE e trailing: existem, e nascem DESLIGADOS — com o porquê escrito.

    Não é omissão nem preguiça de default. O método do Erick **compra o recuo à
    média**: um BE ou um trailing colado na média ejetaria o trade exatamente no
    pullback em que ele estaria ADICIONANDO. Ligado por default, o comprador-de-recuo
    vira vendedor-de-recuo — inverte o método.

    E há uma honestidade a declarar: **`sem evidência` de que o Erick verbalize o
    break-even.** A proteção que ele descreve é REDUZIR (tirar ficha da mesa), não
    mover stop. O BE é pergunta do Samyr, entregue como ferramenta ancorada em duas
    âncoras medíveis — nunca em palpite.
    """
    return {
        "be": {
            "ligado": False,
            "rotulo": "break-even",
            "gatilhos": [
                {"chave": "estrutura", "texto": "quando o preço formar um fundo "
                                                "ascendente ACIMA da entrada (o 1-2-3 confirma um ponto novo)"},
                {"chave": "risco", "texto": "quando o trade atingir +1R (andou a favor o "
                                            "mesmo tanto que arriscava)"},
            ],
            "nota": ("desligado por default: o recuo à média é ENTRADA no método, e um BE "
                     "cedo estopa no pullback em que se adiciona."),
            "evidencia": ("`sem evidência` de BE no corpus do Erick — ele protege REDUZINDO. "
                          "O gatilho de +1R é convenção declarada, `a calibrar`."),
        },
        "trailing": {
            "ligado": False,
            "rotulo": "trailing stop",
            "referencia": ("a MÉDIA ascendente (EMA 21 no swing) e o último FUNDO ascendente "
                           "do 1-2-3 — o que estiver mais alto"),
            "disparo": "só no FECHAMENTO do frame que perde a referência; pavio não dispara.",
            "nota": ("ligar tipicamente só no RESÍDUO, depois de realizado o grosso — "
                     "nunca na posição inteira."),
            "evidencia": ("`sem evidência` de ATR como régua de trailing do Erick: o ATR é "
                          "utilitário do motor (folga do stop), não a referência dele."),
        },
    }


# ───────────────────────── índice de confiabilidade ─────────────────────────────
def wilson(acertos: int, n: int, z: float = _Z_95) -> tuple[float, float] | None:
    """Intervalo de Wilson 95% para uma proporção.

    Wilson e não o normal simples porque com N pequeno — que é exatamente o caso aqui
    — o intervalo normal produz limites fora de [0,1] e cobertura ruim. É o intervalo
    que torna visível *por que* uma taxa com 3 casos não vale nada.
    """
    if n <= 0:
        return None
    p = acertos / n
    d = 1 + z * z / n
    centro = (p + z * z / (2 * n)) / d
    meio = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / d
    return (round(max(0.0, centro - meio), 4), round(min(1.0, centro + meio), 4))


def confiabilidade(por_setup: dict[str, Any] | None) -> dict[str, Any]:
    """O índice honesto: **a taxa só aparece quando a amostra a sustenta**.

    O motor de track record já existe (:func:`scanner.scan_verdicts`) e já separa por
    setup. O que faltava — e é o cerne do pedido — é o GATE DE N: o motor calcula uma
    taxa com QUALQUER N, e *taxa de acerto com 3 casos é ruído que engana mais do que
    ajuda*.

    ==========  ==========================================================
    n < 5       **não exibe taxa** — só "amostra insuficiente (n=X)"
    5 ≤ n < 20  **preliminar** — taxa SEMPRE com o intervalo de Wilson ao lado
    n ≥ 20      **operável** — taxa como número de trabalho, ainda com o intervalo
    ==========  ==========================================================

    E lidera pela EXPECTATIVA, não pelo acerto: *70% de acerto com R:R 0,13 perde
    dinheiro* — a distribuição real de 29/08 exigia 88,5% só pra empatar. O acerto vem
    depois, e sempre ao lado do acerto-de-equilíbrio.

    Por setup, nunca somado: acerto de um grupo com R:R de outro não descreve trade
    nenhum (lição da task 008).
    """
    out: dict[str, Any] = {"n_minimo": _N_MINIMO, "n_operavel": _N_OPERAVEL, "setups": {}}
    # OS DOIS SETUPS SEMPRE. Iterar só o que o ledger devolveu fazia o índice SUMIR da
    # tela quando o track record vinha vazio ou ilegível (o caminho fail-open do
    # runner) — e um bloco ausente não diz "não há amostra", diz nada. O gate é a
    # informação: com n=0 a tela declara "amostra insuficiente (n=0)", que é a
    # verdade, em vez de calar justamente onde a pessoa decide.
    fonte = {n: (por_setup or {}).get(n) or {} for n in SETUPS_DO_LEDGER}
    for nome, bloco in {**fonte, **(por_setup or {})}.items():
        b = bloco or {}
        n = int(b.get("n_fechados") or 0)
        taxa = b.get("taxa_acerto")
        nivel = "insuficiente" if n < _N_MINIMO else (
            "preliminar" if n < _N_OPERAVEL else "operavel")
        item: dict[str, Any] = {
            "n": int(b.get("n") or 0),
            "n_fechados": n,
            "nivel": nivel,
            # A EXPECTATIVA vem SEMPRE que houver base — é ela que lidera.
            "expectativa_r": b.get("expectativa_r"),
            "rr_medio": b.get("rr_medio"),
            "acerto_equilibrio": b.get("acerto_equilibrio"),
            "n_com_rr": b.get("n_com_rr"),
            "taxa_acerto": None,
            "ic95": None,
        }
        if nivel == "insuficiente":
            item["texto"] = (f"amostra insuficiente (n={n}) — sem fechados suficientes "
                             f"pra medir; track record em construção.")
        else:
            item["taxa_acerto"] = round(float(taxa), 4) if taxa is not None else None
            acertos = int(round(float(taxa) * n)) if taxa is not None else 0
            item["ic95"] = wilson(acertos, n)
            item["texto"] = ("taxa preliminar — intervalo largo, leia a expectativa primeiro."
                             if nivel == "preliminar"
                             else "amostra operável — a taxa já é número de trabalho.")
        out["setups"][nome] = item
    return out


def card(plan: dict[str, Any] | None, por_setup: dict[str, Any] | None = None) -> dict[str, Any]:
    """O card inteiro, pronto pra tela: veredito · ordens · saída · proteção · índice."""
    v = veredito_entrada(plan)
    return {
        "veredito": v,
        "ordens": ordens(plan, v),
        "invalidacao": (plan or {}).get("invalidation") or {},
        "saida": saida(plan),
        "protecao": protecao(),
        "peso": peso_relativo(plan),
        "confiabilidade": confiabilidade(por_setup),
    }


def peso_relativo(plan: dict[str, Any] | None) -> dict[str, Any]:
    """Quanto do capital — **sempre em RELATIVO, nunca em valor ou percentual**.

    Regra 8 do método: o Erick fala em proporção à confirmação (caixa · inicial ·
    meia · cheia), nunca em cifra. O degrau sai do ESTADO do setup, que é o que o
    plano já publica — quanto mais confirmado, maior o degrau —, e o card diz o
    degrau, não um número que ninguém disse.
    """
    estado = (plan or {}).get("setup_state") or ""
    degraus = {
        "ativo": ("meia posição", "o setup está ativo agora — proporção à confirmação"),
        "aguardar_rompimento": ("inicial", "o gatilho ainda não veio: entrada inicial, "
                                           "com espaço pra adicionar na confirmação"),
        "aguardar_pullback": ("inicial", "a entrada é no recuo — inicial, adicionando "
                                         "se o recuo for mais fundo"),
    }
    nome, motivo = degraus.get(estado, ("caixa", "sem confirmação para montar posição"))
    return {"degrau": nome, "motivo": motivo,
            "nota": "sempre relativo — o método não cita valor absoluto."}

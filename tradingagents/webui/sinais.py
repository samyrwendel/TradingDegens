"""SINAIS DE ENTRADA — a OPORTUNIDADE como unidade, não o par ativo×frame.

Pedido do Samyr: *"quero acompanhar e ver os gatilhos em tempo real como se fossem
sinais de entrada… tipo oportunidade no Setup123 no 1h e 4h compra janela de x a
y… não sei como organizar isso de uma maneira mais limpa"*.

**A unidade errada era a raiz.** A tabela do scan tem uma linha por ativo×frame —
que é a unidade do DADO. A unidade da DECISÃO é outra: ativo + método + direção,
com os frames que concordam agregados. Na tabela, saber se o 4h e o 1h dizem a
mesma coisa exige cruzar três linhas com o olho, e é exatamente esse cruzamento
que decide se vale entrar. A confluência já está no dado; a apresentação a
dissolvia.

Três coisas passam a existir aqui, e nenhuma é cosmética:

**1. CONFLUÊNCIA.** Frames vivos do mesmo ativo/método que apontam para a MESMA
direção viram UMA oportunidade, com os frames listados. Dois frames concordando
não é o dobro de um — é outra coisa —, e a lista precisa dizer quantos são.

**2. CONFLITO.** Frames vivos que apontam para direções OPOSTAS não viram
oportunidade nenhuma: viram um conflito declarado. Somá-los como se fossem sinal
seria inventar convicção onde o método está dividido. Medido na watchlist real de
31/08: AAPL (venda no 1d contra compra no 4h e 1h), TSM, AMD, LINK-USD e MP.

*Leitura MORTA não entra na conta.* ``invalidou`` quer dizer que a premissa
estrutural rompeu — o padrão não existe mais. Um 1-2-3 de compra invalidado no
diário enquanto o 4h e o 1h leem venda é CONFIRMAÇÃO (a compra morreu porque o
preço caiu), não contradição; contá-lo como voto criaria conflito fantasma
justamente onde o sinal está mais limpo. Ele não some da tela: viaja no card como
dissidente, com o estado dele escrito.

**3. JANELA DE ENTRADA — de X a Y, derivada, não inventada.** A faixa não existe
no dado (o gatilho é preço único), e ±N% seria cosmética. A derivação rigorosa:
entrando a um preço ``E``, o risco é ``|E − SL|`` e o retorno ``|TP − E|``, então

    R:R(E) = |TP − E| / |E − SL|

decai à medida que se entra mais tarde. Igualando ao mínimo aceitável ``m``:

    E_limite = (TP + m · SL) / (1 + m)

— a MESMA fórmula para compra e venda (a álgebra é simétrica; muda só de que lado
do gatilho o limite cai). A janela é o intervalo entre o gatilho e esse limite, e
ela **só existe se o R:R no gatilho já for ≥ m**.

Isso responde o que nenhuma tela de scan responde: não só ONDE entrar, mas **até
onde ainda vale** — e explica com número os R:R 0,06 que a tela já mostrava
(entrar depois do limite é isso).

**O mínimo é 1:1 (:data:`RR_MINIMO`), e é uma escolha declarada.** A taxa de
acerto de equilíbrio é ``1/(1+R)``: com R:R 1 é preciso acertar mais da metade só
pra empatar; com o R:R mediano medido do 1-2-3 (0,23) seriam **81%**. Não é
conceito novo no produto — o painel de track record já publica exatamente essa
conta (``acerto_equilibrio``); aqui ela sai do relatório e vai para o ponto da
decisão. PROVISÓRIO e declarado, como o ``_GATILHO_TOL`` (0,5%) e o
``_RR_RESIDUAL`` (0,05): a calibrar com o ledger quando a amostra passar do gate.

**Medido na watchlist real (31/08, 20 ativos × 3 frames), com m = 1,0:** têm
janela **6 de 55** leituras do 1-2-3 e **60 de 60** do Storm123 — e a mediana do
R:R no gatilho é 0,23 contra 1,41. Baixar para 0,5 levaria o 1-2-3 a 15 de 55 ao
preço de aceitar trade que precisa de 67% de acerto.

**Um achado que a janela expõe:** a janela costuma ser mais ESTREITA que a
tolerância que diz "está no gatilho". O ``_GATILHO_TOL`` é 0,5%, e janelas
medidas de 0,13% (CRWD no Storm) são comuns — ou seja, dá para estar "em gatilho"
pela lista e já ter passado do ponto em que o trade paga. É essa a diferença que
o card mostra com palavra.
"""
from __future__ import annotations

from typing import Any

# R:R mínimo que ainda paga o risco de entrar. Ver o argumento no topo do módulo:
# abaixo de 1:1 é preciso acertar mais da metade das vezes só para empatar.
# PROVISÓRIO e declarado — a calibrar com o track record.
RR_MINIMO = 1.0

# Estados de leitura que contam como VIVOS para confluência e conflito. Fora
# ficam ``invalidou`` (a premissa rompeu — o padrão não existe mais), ``sem_setup``
# e ``sem_dado`` (não há leitura). Ver "leitura MORTA não entra na conta".
_VIVOS = ("em_gatilho", "em_movimento", "formando")

# Quem já acionou (ou está no ponto) contra quem ainda vai. Decide se a
# oportunidade sem janela aberta é "já passou" ou "ainda não chegou".
_ACIONADOS = ("em_gatilho", "em_movimento")

# Ordem de urgência dentro de uma oportunidade: quem manda nos níveis é a leitura
# mais próxima de virar entrada.
_URGENCIA_FRAME = {"em_gatilho": 0, "formando": 1, "em_movimento": 2}

METODOS = ("123", "storm")
METODO_ROTULO = {"123": "Setup123", "storm": "Storm123"}


def limite_da_janela(sl: float, tp: float, rr_min: float = RR_MINIMO) -> float:
    """O preço em que o R:R cai exatamente para ``rr_min``.

    ``E_limite = (TP + m·SL) / (1 + m)`` — a mesma expressão para compra e venda:
    na compra ela fica ACIMA do gatilho (entrar mais caro piora), na venda ABAIXO
    (entrar mais barato piora). Não há caso a distinguir, e não distinguir é o que
    garante que os dois lados usem a mesma régua.
    """
    return (float(tp) + rr_min * float(sl)) / (1.0 + rr_min)


def rr_no_gatilho(trigger: float, sl: float, tp: float) -> float | None:
    """R:R de quem entra EXATAMENTE no gatilho. ``None`` se o risco for zero."""
    risco = abs(float(trigger) - float(sl))
    if risco <= 0:
        return None
    return abs(float(tp) - float(trigger)) / risco


def janela_de_entrada(trigger, sl, tp, price, direction,
                      rr_min: float = RR_MINIMO) -> dict[str, Any] | None:
    """A faixa de preço em que entrar ainda paga o risco — ou ``None``.

    Devolve ``None`` quando falta nível para calcular. Quando o setup não paga
    nem no gatilho, devolve um dicionário com ``existe: False`` e o MOTIVO escrito
    — porque "não há janela" e "não sei calcular" são coisas diferentes, e a tela
    tem de poder dizer qual das duas.
    """
    if trigger is None or sl is None or tp is None:
        return None
    trigger, sl, tp = float(trigger), float(sl), float(tp)
    rr_gat = rr_no_gatilho(trigger, sl, tp)
    if rr_gat is None:
        return None
    if rr_gat < rr_min:
        return {"existe": False, "rr_gatilho": rr_gat, "rr_min": rr_min,
                "motivo": f"no gatilho o R:R já é {rr_gat:.2f} — abaixo de "
                          f"{rr_min:.0f}:1 não há preço de entrada que pague o risco"}
    limite = limite_da_janela(sl, tp, rr_min)
    venda = direction == "venda"
    de, ate = (limite, trigger) if venda else (trigger, limite)
    out = {"existe": True, "de": de, "ate": ate, "limite": limite,
           "gatilho": trigger, "rr_gatilho": rr_gat, "rr_min": rr_min,
           "largura_pct": abs(limite / trigger - 1.0) if trigger else None}
    if price is None:
        out["estado"] = "sem_preco"
        return out
    price = float(price)
    # ANTES do gatilho: o padrão ainda não acionou. DEPOIS do limite: acionou e o
    # que sobra do movimento não paga mais o risco — é o "cheguei tarde" com nome.
    if (price < trigger) if not venda else (price > trigger):
        out["estado"] = "nao_abriu"
    elif (price <= limite) if not venda else (price >= limite):
        out["estado"] = "aberta"
    else:
        out["estado"] = "fechada"
    return out


def _leitura(f: dict[str, Any], metodo: str) -> dict[str, Any] | None:
    """A leitura de UM frame para UM método, normalizada — ou ``None`` se não há.

    Os dois métodos publicam campos com nomes iguais mas origens diferentes (o
    Storm mora em ``f["storm"]`` e carrega o veto do Éden). Normalizar aqui é o
    que permite uma regra de confluência só; ler cada um no seu formato lá na
    frente é como nasceriam duas regras que divergem.
    """
    if metodo == "123":
        estado, src = f.get("estado"), f
        aviso = None
    else:
        src = f.get("storm") or {}
        estado = src.get("estado")
        # VETO DO ÉDEN NÃO É OPORTUNIDADE (DA-079): gatilho que a regra proíbe
        # operar não é trade. Ele não vira card e não entra na contagem de
        # confluência — mas continua inteiro na tabela de dado.
        if estado == "vetado" or (src and src.get("opera") is False):
            return None
        aviso = ("zona neutra do Éden — o Stormer marca esta região como muito "
                 "mais perigosa") if src.get("zona_neutra") else None
        if estado == "zona_neutra":
            estado = "em_gatilho"
    if estado not in _VIVOS and estado != "invalidou":
        return None
    direcao = src.get("direction")
    if not direcao:
        return None
    return {"frame": f.get("frame"), "estado": estado, "direcao": direcao,
            "trigger": src.get("trigger"), "sl": src.get("sl"), "tp": src.get("tp"),
            "dist_pct": src.get("dist_pct"), "aviso": aviso,
            "entrada": src.get("entrada"), "ordem": src.get("ordem")}


def _lider(leituras: list[dict[str, Any]]) -> dict[str, Any]:
    """Qual leitura manda nos níveis: a mais perto de virar entrada.

    Mesma régua da coluna "melhor" do scan — urgência primeiro, distância do
    gatilho como desempate. Duas réguas fariam o card e a tabela apontarem frames
    diferentes para o mesmo ativo.
    """
    return min(leituras, key=lambda L: (
        _URGENCIA_FRAME.get(L["estado"], 9),
        L["dist_pct"] if L["dist_pct"] is not None else 9.9))


def _chave(ticker: str, metodo: str, direcao: str, trigger) -> str:
    """Identidade de um SINAL, para marcar o que é novo desde a última visita.

    Mesma família da chave de de-duplicação do ledger — ``(setup, ticker, frame,
    gatilho)``: é o GATILHO que identifica a instância do padrão. Sem ele, um
    padrão que morreu e outro que nasceu no mesmo ativo e direção seriam "o
    mesmo sinal", e o novo nunca se anunciaria.
    """
    g = "" if trigger is None else f"{float(trigger):.6g}"
    return f"{ticker}|{metodo}|{direcao}|{g}"


def _oportunidade(ticker: str, metodo: str, leituras: list[dict[str, Any]],
                  price, rr_min: float) -> dict[str, Any]:
    """Monta UMA oportunidade a partir das leituras vivas que concordam."""
    lider = _lider(leituras)
    janela = janela_de_entrada(lider["trigger"], lider["sl"], lider["tp"],
                               price, lider["direcao"], rr_min)
    # O ESTADO descreve o LÍDER, porque é dele que são os níveis mostrados. Ler o
    # estado de um frame e os níveis de outro produzia a contradição de um card
    # dizendo "já passou" com uma janela que ainda não abriu: outro frame tinha
    # acionado, mas o preço não chegou NESTE gatilho — que é o que está na tela.
    acionado = any(L["estado"] in _ACIONADOS for L in leituras)
    jestado = (janela or {}).get("estado")
    if (janela or {}).get("existe"):
        estado = {"aberta": "entrada", "nao_abriu": "a_caminho",
                  "fechada": "passou"}.get(jestado, "a_caminho" if not acionado else "passou")
    else:
        # Sem janela (o setup não paga nem no gatilho, ou faltam níveis) o que
        # resta é dizer se o gatilho já ficou para trás.
        estado = "passou" if acionado else "a_caminho"
    return {
        "ticker": ticker, "metodo": metodo, "metodo_rotulo": METODO_ROTULO[metodo],
        "direcao": lider["direcao"], "estado": estado,
        "frames": [L["frame"] for L in leituras],
        "confluencia": len(leituras),
        "frame_lider": lider["frame"],
        "gatilho": lider["trigger"], "sl": lider["sl"], "tp": lider["tp"],
        "preco": price,
        "rr_gatilho": (rr_no_gatilho(lider["trigger"], lider["sl"], lider["tp"])
                       if None not in (lider["trigger"], lider["sl"], lider["tp"])
                       else None),
        "janela": janela,
        "aviso": lider.get("aviso"),
        "entrada": lider.get("entrada"), "ordem": lider.get("ordem"),
        "chave": _chave(ticker, metodo, lider["direcao"], lider["trigger"]),
    }


def _conflito(ticker: str, metodo: str, por_direcao: dict[str, list],
              mortas: list[dict[str, Any]]) -> dict[str, Any]:
    """Frames vivos apontando para lados opostos: um conflito, nunca um sinal.

    Sem níveis e sem janela de propósito. Publicar gatilho e alvo aqui convidaria
    a operar um lado de uma leitura que está dividida — e a divisão é justamente
    a informação. Os dois lados vão inteiros, com os frames de cada um.
    """
    lados = [{"direcao": d, "frames": [L["frame"] for L in ls],
              "estados": [L["estado"] for L in ls]}
             for d, ls in sorted(por_direcao.items())]
    return {"ticker": ticker, "metodo": metodo, "metodo_rotulo": METODO_ROTULO[metodo],
            "estado": "conflito", "direcao": None, "lados": lados,
            "confluencia": 0,
            "frames": sorted({L["frame"] for ls in por_direcao.values() for L in ls}),
            "dissidentes": mortas,
            "chave": f"{ticker}|{metodo}|conflito"}


def oportunidades(scan: dict[str, Any], rr_min: float = RR_MINIMO) -> list[dict[str, Any]]:
    """As oportunidades de um resultado de scan, ordenadas por urgência de decisão.

    Uma por (ativo, método, direção) quando os frames vivos concordam; uma de
    CONFLITO por (ativo, método) quando não concordam. Ativo sem leitura viva
    nenhuma (todos os frames invalidados, sem setup ou sem dado) não aparece — não
    há oportunidade a mostrar, e inventar uma linha vazia seria afirmar ausência de
    setup onde o que há é ausência de leitura. Ele continua na tabela de dado.
    """
    out: list[dict[str, Any]] = []
    for a in scan.get("ativos") or []:
        ticker = a.get("ticker")
        if not ticker:
            continue
        frames = a.get("frames") or []
        price = next((f.get("price") for f in frames if f.get("price") is not None), None)
        for metodo in METODOS:
            lidas = [L for L in (_leitura(f, metodo) for f in frames) if L]
            vivas = [L for L in lidas if L["estado"] in _VIVOS]
            mortas = [{"frame": L["frame"], "direcao": L["direcao"], "estado": L["estado"]}
                      for L in lidas if L["estado"] == "invalidou"]
            if not vivas:
                continue
            por_direcao: dict[str, list] = {}
            for L in vivas:
                por_direcao.setdefault(L["direcao"], []).append(L)
            if len(por_direcao) > 1:
                out.append(_conflito(ticker, metodo, por_direcao, mortas))
                continue
            op = _oportunidade(ticker, metodo, vivas, price, rr_min)
            op["dissidentes"] = mortas
            op["total_frames"] = len(frames)
            out.append(op)
    _marcar_o_outro_metodo(out)
    out.sort(key=_ordem)
    return out


def _marcar_o_outro_metodo(ops: list[dict[str, Any]]) -> None:
    """Quando os dois métodos falam do mesmo ativo, cada card diz o que o outro lê.

    Eles NÃO se colapsam (DA-077: uma leitura, um card, com os SEUS níveis) — são
    detectores diferentes, com stop, alvo e filtro diferentes, e a média de dois
    métodos não descreve nenhum dos dois. Mas esconder que o Storm lê o contrário
    enquanto o 1-2-3 diz compra seria deixar o leitor descobrir sozinho, rolando.
    A menção é só isso: uma menção, com o nome do método e a direção dele.
    """
    por_ticker: dict[str, list[dict[str, Any]]] = {}
    for op in ops:
        por_ticker.setdefault(op["ticker"], []).append(op)
    for irmas in por_ticker.values():
        for op in irmas:
            outra = next((o for o in irmas if o["metodo"] != op["metodo"]), None)
            op["outro_metodo"] = (
                {"metodo": outra["metodo"], "metodo_rotulo": outra["metodo_rotulo"],
                 "direcao": outra["direcao"], "estado": outra["estado"]}
                if outra else None)


# Ordem de leitura da tela: o que se pode fazer AGORA primeiro; o aviso de
# conflito por último, porque ele não é entrada e não pode competir por atenção
# com quem é. Dentro de cada grupo, mais confluência antes, e R:R maior antes.
_ORDEM_ESTADO = {"entrada": 0, "a_caminho": 1, "passou": 2, "conflito": 3}


def _ordem(op: dict[str, Any]):
    # TER JANELA vem ANTES da confluência, e isto foi corrigido olhando a tela: um
    # setup de 3 frames cujo R:R no gatilho é 0,18 (não paga em preço nenhum)
    # aparecia acima de um de 2 frames com janela real de 1,37. Confluência mede
    # quantos concordam; janela mede se há o que operar — e num painel de decisão a
    # segunda pergunta vem primeiro.
    tem_janela = bool((op.get("janela") or {}).get("existe"))
    return (_ORDEM_ESTADO.get(op.get("estado"), 9),
            0 if tem_janela else 1,
            -(op.get("confluencia") or 0),
            -(op.get("rr_gatilho") or 0.0),
            op.get("ticker") or "")

"""Alertas no Telegram — e a SEPARAÇÃO DE DESTINO que os governa (DA-149).

São dois mecanismos, e o que mais importa aqui não é o formato: é **para onde cada
um pode ir**.

**(A) MUDANÇA NA CARTEIRA DO ERICK → SÓ DM, NUNCA GRUPO.** É conteúdo de assinatura
paga ("Acesso exclusivo para alunos"). O Samyr é aluno e pode consumir o que
comprou; a comunidade dele não comprou. Mandar isso a um grupo seria
**redistribuir conteúdo pago de terceiro** — e a diferença entre ler e redistribuir
é a diferença entre usar e infringir.

Por isso o destino é **parte da regra, não configuração**: :func:`destino_valido`
RECUSA mecanicamente qualquer chat de grupo para esta fonte. No Telegram, id de
grupo/supergrupo/canal é NEGATIVO e o de DM é positivo — a regra cabe numa
comparação, e é ela que impede que alguém, meses depois, "só troque o chat_id no
.env" sem lembrar por que ele era aquele.

**(B) SINAIS DO PRÓPRIO PRODUTO → podem ir ao grupo.** São gerados pelo sistema do
Samyr sobre dado de mercado público: ele distribui o que é dele.

**E "poder ir" não é "dever ir".** Já houve um bloqueio objetivo para entregar
análise à comunidade — 12 vereditos "Underweight" em 12 datas com o papel indo de
67 a 276 dólares, e a conclusão registrada foi que entregar aquilo seria
"distribuir um conselho constante disfarçado de análise". A mesma régua vale aqui,
e é por isso que o mecanismo de sinais **nasce DESLIGADO** com o critério escrito:
ligar é decisão de quem responde pela comunidade, depois de ver a amostra.
"""

from __future__ import annotations

import textwrap
from typing import Any

# ── (A) política de destino ────────────────────────────────────────────────────
# As fontes que o sistema sabe alertar, e o teto de distribuição de cada uma.
FONTE_CARTEIRA = "carteira_erick"
FONTE_SINAIS = "sinais_scan"

# Fonte cujo conteúdo é de terceiro: teto = DM do dono, e ponto.
_SO_DM = {FONTE_CARTEIRA}


def destino_valido(fonte: str, chat_id: int | str) -> tuple[bool, str]:
    """Este destino pode receber esta fonte? Devolve ``(ok, motivo)``.

    O motivo é devolvido mesmo no caso bom porque quem chama LOGA a decisão: um
    "não enviei" silencioso é indistinguível de um bug de rede, e esta é
    exatamente a regra que ninguém pode descobrir só quando for tarde.
    """
    try:
        cid = int(str(chat_id).strip())
    except (TypeError, ValueError):
        return False, f"chat_id inválido: {chat_id!r}"
    # Telegram: id NEGATIVO é grupo/supergrupo/canal; positivo é conversa privada.
    grupo = cid < 0
    if fonte in _SO_DM and grupo:
        return False, (
            "RECUSADO: a carteira do Erick é conteúdo de assinatura paga e só pode "
            "ir para a DM do dono — mandar a um grupo seria redistribuir conteúdo "
            "pago de terceiro (DA-149)")
    return True, "destino permitido para esta fonte"


# ── (A) o que mudou na carteira ────────────────────────────────────────────────
def _por_ticker(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    ativos = ((payload or {}).get("carteira") or {}).get("ativos") or []
    return {str(a.get("ticker") or "").upper(): a for a in ativos if a.get("ticker")}


def _valor(a: dict[str, Any]) -> float:
    try:
        return float(a.get("qtd") or 0) * float(a.get("precoMedio") or 0)
    except (TypeError, ValueError):
        return 0.0


def mudancas(anterior: dict[str, Any] | None,
             atual: dict[str, Any] | None) -> list[dict[str, Any]]:
    """O que mudou entre duas leituras — ativo, o QUE mudou e o % DO CAPITAL.

    O percentual é sobre o capital TOTAL da leitura NOVA (posições + caixa): é o
    número que responde "o quanto isso importa na carteira dele", que foi o que o
    Samyr pediu. Medir sobre o total antigo faria uma entrada nova parecer maior
    do que é, e sobre o valor da própria posição não diria nada sobre peso.

    Sem leitura anterior devolve ``[]`` — a PRIMEIRA leitura não é mudança. Sem
    isso, o primeiro alerta seria a carteira inteira anunciada como novidade.
    """
    if not anterior or not atual:
        return []
    antes, agora = _por_ticker(anterior), _por_ticker(atual)
    total = sum(_valor(a) for a in agora.values()) or 1.0
    out: list[dict[str, Any]] = []
    for tk in sorted(set(antes) | set(agora)):
        a, b = antes.get(tk), agora.get(tk)
        va, vb = _valor(a or {}), _valor(b or {})
        if a is None and b is not None:
            tipo = "entrou"
        elif b is None and a is not None:
            tipo = "saiu"
        else:
            qa, qb = float((a or {}).get("qtd") or 0), float((b or {}).get("qtd") or 0)
            if abs(qb - qa) < 1e-9:
                continue
            tipo = "aumentou" if qb > qa else "reduziu"
        out.append({
            "ticker": tk,
            "nome": ((b or a) or {}).get("nome") or "",
            "classe": ((b or a) or {}).get("classe") or "",
            "tipo": tipo,
            # a MAGNITUDE da mudança em % do capital de hoje — não o peso final
            "pct_capital": abs(vb - va) / total,
            "peso_agora": (vb / total) if b is not None else 0.0,
            "qtd_antes": (a or {}).get("qtd"),
            "qtd_agora": (b or {}).get("qtd"),
        })
    return out


def _racional(atual: dict[str, Any] | None, ticker: str) -> str:
    """O que ELE escreveu sobre a mudança, quando existe. É o que dá sentido —
    "reduziu 12% do capital em BE" sem o porquê é um número sem leitura."""
    feed = ((atual or {}).get("carteira") or {}).get("feed") or []
    for f in feed:
        if str(f.get("ticker") or "").upper() == ticker.upper():
            return str(f.get("resumo") or f.get("texto") or "").strip()
    return ""


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.1f}%".replace(".", ",")


def _money(v: float) -> str:
    s = f"{abs(v):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"{'-' if v < 0 else ''}${s}"


def _qtd(v: float | None) -> str:
    """Quantidade pro humano ler — sem zero de sobra, vírgula decimal."""
    if v is None:
        return "0"
    v = float(v)
    s = f"{v:.4f}".rstrip("0").rstrip(".") if v != int(v) else str(int(v))
    return s.replace(".", ",")


def _caixa(payload: dict[str, Any] | None) -> tuple[float, float, float | None]:
    """(valor do caixa, total da carteira, % do total em caixa) de uma leitura."""
    ativos = _por_ticker(payload)
    total = sum(_valor(a) for a in ativos.values())
    caixa = sum(_valor(a) for a in ativos.values()
                if str(a.get("classe") or "").strip().lower() == "caixa")
    return caixa, total, (caixa / total if total else None)


_BARRA_BLOCOS = 5


def _barra(valor: float, maximo: float, blocos: int = _BARRA_BLOCOS) -> str:
    """Barra de magnitude relativa ao MAIOR movimento do lote — o tamanho salta aos
    olhos sem precisar ler o número (DA-034)."""
    cheios = round(blocos * abs(valor) / maximo) if maximo > 0 else 0
    cheios = max(0, min(blocos, cheios))
    return "█" * cheios + "░" * (blocos - cheios)


def _quebra(texto: str, prefixo: str = "  ↳ ", largura: int = 60) -> list[str]:
    """Quebra uma linha longa em várias de até ``largura`` chars — o dente que
    proíbe parede de texto numa linha só (DA-033)."""
    if not texto:
        return []
    disponivel = max(20, largura - len(prefixo))
    primeira, resto = True, textwrap.wrap(texto, width=disponivel)
    out = []
    for linha in resto:
        out.append(f"{prefixo}{linha}" if primeira else f"{' ' * len(prefixo)}{linha}")
        primeira = False
    return out


_VERBO = {"entrou": "ENTROU", "saiu": "SAIU", "aumentou": "AUMENTOU", "reduziu": "REDUZIU"}
_EMOJI_MUDANCA = {"entrou": "🟢⬆", "aumentou": "🟢⬆", "reduziu": "🔴⬇", "saiu": "🔴⬇"}


def formata_carteira(mudou: list[dict[str, Any]], atual: dict[str, Any] | None,
                     anterior: dict[str, Any] | None = None) -> str:
    """A mensagem de DM, no formato lúdico que o Samyr aprovou (DA-034): emoji como
    MARCADOR DE DADO — não decoração — (🟢⬆ ganhou exposição, 🔴⬇ perdeu), barra de
    magnitude proporcional ao maior movimento do lote, ZERO markdown (Telegram
    mostra asterisco cru), respiro entre blocos e fecho com a AÇÃO em linha própria.

    ``anterior`` é opcional só por compatibilidade — sem ele o caixa mostra o valor
    de agora sem o "antes → depois" (task 20260902-053 pediu esse contraste, mas
    quem já chamava com dois argumentos não pode quebrar).

    Lista vazia devolve string vazia — e quem chama NÃO envia. "Nenhuma mudança
    hoje" todo dia é o ruído que faz o alerta deixar de ser lido.
    """
    if not mudou:
        return ""

    caixa_v, total, caixa_pct = _caixa(atual)
    _, _, caixa_pct_antes = _caixa(anterior) if anterior else (0.0, 0.0, None)

    linhas = [f"💼 CARTEIRA DO ERICK mudou — {len(mudou)} movimento(s)"]
    if total > 0:
        linhas.append(f"Total: {_money(total)}")
        if caixa_pct_antes is not None and caixa_pct is not None:
            caixa_txt = f"{_pct(caixa_pct_antes)} → {_pct(caixa_pct)}"
        else:
            caixa_txt = _pct(caixa_pct)
        linhas.append(f"  ↳ caixa {_money(caixa_v)} · {caixa_txt} do total")
    atualizado = ((atual or {}).get("carteira") or {}).get("atualizado")
    if atualizado:
        linhas.append(f"  ↳ publicado por ele em {atualizado}")
    linhas.append("")

    maior_pct = max(m["pct_capital"] for m in mudou) or 1.0
    for m in mudou:
        emoji = _EMOJI_MUDANCA.get(m["tipo"], "⚪")
        cab = (f"{emoji} {m['ticker']} {_VERBO.get(m['tipo'], m['tipo'])} "
               f"· {_pct(m['pct_capital'])} do capital {_barra(m['pct_capital'], maior_pct)}")
        if m["tipo"] != "saiu":
            cab += f" · peso {_pct(m['peso_agora'])}"
        linhas.append(cab)
        linhas.extend(_quebra(f"qtd {_qtd(m['qtd_antes'])} → {_qtd(m['qtd_agora'])}"))
        if m.get("nome"):
            linhas.extend(_quebra(m["nome"]))
        r = _racional(atual, m["ticker"])
        if r:
            linhas.extend(_quebra(r[:400]))
    linhas.append("")

    maior = max(mudou, key=lambda m: m["pct_capital"])
    linhas.append(f"👉 Olhar primeiro: {maior['ticker']} "
                  f"({_VERBO.get(maior['tipo'], maior['tipo']).lower()}, "
                  f"{_pct(maior['pct_capital'])} do capital)")
    return "\n".join(linhas).strip()


# ── (B) sinais do scan ─────────────────────────────────────────────────────────
#
# O CRITÉRIO É PROPOSTA, e vem DESLIGADO. Três condições, e cada número tem razão:
#
#   1. estado ``em_gatilho`` — só o que pede decisão AGORA. "Aguardando" num grupo
#      vira ruído diário sobre o mesmo papel.
#   2. R:R >= 1,5 — e o piso NÃO é 1. MEDIDO no ledger real deste produto
#      (`scans.jsonl`, 01/09/2026): 144 gatilhos logados, 88 com R:R conhecido,
#      MEDIANA 1,12 — só 55% chegam a 1,0 e 35% a 1,5. Ou seja: um alerta sem piso
#      distribuiria, quase metade das vezes, um trade que perde dinheiro por
#      construção. O piso em 1,5 corta ~2 de cada 3 antes mesmo da confluência.
#   3. CONFLUÊNCIA de 2+ frames na mesma direção — um gatilho de 1h sozinho é a
#      fonte mais barata de sinal e a mais fácil de invalidar na hora seguinte.
#
# Nada disso está calibrado por track record ainda, e é exatamente por isso que o
# mecanismo entrega desligado: a régua da comunidade é a mesma que já barrou
# distribuir análise antes — sinal que vai ao grupo precisa ser DEFENSÁVEL.
RR_MINIMO = 1.5
FRAMES_MINIMOS = 2

_VIVOS = ("em_gatilho",)


def sinais_dignos(scan: dict[str, Any] | None, *, rr_minimo: float = RR_MINIMO,
                  frames_minimos: int = FRAMES_MINIMOS) -> list[dict[str, Any]]:
    """Os gatilhos do scan salvo que passam no critério acima.

    Lê o scan JÁ SALVO (a agenda o grava a cada passada): não dispara varredura,
    não chama LLM, não bate em fonte nenhuma.
    """
    ativos = (scan or {}).get("ativos") or []
    out = []
    for a in ativos:
        frames = a.get("frames") or []
        gatilhos = [f for f in frames if f.get("estado") in _VIVOS and f.get("direction")]
        if not gatilhos:
            continue
        for direcao in {str(f.get("direction")) for f in gatilhos}:
            mesmos = [f for f in gatilhos if str(f.get("direction")) == direcao]
            if len(mesmos) < frames_minimos:
                continue
            # o melhor R:R entre os frames confluentes é o que se anuncia; o piso
            # é aplicado sobre ELE, não sobre a média (média esconde um 0,3)
            rrs = [f.get("rr") for f in mesmos if isinstance(f.get("rr"), (int, float))]
            if not rrs or max(rrs) < rr_minimo:
                continue
            melhor = max(mesmos, key=lambda f: (f.get("rr") or 0))
            out.append({
                "ticker": a.get("ticker"), "direcao": direcao,
                "frames": [f.get("frame") for f in mesmos],
                "frame_lider": melhor.get("frame"), "rr": melhor.get("rr"),
                "trigger": melhor.get("trigger"), "sl": melhor.get("sl"),
                "tp": melhor.get("tp"),
                "chave": chave_do_sinal(a.get("ticker"), melhor),
            })
    return out


def chave_do_sinal(ticker: str | None, frame_row: dict[str, Any]) -> str:
    """Identidade do sinal, pra NÃO re-alertar o mesmo gatilho.

    Mesma forma do ledger do scan (``ticker|frame|trigger``): o mesmo gatilho no
    mesmo frame é o mesmo sinal, quantas vezes a agenda passar por ele. Sem isto o
    grupo receberia o mesmo alerta de hora em hora enquanto o preço não sai da
    faixa — a maneira mais rápida de ensinar todo mundo a ignorar o canal.
    """
    return "|".join([str(ticker or ""), str(frame_row.get("frame") or ""),
                     str(frame_row.get("trigger") or "")])


_DIR_PT = {"compra": "COMPRA", "venda": "VENDA"}


def formata_sinais(sinais: list[dict[str, Any]], *, quando: str = "") -> str:
    """A mensagem de sinal. Curta por escolha: um grupo lê de relance, e o detalhe
    que não cabe aqui está na tela. Lista vazia → string vazia (não envia)."""
    if not sinais:
        return ""
    linhas = [f"*Sinais do scan* — {len(sinais)} em gatilho", ""]
    for s in sinais:
        frames = " + ".join(str(f) for f in (s.get("frames") or []))
        linhas.append(
            f"*{s['ticker']}* {_DIR_PT.get(s['direcao'], s['direcao'])} "
            f"· {frames} · R:R {s['rr']:.2f}".replace(".", ","))
        linhas.append(f"  gatilho {s['trigger']} · stop {s['sl']} · alvo {s['tp']}")
        linhas.append("")
    linhas.append(f"Critério: em gatilho, R:R ≥ {RR_MINIMO} e {FRAMES_MINIMOS}+ frames "
                  f"na mesma direção.".replace(".", ",", 1))
    if quando:
        linhas.append(f"Varredura de {quando}.")
    return "\n".join(linhas).strip()

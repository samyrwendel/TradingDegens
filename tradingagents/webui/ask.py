"""Q&A ancorado sobre uma análise JÁ computada (endpoint ``/api/ask``).

O Samyr lê o veredito e pergunta, em linguagem natural, sobre AQUELA run — ex.:
sobre "esperar o recuo à média antes de agir com peso", pergunta "onde seria
isso?" e quer o NÍVEL concreto (a faixa das EMAs 8/21, ~preço X), não papo vago.

Este módulo NÃO re-roda a análise nem busca dado externo: monta o contexto a
partir do que a run já computou e cacheou — ``price_structure`` (EMA 8/21/50, MMS,
zona de compra/realização, gatilho 1-2-3, preço no momento), o veredito e os
relatórios — e devolve as mensagens pro modelo barato responder.

Regras da casa embutidas aqui:

* **Grounding obrigatório em número:** todo nível citado sai dos DADOS reais desta
  run. O prompt proíbe inventar preço/nível/data.
* **Sem base = honesto:** número ausente vira "sem nível definido"; a resposta diz
  que não dá pra afirmar, nunca chuta (mesma regra de fonte caída = indisponível).
* **Anti look-ahead por construção:** só usa o que a run fechou em ``as_of`` — não
  há como enxergar candle futuro porque nada é recomputado.

As funções de montagem são puras (sem LLM, sem I/O) pra serem testáveis; o
:class:`~tradingagents.webui.runner.AnalysisRunner` chama o modelo e mede o custo.
"""

from __future__ import annotations

from typing import Any

# Modelo barato responde perguntas curtas ancoradas; sem rodeio, sem inventar
# número, e honesto quando não há base. O idioma é PT-BR (o Samyr fala PT-BR).
SYSTEM_PROMPT = (
    "Você responde perguntas sobre UMA análise de trading JÁ computada (a \"run\"). "
    "Regras invioláveis:\n"
    "- Responda em português do Brasil, curto e direto (2 a 5 frases). Sem saudação, "
    "sem repetir a pergunta, sem encher linguiça.\n"
    "- ANCORE todo nível/preço nos números da seção DADOS (vindos do price_structure "
    "desta run): EMA 8/21/50, médias MMS, zona de compra, zona de realização, gatilho "
    "1-2-3 e preço no momento. Cite o NÚMERO real de lá.\n"
    "- COPIE cada número e cada rótulo EXATAMENTE como aparecem em DADOS — não arredonde, "
    "não altere um dígito, não invente vírgula. As ÚNICAS médias que existem são EMA 8, "
    "EMA 21, EMA 50 e MMS 20, MMS 50, MMS 200: NUNCA cite uma média que não esteja "
    "listada em DADOS (nada de \"EMA 10\", \"média de 9\" etc.). Se uma média ou nível "
    "vier marcado \"sem dado\" em DADOS, diga que esta análise não tem esse valor — não "
    "pegue o número de outra média no lugar.\n"
    "- Pergunta de \"onde / qual nível / quanto / a que preço\" DEVE devolver o preço ou "
    "a FAIXA real dos DADOS. Ex.: \"recuo à média\" no método Erick = a faixa das EMAs "
    "8 e 21 (com os números exatos). Nunca responda de forma vaga quando o número existe "
    "nos DADOS.\n"
    "- Se DADOS tiver mais de uma leitura (ex.: Padrão e Método Erick), e a pergunta for "
    "de uma delas, use os números DAQUELA coluna e diga de qual leitura são. Não misture "
    "nem faça média das duas.\n"
    "- Se a pergunta não tiver base nos DADOS nem nos RELATÓRIOS desta run, diga "
    "honestamente que não dá pra afirmar com esta análise. JAMAIS invente número, "
    "nível, data ou fato.\n"
    "- Use SOMENTE o que está aqui — é a análise fechada nesta data. Não fale de dado "
    "futuro nem sugira buscar fora."
)

# Teto de contexto por relatório: mantém a pergunta barata sem cortar o miolo.
_REPORT_CAP = 1400
# Quantos relatórios (e em que ordem) entram no contexto de uma análise simples.
_SINGLE_REPORTS = (
    ("erick_report", "Método Erick (recuo à média · saída · peso)"),
    ("trader_plan", "Plano do Trader"),
    ("final_trade_decision", "Decisão final"),
    ("market_report", "Mercado"),
    ("research_manager", "Juiz do debate"),
    ("news_report", "Notícias"),
)
# No confronto cada coluna carrega só estes (é o que build_column preserva).
_COLUMN_REPORTS = (
    ("erick_report", "Método Erick"),
    ("trader_plan", "Plano do Trader"),
    ("final_decision", "Decisão final"),
)


def _num(v: Any) -> str | None:
    """Formata um número no padrão da casa (vírgula decimal), ou ``None``.

    ``None`` sobe como ``None`` pra quem chama decidir "sem nível definido" — nunca
    vira 0 nem texto inventado."""
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    s = f"{f:,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".")
    return s


def _last_valid(seq: Any) -> Any:
    """Último valor não-nulo de uma série (a EMA/MMS "de agora"), ou ``None``."""
    if not isinstance(seq, (list, tuple)):
        return None
    for v in reversed(seq):
        if v is not None:
            return v
    return None


def _zone_line(label_pt: str, zone: dict | None) -> tuple[str, bool]:
    """``(linha, tem_numero)`` pra uma zona (compra/realização/recuo).

    Sem base numérica → "sem nível definido" e ``False`` (nunca um número
    inventado); com âncora real → linha com preço + faixa e ``True``."""
    if not isinstance(zone, dict):
        return f"{label_pt}: sem nível definido.", False
    price = _num(zone.get("price"))
    if price is None:
        return f"{label_pt}: sem nível definido.", False
    lbl = str(zone.get("label") or "").strip()
    lo, hi = _num(zone.get("low")), _num(zone.get("high"))
    band = f" (faixa {lo}–{hi})" if lo and hi else ""
    head = f"{lbl} → " if lbl else ""
    return f"{label_pt}: {head}{price}{band}.", True


def price_facts(actionable: dict | None, price_chart: dict | None) -> list[str]:
    """Linhas de FATOS numéricos desta run (só números reais do price_structure).

    Retorna ``[]`` quando não há nenhum número — o chamador marca a run como "sem
    base numérica" e a resposta segue honesta em vez de fabricar nível."""
    actionable = actionable or {}
    price_chart = price_chart or {}
    lines: list[str] = []
    has_number = False

    price = _num(actionable.get("price"))
    as_of = str(actionable.get("as_of") or price_chart.get("as_of") or "").strip()
    if price is not None:
        when = f" (fechamento de {as_of})" if as_of else ""
        lines.append(f"Preço no momento da análise: {price}{when}.")
        has_number = True

    # Enumera TODAS as janelas (mesmo as ausentes → "sem dado"): com o inventário
    # completo à vista, o modelo não preenche a lacuna com um número vizinho quando
    # perguntam de uma média que esta run não computou (ex.: MMS 200 sem histórico).
    ema = price_chart.get("ema") or {}
    ema_bits = []
    for w in ("8", "21", "50"):
        val = _num(_last_valid(ema.get(w)))
        ema_bits.append(f"EMA {w}: {val}" if val is not None else f"EMA {w}: sem dado")
        has_number = has_number or val is not None
    lines.append(
        "Médias exponenciais (EMA) — referência do \"recuo à média\" do método "
        "Erick: " + " · ".join(ema_bits) + "."
    )

    ma = price_chart.get("ma") or {}
    ma_bits = []
    for w in ("20", "50", "200"):
        val = _num(_last_valid(ma.get(w)))
        ma_bits.append(f"MMS {w}: {val}" if val is not None else f"MMS {w}: sem dado")
        has_number = has_number or val is not None
    lines.append("Médias simples (MMS): " + " · ".join(ma_bits) + ".")

    for label_pt, key in (("Zona de compra", "buy_zone"),
                          ("Zona de realização", "realize_zone"),
                          ("Recuo/gatilho a aguardar", "pullback_zone")):
        line, ok = _zone_line(label_pt, actionable.get(key))
        lines.append(line)
        has_number = has_number or ok

    pat = actionable.get("pattern")
    if isinstance(pat, dict) and _num(pat.get("trigger")) is not None:
        direction = pat.get("direction") or ""
        state = pat.get("state") or ""
        tag = " ".join(x for x in ("de", direction) if x).strip()
        lines.append(
            f"Padrão 1-2-3 {tag} ({state}): gatilho em {_num(pat.get('trigger'))}."
        )
        has_number = True

    # Onde INVALIDA / stop / alvo / R:R — os níveis que tornam o padrão operável.
    # Entram no grounding pelo mesmo contrato das zonas: número real ou a frase
    # "sem nível definido", nunca um nível preenchido pelo modelo.
    inval = actionable.get("invalidation")
    if isinstance(inval, dict) and _num(inval.get("price")) is not None:
        lines.append(
            f"Invalidação do padrão: {_num(inval['price'])} — {inval.get('meaning') or ''}".strip()
        )
        has_number = True
    else:
        lines.append("Invalidação do padrão: sem nível definido.")

    stop = actionable.get("stop")
    if isinstance(stop, dict) and _num(stop.get("price")) is not None:
        lines.append(f"Stop (SL): {_num(stop['price'])} ({stop.get('basis') or 'estrutura'}).")
        has_number = True
    else:
        lines.append("Stop (SL): sem nível definido.")

    tgt = actionable.get("target")
    if isinstance(tgt, dict) and _num(tgt.get("price")) is not None:
        lo, hi = _num(tgt.get("low")), _num(tgt.get("high"))
        band = f" (faixa {lo}–{hi})" if lo and hi else ""
        same = " — mesmo nível da zona de realização" if tgt.get("same_as_realize") else ""
        lines.append(f"Alvo (TP) do padrão: {_num(tgt['price'])}{band} — {tgt.get('label') or ''}{same}.")
        has_number = True
    else:
        lines.append("Alvo (TP) do padrão: sem nível definido.")

    rr = actionable.get("risk_reward")
    if isinstance(rr, dict) and _num(rr.get("rr")) is not None:
        lines.append(
            f"Risco/retorno: {_num(rr['rr'])}:1 — entrada {_num(rr.get('entry'))} "
            f"({rr.get('entry_basis') or ''}), risco {_num(rr.get('risk'))}, "
            f"retorno {_num(rr.get('reward'))}."
        )
        has_number = True
    elif isinstance(rr, dict):
        lines.append(f"Risco/retorno: não calculável — {rr.get('note') or 'sem base'}.")
    else:
        lines.append("Risco/retorno: sem base (stop ou alvo indefinido).")

    # Zonas "sem nível definido" sozinhas não contam como base numérica.
    return lines if has_number else []


def _clip(text: str, cap: int = _REPORT_CAP) -> str:
    text = (text or "").strip()
    if len(text) <= cap:
        return text
    return text[:cap].rstrip() + " […]"


def _reports_block(source: dict, fields: tuple) -> str:
    out: list[str] = []
    for key, title in fields:
        val = _clip(str(source.get(key) or ""))
        if val:
            out.append(f"### {title}\n{val}")
    return "\n\n".join(out)


def build_context(record: dict) -> dict[str, Any]:
    """Monta o contexto ancorado de uma run (simples ou confronto).

    Retorna ``{mode, ticker, timeframe, as_of, facts, reports, has_numbers}``.
    ``facts`` é o bloco de números reais (o grounding); ``reports`` é o texto das
    leituras; ``has_numbers`` diz se havia algum nível pra ancorar."""
    result = record.get("result") or {}
    ticker = record.get("ticker") or result.get("ticker") or ""
    timeframe = (
        record.get("verdict_timeframe") or result.get("verdict_timeframe")
        or result.get("timeframe") or "1d"
    )
    compare = result.get("compare")

    if isinstance(compare, dict) and (compare.get("a") or compare.get("b")):
        facts_parts: list[str] = []
        reports_parts: list[str] = []
        has_numbers = False
        as_of = ""
        for slot in ("a", "b"):
            col = compare.get(slot) or {}
            if not col:
                continue
            label = col.get("label") or ("Padrão" if slot == "a" else "Método Erick")
            verdict = col.get("verdict") or "—"
            actionable = col.get("actionable") or {}
            as_of = as_of or str(actionable.get("as_of") or "")
            facts = price_facts(actionable, col.get("price_chart"))
            if facts:
                has_numbers = True
            head = f"[{label} — veredito {verdict}]"
            facts_parts.append(
                head + "\n" + ("\n".join(facts) if facts
                               else "Sem níveis numéricos nesta coluna.")
            )
            rep = _reports_block(col, _COLUMN_REPORTS)
            if rep:
                reports_parts.append(f"[{label}]\n{rep}")
        meta = compare.get("meta") or {}
        for mk, mt in (("agreement", "Concordância"), ("divergence", "Divergência"),
                       ("meaning", "O que significa")):
            mv = _clip(str(meta.get(mk) or ""), 800)
            if mv:
                reports_parts.append(f"### Meta-juiz — {mt}\n{mv}")
        return {
            "mode": "compare",
            "ticker": ticker,
            "timeframe": timeframe,
            "as_of": as_of,
            "facts": "\n\n".join(facts_parts),
            "reports": "\n\n".join(reports_parts),
            "has_numbers": has_numbers,
        }

    # análise simples
    actionable = result.get("actionable") or {}
    facts = price_facts(actionable, result.get("price_chart"))
    verdict = result.get("verdict") or record.get("verdict") or "—"
    facts_text = f"[Veredito {verdict} · timeframe {timeframe}]\n" + (
        "\n".join(facts) if facts else "Sem níveis numéricos nesta análise."
    )
    return {
        "mode": "single",
        "ticker": ticker,
        "timeframe": timeframe,
        "as_of": str(actionable.get("as_of") or ""),
        "facts": facts_text,
        "reports": _reports_block(result, _SINGLE_REPORTS),
        "has_numbers": bool(facts),
    }


def build_user_prompt(context: dict, question: str) -> str:
    """Texto do turno do usuário: DADOS (grounding) + RELATÓRIOS + a PERGUNTA."""
    ticker = context.get("ticker") or "—"
    timeframe = context.get("timeframe") or "1d"
    as_of = context.get("as_of") or ""
    fechada = f" · fechada em {as_of}" if as_of else ""
    reports = context.get("reports") or "(sem relatórios de texto nesta run)"
    return (
        f"DADOS (números reais desta análise — {ticker} · timeframe {timeframe}{fechada}; "
        "ancore os níveis AQUI):\n"
        f"{context.get('facts') or '(sem níveis numéricos)'}\n\n"
        f"RELATÓRIOS (texto das leituras desta run):\n{reports}\n\n"
        f"PERGUNTA: {question.strip()}"
    )


def build_messages(record: dict, question: str) -> tuple[list[tuple[str, str]], dict]:
    """``([(role, content), …], meta)`` pronto pro ``.invoke`` do chat client.

    ``meta`` leva ``mode/ticker/timeframe/as_of/has_numbers`` pra UI e os testes."""
    context = build_context(record)
    messages = [
        ("system", SYSTEM_PROMPT),
        ("human", build_user_prompt(context, question)),
    ]
    meta = {k: context[k] for k in ("mode", "ticker", "timeframe", "as_of", "has_numbers")}
    return messages, meta

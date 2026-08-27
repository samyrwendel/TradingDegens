"""Leitura determinística do MÉTODO ERICK — o núcleo do analista `erick`.

O analista `erick` (LLM) escreve a moldura (regime, macro, filtros de sentimento
e derivativo). Este módulo garante — determinístico, ancorado em dado de
ferramenta, jamais inventado — a parte que o método EXIGE e que a prosa costuma
deixar solta: **timeframe intradiário, entrada no recuo à média (EMA 8/21),
saída antes da reversão, e o PESO RELATIVO do trade** (posição cheia / meia /
inicial / caixa) — a resposta ao "quantos %" do Samyr sem chutar valor absoluto.

Modelado de 4 fontes independentes (59 transcrições, 18 frames de gráfico,
carteira real 62% em caixa, racional escrito por posição). Ver
`~/brain/trading-ops/modelo-decisorio-erick-sekiama.md`. O eixo é a média móvel
(EMA 8/21 pro timing, 50 pra tendência), no 15m/4h; entrada FRACIONADA no recuo
à média; saída em exaustão ("pega a maior parte e sai antes de reverter"); caixa
é posição ativa; separa tático de estrutural.

Tudo reusa a fundação já pronta (EMA + intradiário + região/1-2-3 do
price_structure, cacheado e date-guarded). Fail-open: qualquer erro devolve o
relatório intacto — enriquecimento nunca derruba a análise.
"""
from __future__ import annotations

import logging

from tradingagents.dataflows.price_structure import (
    build_actionable_plan_dict,
    build_price_chart,
)

logger = logging.getLogger(__name__)

# O método vive no intradiário — 4h de swing + 15m de timing fino — para QUALQUER
# ativo: cripto lê o candle real da exchange, ação lê o intradiário keyless do
# yfinance (15m/1h nativos, 4h reamostrado por pregão). Quando a fonte não tem
# candle pro símbolo/data (ação fora da janela intradiária do yfinance, ou feed
# fora do ar) a leitura cai no diário e DECLARA o degradê — nunca inventa barra.
_SWING_FRAME = "4h"
_FINE_FRAME = "15m"
_FALLBACK_FRAME = "1d"

_FRAME_LABEL = {
    "15m": "15 minutos (intradiário)",
    "1h": "1 hora (intradiário)",
    "4h": "4 horas (intradiário)",
    "1d": "diário",
    "1w": "semanal",
}

# Toque na média: dentro desta faixa o preço está "na média" (recuo concluído /
# em curso). Fora, esticado (aguardar recuo) ou abaixo (sem gatilho de alta).
_TOUCH_TOL = 0.004      # 0,4%
_EXTENDED = 0.012       # 1,2% acima da EMA21 = esticado


def _fmt(v) -> str:
    return f"{v:,.2f}" if isinstance(v, (int, float)) else "—"


def _last(seq):
    """Último valor não-nulo de uma série (EMA alinhada tem None no começo)."""
    if not seq:
        return None
    for v in reversed(seq):
        if v is not None:
            return v
    return None


def _fmt_zone(zone) -> str | None:
    """Faixa {label, price, low, high} -> texto. Banda quando há ATR, senão ponto."""
    if not zone:
        return None
    label = zone.get("label") or ""
    low, high, price = zone.get("low"), zone.get("high"), zone.get("price")
    if low is not None and high is not None:
        return f"{label}: {_fmt(low)}–{_fmt(high)}"
    if price is not None:
        return f"{label}: {_fmt(price)}"
    return label or None


def _ema_read(chart: dict) -> dict | None:
    """Leitura de EMA 8/21/50 no último candle do frame. None se sem dado."""
    candles = (chart or {}).get("candles") or []
    ema = (chart or {}).get("ema") or {}
    if not candles:
        return None
    close = candles[-1].get("c")
    e8, e21, e50 = _last(ema.get("8")), _last(ema.get("21")), _last(ema.get("50"))
    if close is None or e21 is None:
        return None

    # Tendência pela pilha de médias (o "regime antes do preço" do método).
    if e8 is not None and e50 is not None and e8 > e21 > e50:
        trend = "alta"
    elif e8 is not None and e50 is not None and e8 < e21 < e50:
        trend = "baixa"
    else:
        trend = "transicao"

    dist8 = (close - e8) / e8 if e8 else None
    dist21 = (close - e21) / e21
    near = min(abs(d) for d in (dist8, dist21) if d is not None)
    at_media = near <= _TOUCH_TOL
    extended = trend == "alta" and dist21 > _EXTENDED
    below = close < e21
    return {
        "close": close, "e8": e8, "e21": e21, "e50": e50,
        "dist8": dist8, "dist21": dist21,
        "trend": trend, "at_media": at_media, "extended": extended, "below": below,
    }


_TREND_PT = {"alta": "alta (médias empilhadas)", "baixa": "baixa (médias invertidas)",
             "transicao": "transição (médias entrelaçadas)"}

# Rótulo compacto do frame para a linha do gatilho 1-2-3 (evita parênteses aninhados).
_COMPACT_FRAME = {"15m": "15m", "1h": "1h", "4h": "4h", "1d": "diário", "1w": "semanal"}


def _estado(acao: str, trend: str, drop_cls: str | None = None) -> str:
    """The single method state enum (item 6b): AGIR | AGUARDAR | CAIXA, computed ONCE
    so every sub-block renders from it (no 'Veredito AGUARDAR' vs 'Estado AGIR').

    A natureza da queda (``drop_cls``) é a FONTE DE VERDADE quando presente:
    * ``fraqueza`` → CAIXA (estrutura rompida veta, mesmo um AGIR mecânico);
    * ``liquidacao_saudavel`` numa tendência de "baixa" → AGUARDAR (a inversão das
      EMAs curtas é o SINTOMA da liquidação, não um downtrend a ler como CAIXA).

    ``drop_cls=None`` (default) reproduz a mecânica de hoje BYTE-A-BYTE:
    * AGIR — there is an entry at the pullback now;
    * CAIXA — downtrend / no setup: cash IS the active position (filtro do método);
    * AGUARDAR — a valid pullback/breakout is still forming.
    """
    if drop_cls == "fraqueza":
        return "CAIXA"
    if acao == "AGIR":
        return "AGIR"
    if trend == "baixa":
        return "AGUARDAR" if drop_cls == "liquidacao_saudavel" else "CAIXA"
    return "AGUARDAR"


def _decide(r: dict, drop_cls: str | None = None) -> dict:
    """Do read de EMA para o veredito do método: agir/aguardar, entrada e PESO.

    A natureza da queda (``drop_cls``) manda ANTES da pilha de médias: ``fraqueza``
    zera a entrada (caixa) e ``liquidacao_saudavel`` numa tendência de baixa/transição
    lê o recuo à média que sobe como comprável (posição inicial no toque, aguardar o
    toque senão). ``drop_cls=None`` (default) reproduz a mecânica de hoje BYTE-A-BYTE.

    Peso relativo sempre FRACIONADO (o método nunca entra 100% de uma vez) e
    responde em termos relativos — cheia / meia / inicial / caixa — não em %
    absoluto inventado.
    """
    trend, at_media, extended, below = r["trend"], r["at_media"], r["extended"], r["below"]
    e8, e21 = r["e8"], r["e21"]

    # Natureza da queda ANTES das médias (fonte única). Nunca rebaixa uma leitura de
    # alta já válida — só reenquadra a baixa/transição da liquidação e veta a fraqueza.
    if drop_cls == "fraqueza":
        return {
            "acao": "AGUARDAR",
            "entrada": f"estrutura rompida (fraqueza) — sem entrada; caixa até um novo setup à média (EMA 21 {_fmt(e21)})",
            "peso": "caixa",
            "peso_racional": "filtro do método: estrutura rompida, sem entrada contra o gráfico quebrado — caixa é a posição",
        }
    if drop_cls == "liquidacao_saudavel" and trend in ("baixa", "transicao"):
        if at_media:
            return {
                "acao": "AGIR",
                "entrada": f"liquidação de longs — recuo comprável na média (EMA 8 {_fmt(e8)} · EMA 21 {_fmt(e21)}); é o ponto de entrada",
                "peso": "posição inicial",
                "peso_racional": "recuo comprável numa liquidação saudável: fração inicial na média que sobe, com espaço para somar — nunca 100% de uma vez",
            }
        return {
            "acao": "AGUARDAR",
            "entrada": f"liquidação de longs — aguardar o toque na EMA 21 ({_fmt(e21)}) que sobe antes de montar (recuo comprável, ainda sem toque)",
            "peso": "caixa",
            "peso_racional": "recuo comprável, mas o preço ainda não tocou a média — caixa até o ponto de entrada",
        }

    if trend == "alta":
        if at_media:
            return {
                "acao": "AGIR",
                "entrada": f"preço recuou até a média agora (EMA 8 {_fmt(e8)} · EMA 21 {_fmt(e21)}) — é o ponto de entrada no recuo",
                "peso": "meia posição",
                "peso_racional": "monta fracionado no toque da média, com espaço para adicionar se ceder à EMA seguinte — nunca 100% de uma vez",
            }
        if extended:
            return {
                "acao": "AGUARDAR",
                "entrada": f"esticado acima da média — aguardar recuo à EMA 8 ({_fmt(e8)}) ou EMA 21 ({_fmt(e21)})",
                "peso": "caixa",
                "peso_racional": "sem entrada no esticado; posição inicial só no recuo à média",
            }
        return {
            "acao": "AGIR",
            "entrada": f"tendência de alta, preço entre as médias — recuo à EMA 21 ({_fmt(e21)}) é a referência de entrada",
            "peso": "posição inicial",
            "peso_racional": "confirmação parcial: só uma fração inicial, aguardando recuo mais limpo à média",
        }

    if trend == "transicao":
        if at_media and not below:
            return {
                "acao": "AGIR",
                "entrada": f"médias entrelaçadas, preço na EMA 21 ({_fmt(e21)}) — entrada tentativa no recuo",
                "peso": "posição inicial",
                "peso_racional": "sem alinhamento pleno de médias: só inicial, com confirmação antes de somar",
            }
        return {
            "acao": "AGUARDAR",
            "entrada": f"médias sem alinhamento — aguardar recuo à EMA 21 ({_fmt(e21)}) e confirmação acima da EMA 8 ({_fmt(e8)})",
            "peso": "caixa",
            "peso_racional": "sem tendência definida, caixa até o gatilho",
        }

    # baixa
    return {
        "acao": "AGUARDAR",
        "entrada": f"tendência de baixa (preço sob a EMA 21 {_fmt(e21)}) — não comprar contra a tendência sem confirmação",
        "peso": "caixa",
        "peso_racional": "filtro do método: sem entrada contra médias invertidas; caixa é a posição",
    }


def _saida(plan: dict, read: dict) -> str:
    """Saída antes da reversão: topo anterior (realização) quando existe; senão,
    exaustão/liquidação — 'pega a maior parte e sai antes de reverter', sem alvo
    fixo inventado."""
    realize = _fmt_zone((plan or {}).get("realize_zone"))
    if realize:
        return f"realizar na resistência acima ({realize}) — pegar a maior parte e sair antes da reversão"
    return ("sem topo anterior acima (preço em ar de máxima) — realizar por exaustão/"
            "liquidação, não por alvo fixo ('pega a maior parte e sai antes de reverter')")


def _drop_nature(symbol: str, curr_date: str, asset_type: str) -> dict | None:
    """Classifica a natureza da queda ANTES da decisão (fonte única do módulo).
    Fail-open → None. Seam isolado para monkeypatch nos testes."""
    try:
        from tradingagents.agents.utils.drop_nature import classify_drop_nature_safe

        return classify_drop_nature_safe(symbol, curr_date, asset_type)
    except Exception as exc:  # noqa: BLE001 — enriquecimento nunca quebra o relatório
        logger.info("drop-nature classify failed for %s: %s", symbol, exc)
        return None


def _render_drop_nature(res: dict | None, estado: str | None) -> str | None:
    """Texto da 'natureza da queda' a partir da classificação JÁ FEITA (DEPOIS que o
    Estado já derivou dela). Sem re-leitura. Fail-open → None. Seam de monkeypatch."""
    try:
        from tradingagents.agents.utils.drop_nature import render_drop_nature_line

        return render_drop_nature_line(res, estado)
    except Exception as exc:  # noqa: BLE001 — enriquecimento nunca quebra o relatório
        logger.info("drop-nature render failed: %s", exc)
        return None


def _sell_breakdown_15m(symbol: str, curr_date: str) -> bool:
    """True quando o 15m tem um 1-2-3 de VENDA acionado — o sinal mais rápido de que
    o recuo virou ruptura (mitigação 1 do caveat). Fail-open → False: na dúvida NÃO
    veta (não inventa um rompimento que a fonte não confirma)."""
    try:
        plan = build_actionable_plan_dict(symbol, curr_date, _FINE_FRAME)
    except Exception:  # noqa: BLE001
        return False
    pat = (plan or {}).get("pattern") or {}
    return pat.get("direction") == "venda" and pat.get("state") == "acionado"


def _liquidation_veto(decision: dict, drop_cls: str | None, sell_15m: bool) -> tuple[dict, bool]:
    """Mitigação 1: um 1-2-3 de venda no 15m impede a liquidação de promover a leitura
    além de AGUARDAR (não vira AGIR). Devolve ``(decision, vetado)`` — não muta a
    entrada. Só age em ``liquidacao_saudavel`` que ia AGIR; o resto passa intacto."""
    if drop_cls == "liquidacao_saudavel" and decision.get("acao") == "AGIR" and sell_15m:
        capped = {
            **decision,
            "acao": "AGUARDAR",
            "peso": "caixa",
            "peso_racional": "1-2-3 de venda no 15m — o recuo virou ruptura; "
                             "a liquidação não promove a entrada além de aguardar",
        }
        return capped, True
    return decision, False


_PAT_DIR_PT = {"compra": "de compra", "venda": "de venda"}
_PAT_STATE_PT = {
    "acionado": "acionado",
    "rompeu_retracou": "rompeu e retraçou (não confirmado)",
    "formando": "em formação",
}


def _pattern_line(plan: dict | None, frame_label: str) -> str | None:
    """Gatilho 1-2-3 do frame (o outro pilar do método além do recuo à EMA 8/21),
    a partir do plano já computado. ``None`` quando não há padrão detectado."""
    pat = (plan or {}).get("pattern")
    if not pat:
        return None
    direction = _PAT_DIR_PT.get(pat.get("direction"), pat.get("direction") or "")
    state = _PAT_STATE_PT.get(pat.get("state"), pat.get("state") or "")
    trigger = pat.get("trigger")
    verb = "perda de" if pat.get("direction") == "venda" else "rompimento de"
    trig_txt = f"{verb} {_fmt(trigger)}" if trigger is not None else "gatilho sem nível"
    return f"**Gatilho 1-2-3 {direction} ({frame_label}):** {trig_txt} — {state}."


def _fine_timing(symbol: str, curr_date: str) -> str | None:
    """Linha de timing fino no 15m (cripto ou ação — a fonte keyless intradiária
    existe pros dois agora): estado do setup + gatilho 1-2-3 do 15m, o timing do
    método. Fail-open -> None quando não há candle."""
    try:
        plan = build_actionable_plan_dict(symbol, curr_date, _FINE_FRAME)
    except Exception:  # noqa: BLE001
        return None
    state = (plan or {}).get("setup_state")
    labels = {
        "ativo": "preço já na média (recuo concluído) — janela de entrada aberta",
        "aguardar_pullback": "aguardando recuo à média para o gatilho fino",
        "aguardar_rompimento": "aguardando rompimento do 1-2-3",
        "sem_setup": "sem gatilho fino no momento",
        "sem_dado": None,
        "intradiario_indisponivel": None,
    }
    txt = labels.get(state)
    if not txt:
        return None
    line = f"**Timing fino (15m):** {txt}"
    pat_line = _pattern_line(plan, "15m")
    if pat_line:
        line += "\n" + pat_line
    return line


def _estado_note(drop_cls: str | None, estado: str) -> str:
    """Nota curta que explica a DERIVAÇÃO do Estado a partir da natureza da queda
    (quando ela mandou), pra ler de cima pra baixo dar a mesma conclusão do enum."""
    if drop_cls == "fraqueza":
        return ("Deriva da natureza da queda (fraqueza: estrutura rompida) — "
                "não da pilha curta de médias.")
    if drop_cls == "liquidacao_saudavel" and estado in ("AGIR", "AGUARDAR"):
        return ("Deriva da natureza da queda (liquidação saudável: recuo comprável) — "
                "não da pilha curta de médias.")
    return ""


def build_erick_method_section(
    symbol: str, curr_date: str, asset_type: str, drop: dict | None = None
) -> str:
    """Seção markdown pt-BR do método Erick — determinística e ancorada em dado.

    O método lê no 4h (frame de swing) + timing fino no 15m para QUALQUER ativo —
    cripto no candle da exchange, ação no intradiário keyless do yfinance. Se a
    fonte não tem candle pro símbolo/data, cai no diário e declara o degradê. Nunca
    inventa nível: sem candle, diz que a leitura intradiária está indisponível.

    ``drop`` é a classificação da natureza da queda JÁ FEITA pelo analista (fonte
    única, compartilhada com o juiz). ``None`` → classifica aqui uma vez.
    """
    frame = _SWING_FRAME
    chart = build_price_chart(symbol, curr_date, timeframe=frame)
    read = _ema_read(chart)

    # Fonte intradiária sem candle pro símbolo/data (ação fora da janela do yfinance,
    # ou feed cripto fora do ar): cai no diário, declarando o degradê.
    degraded_note = ""
    if read is None:
        frame = _FALLBACK_FRAME
        chart = build_price_chart(symbol, curr_date, timeframe=frame)
        read = _ema_read(chart)
        degraded_note = ("\n\n> Fonte intradiária indisponível agora — leitura caiu no "
                         "diário. Nenhuma barra inventada; o método pede o 4h/15m.")

    frame_label = _FRAME_LABEL.get(frame, frame)
    head = "## 🧭 Método Erick — leitura do setup"

    if read is None:
        return (
            f"{head}\n\n"
            f"**Timeframe da leitura:** {frame_label}\n\n"
            f"Sem candle suficiente para a leitura de EMA neste frame — nada inventado."
        )

    # Natureza da queda classificada ANTES da decisão (fonte única). Se o analista
    # não passou a classificação, classifica aqui — uma vez.
    if drop is None:
        drop = _drop_nature(symbol, curr_date, asset_type)
    drop_cls = (drop or {}).get("classification")

    decision = _decide(read, drop_cls)
    # Mitigação 1: veto no timing fino — 1-2-3 de venda no 15m trava a promoção da
    # liquidação além de AGUARDAR (o recuo virou ruptura). Fail-open → não veta.
    sell_15m = _sell_breakdown_15m(symbol, curr_date)
    decision, vetoed = _liquidation_veto(decision, drop_cls, sell_15m)
    # ONE canonical state (item 6b), agora derivado da natureza da queda.
    decision["estado"] = _estado(decision["acao"], read["trend"], drop_cls)

    # Mitigação 2: LOG da taxa de flip — quando a liquidação promove o Estado que a
    # mecânica leria como CAIXA (medir num backtest se o gate filtra algo).
    mech = _decide(read, None)
    mech_estado = _estado(mech["acao"], read["trend"], None)
    if drop_cls == "liquidacao_saudavel" and mech_estado == "CAIXA" and decision["estado"] != "CAIXA":
        logger.info(
            "erick-drop-flip %s %s: estado %s->%s por liquidacao_saudavel (veto15m=%s)",
            symbol, curr_date, mech_estado, decision["estado"], vetoed,
        )

    swing_plan = build_actionable_plan_dict(symbol, curr_date, frame)
    saida = _saida(swing_plan, read)
    trend_pt = _TREND_PT.get(read["trend"], read["trend"])
    caixa = decision["estado"] == "CAIXA"
    # Texto da natureza da queda a partir da classificação já feita (sem re-leitura):
    # o Estado já veio dela, o texto só explica de onde. Vem ANTES do Estado abaixo.
    drop_line = _render_drop_nature(drop, decision["estado"])
    estado_note = _estado_note(drop_cls, decision["estado"])
    estado_txt = ("**Estado (Método Erick):** "
                  f"{decision['estado']} — estado único do método neste run; a "
                  "leitura abaixo deriva dele (sem veredito paralelo).")
    if estado_note:
        estado_txt += " " + estado_note

    lines = [
        head,
        "",
        f"**Timeframe da leitura:** {frame_label} — o método opera no 15m/4h; "
        "diário/semanal dão a tendência de fundo.",
        "",
        f"**Regime (médias):** {trend_pt} — preço {_fmt(read['close'])}, "
        f"EMA 8 {_fmt(read['e8'])} · EMA 21 {_fmt(read['e21'])} · EMA 50 {_fmt(read['e50'])}.",
    ]
    # 🩸 Natureza da queda ANTES do Estado: ler de cima pra baixo dá a MESMA conclusão
    # que o enum (a classificação é a fonte de onde o Estado veio, não uma re-leitura
    # anexada no fim que o contradiz).
    if drop_line:
        lines += ["", drop_line]
    lines += [
        "",
        estado_txt,
        f"**Entrada (recuo à média):** {decision['entrada']}.",
        f"**Saída (antes da reversão):** {saida}.",
        f"**Peso relativo do trade:** {decision['peso']} — {decision['peso_racional']}.",
    ]

    # Gatilho 1-2-3 do frame de swing (o outro pilar do método além do recuo à EMA
    # 8/21), agora DENTRO da leitura do método — não só na seção de mercado.
    swing_pat = _pattern_line(swing_plan, _COMPACT_FRAME.get(frame, frame))
    if swing_pat:
        lines.append(swing_pat)

    fine = _fine_timing(symbol, curr_date)
    if fine:
        lines += ["", fine]

    lines += [
        "",
        "**Tático × estrutural:** esta é uma leitura TÁTICA de curto prazo "
        "(intradiário); a tese estrutural de longo prazo é outra decisão, separada.",
    ]
    if caixa:
        lines.append("**Caixa é posição:** ficar de fora aqui é decisão ativa — "
                     "caixa elevado por escolha, aguardando o ponto.")

    return "\n".join(lines) + degraded_note


def ensure_erick_method_coverage(
    report: str, symbol: str, curr_date: str, asset_type: str, drop: dict | None = None
) -> str:
    """Anexa a seção determinística do método ao relatório do analista `erick`.

    Espelha os outros guardas de cobertura (multi-timeframe, derivativos,
    price-structure): a prosa do LLM molda; esta seção garante o núcleo
    operável (timeframe, recuo à média, saída, peso). ``drop`` propaga a
    classificação já feita (fonte única). Fail-open: qualquer erro devolve o
    relatório intacto.
    """
    try:
        section = build_erick_method_section(symbol, curr_date, asset_type, drop=drop)
    except Exception as exc:  # noqa: BLE001 — enriquecimento nunca quebra o relatório
        logger.warning("erick-method coverage failed for %s: %s", symbol, exc)
        return report
    base = (report or "").rstrip()
    return f"{base}\n\n{section}\n" if base else section + "\n"

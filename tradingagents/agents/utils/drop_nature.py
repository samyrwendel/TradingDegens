"""Natureza da queda — liquidação de longs (saudável) × fraqueza (evitar).

A lacuna nº 3 do confronto real × sintético: o humano lê uma queda-pós-balanço num
regime de fundo intacto como **liquidação de longs** (combustível — segue
comprador no recuo à média); o sintético mecânico lê "EMAs invertidas → CAIXA,
evitar". Não é viés fixo de alta — é uma REGRA DE LEITURA aplicada aos DADOS:

* **liquidação saudável** quando, ao mesmo tempo — (a) a tendência de fundo do
  ativo está intacta (MMS200 subindo, preço testando/ acima dela), (b) a queda é um
  RECUO a uma média que sobe (região de compra ativa), não um rompimento de baixa
  confirmado, e (c) o ÂNCORA (ex.: NVDA) está em alta e BATEU o último balanço — o
  catalisador que arrasta os correlacionados para cima. Aí a queda é combustível:
  segue comprador no recuo.
* **fraqueza** quando a estrutura ROMPE (1-2-3 de venda acionado, ou MMS200 caindo
  com preço abaixo) e NÃO há catalisador do âncora (não bateu). Aí é para evitar.
* **indefinido** quando os sinais se misturam, não há queda relevante, ou falta
  dado — estado honesto, nunca um chute bullish.

Determinístico e ancorado no MESMO dado date-guarded do resto do motor
(``build_price_chart``/``detect_price_structure`` diário + resultado reportado do
âncora via Finnhub). Fail-open: qualquer erro devolve "indefinido", nunca quebra
o relatório nem inventa sinal.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Queda mínima (vs máxima recente) para haver uma "queda" a classificar.
_DROP_MIN_PCT = 3.0
# Janela (barras) da máxima recente e da inclinação da MMS200.
_RECENT_BARS = 20
_SLOPE_BARS = 10
# Tolerância "testando a MMS200 por baixo" — preço até 7% abaixo de uma 200 que
# sobe ainda conta como tendência de fundo intacta (recuo profundo, não ruptura).
_MA200_TEST_TOL = 0.07
# "Bateu recente" do âncora: dentro de ~um trimestre A PARTIR DA DIVULGAÇÃO. O
# finnhub_earnings já resolve a data real de divulgação (_fetch_announce_date) e
# devolve days_since medido DELA quando o calendário a tem; só cai no fim de período
# fiscal (~8 semanas antes) no fallback sem calendário. 100 dias ≈ um trimestre desde
# o anúncio — a janela do catalisador ativo, não a folga do fim de período.
_ANCHOR_BEAT_MAX_DAYS = 100


def _last(seq):
    for v in reversed(seq or []):
        if v is not None:
            return v
    return None


def _nth_last(seq, n):
    """n-ésimo valor não-nulo a partir do fim (0 = último), ou None."""
    vals = [v for v in (seq or []) if v is not None]
    if len(vals) <= n:
        return None
    return vals[-1 - n]


def _daily_snapshot(symbol: str, curr_date: str) -> dict | None:
    """Leitura diária date-guarded do ``symbol`` para a natureza da queda.

    Reusa ``build_price_chart``/``detect_price_structure`` (mesma série cacheada).
    ``None`` quando não há candle suficiente."""
    from tradingagents.dataflows.price_structure import (
        build_price_chart,
        detect_price_structure,
    )

    ch = build_price_chart(symbol, curr_date, timeframe="1d")
    candles = ch.get("candles") or []
    if len(candles) < _RECENT_BARS + 1:
        return None
    closes = [c.get("c") for c in candles if c.get("c") is not None]
    if len(closes) < _RECENT_BARS + 1:
        return None
    price = closes[-1]
    recent_high = max(closes[-_RECENT_BARS:])
    dd_pct = (price / recent_high - 1.0) * 100.0 if recent_high else 0.0

    ma = ch.get("ma") or {}
    ma50 = _last(ma.get("50"))
    ma200 = _last(ma.get("200"))
    ma200_prev = _nth_last(ma.get("200"), _SLOPE_BARS)
    ma200_rising = ma200 is not None and ma200_prev is not None and ma200 > ma200_prev

    ema = ch.get("ema") or {}
    e8, e21, e50 = _last(ema.get("8")), _last(ema.get("21")), _last(ema.get("50"))
    if e8 is not None and e21 is not None and e50 is not None and e8 > e21 > e50:
        trend = "alta"
    elif e8 is not None and e21 is not None and e50 is not None and e8 < e21 < e50:
        trend = "baixa"
    else:
        trend = "transicao"

    struct = detect_price_structure(symbol, curr_date, "1d", "padrao")
    active_rising = None
    if struct.active_region is not None:
        # active_region só é emitida sobre uma média em ALTA (o detector já exige
        # rising+was_above), então basta a presença dela.
        active_rising = struct.active_region.ma_label
    pat_dir = struct.pattern.direction if struct.pattern else None
    pat_state = struct.pattern.state if struct.pattern else None

    return {
        "price": price,
        "recent_high": recent_high,
        "dd_pct": dd_pct,
        "ma50": ma50,
        "ma200": ma200,
        "ma200_rising": ma200_rising,
        "above_ma50": ma50 is not None and price > ma50,
        "trend": trend,
        "active_rising_label": active_rising,
        "pattern_dir": pat_dir,
        "pattern_state": pat_state,
    }


def _anchor_beat_recent(symbol: str, curr_date: str) -> tuple[bool, dict | None]:
    """(bateu recente?, evento) do âncora, via Finnhub. Fail-open → (False, None)."""
    try:
        from tradingagents.dataflows.finnhub_earnings import get_reported_earnings

        ev = get_reported_earnings(symbol, curr_date)
    except Exception as exc:  # noqa: BLE001
        logger.info("anchor earnings unavailable for %s: %s", symbol, exc)
        return False, None
    if not ev or not ev.get("beat"):
        return False, ev
    days = ev.get("days_since")
    recent = days is not None and 0 <= days <= _ANCHOR_BEAT_MAX_DAYS
    return bool(recent), ev


def classify_drop_nature(
    symbol: str, curr_date: str, asset_type: str = "stock", anchor: str | None = None
) -> dict:
    """Classifica a queda atual do ``symbol`` como liquidação × fraqueza × indefinido.

    Determinístico e fail-open. Retorna ``{"classification","reasons","evidence"}``;
    ``classification`` ∈ ``{"liquidacao_saudavel","fraqueza","indefinido"}``.
    Cripto não tem earnings do âncora do jeito do método → só usa estrutura (o
    catalisador de balanço fica ausente, e a classificação tende a "indefinido"
    sem o âncora que bateu).
    """
    from tradingagents.dataflows.correlation import default_anchor

    evidence: dict = {"symbol": symbol.upper()}
    try:
        snap = _daily_snapshot(symbol, curr_date)
    except Exception as exc:  # noqa: BLE001 — nunca quebra o relatório
        logger.info("drop-nature snapshot failed for %s: %s", symbol, exc)
        snap = None
    if snap is None:
        return {
            "classification": "indefinido",
            "reasons": ["sem série diária suficiente para ler a natureza da queda"],
            "evidence": evidence,
        }
    evidence["asset"] = snap

    has_drop = snap["dd_pct"] <= -_DROP_MIN_PCT
    ma200 = snap["ma200"]
    long_uptrend_intact = bool(
        snap["ma200_rising"]
        and ma200 is not None
        and snap["price"] > ma200 * (1 - _MA200_TEST_TOL)
    )
    pullback_to_rising_avg = snap["active_rising_label"] is not None
    sell_breakdown = snap["pattern_dir"] == "venda" and snap["pattern_state"] == "acionado"
    struct_down = bool(
        (not snap["ma200_rising"]) and ma200 is not None and snap["price"] < ma200
    )

    anchor_name = (anchor or default_anchor(asset_type)).upper()
    beat_recent, ev = _anchor_beat_recent(anchor_name, curr_date)
    evidence["anchor"] = {"name": anchor_name, "beat_recent": beat_recent, "earnings": ev}

    # Regime do âncora (tendência de fundo do líder do setor).
    anchor_up = False
    if symbol.upper() != anchor_name:
        try:
            asnap = _daily_snapshot(anchor_name, curr_date)
            if asnap is not None:
                anchor_up = asnap["trend"] == "alta" or asnap["above_ma50"]
                evidence["anchor"]["trend"] = asnap["trend"]
        except Exception as exc:  # noqa: BLE001
            logger.info("anchor snapshot failed for %s: %s", anchor_name, exc)
    else:
        # O próprio ativo é o âncora: seu regime é o dele mesmo.
        anchor_up = snap["trend"] == "alta" or snap["above_ma50"]

    reasons: list[str] = []

    if not has_drop:
        reasons.append(
            f"sem queda relevante agora (recuo de apenas {snap['dd_pct']:.1f}% da máxima recente)"
        )
        return {"classification": "indefinido", "reasons": reasons, "evidence": evidence}

    # Liquidação exige EVIDÊNCIA positiva conjunta (não é viés de alta). Fraqueza é a
    # estrutura ROMPIDA do próprio ativo — o âncora ter batido não resgata um gráfico
    # quebrado, então não se exige "âncora não bateu" aqui (são mutuamente exclusivas:
    # liquidação pede MMS200 subindo + sem venda acionada).
    liquidacao = (
        has_drop
        and pullback_to_rising_avg
        and long_uptrend_intact
        and not sell_breakdown
        and beat_recent
        and anchor_up
    )
    fraqueza = has_drop and (sell_breakdown or struct_down)

    if liquidacao:
        reasons = [
            f"queda de {snap['dd_pct']:.1f}% recuou a uma média que sobe "
            f"({snap['active_rising_label']}) — recuo, não rompimento",
            "tendência de fundo intacta (MMS200 subindo, preço testando/ acima dela)",
            f"âncora {anchor_name} em alta e bateu o último balanço"
            + (f" (+{ev['surprise_pct']:.1f}% de surpresa)".replace(".", ",") if ev and ev.get("surprise_pct") is not None else ""),
        ]
        return {"classification": "liquidacao_saudavel", "reasons": reasons, "evidence": evidence}

    if fraqueza:
        why = "1-2-3 de venda acionado" if sell_breakdown else "MMS200 caindo com preço abaixo dela"
        reasons = [f"queda de {snap['dd_pct']:.1f}% com estrutura rompida ({why})"]
        if beat_recent:
            reasons.append(
                f"o âncora {anchor_name} bateu, mas a estrutura do próprio ativo rompeu — "
                "a força do setor não resgata o gráfico quebrado"
            )
        else:
            reasons.append(f"sem catalisador do âncora {anchor_name} (não bateu / indisponível)")
        return {"classification": "fraqueza", "reasons": reasons, "evidence": evidence}

    # Sinais misturados: diz o que falta para não ser um chute.
    missing = []
    if not long_uptrend_intact:
        missing.append("tendência de fundo (MMS200) não claramente intacta")
    if not pullback_to_rising_avg:
        missing.append("preço não está num recuo a uma média que sobe")
    if not beat_recent:
        missing.append("sem beat recente confirmado do âncora")
    if sell_breakdown:
        missing.append("mas há 1-2-3 de venda acionado")
    reasons = ["sinais mistos — nem liquidação saudável nem fraqueza clara"] + missing
    return {"classification": "indefinido", "reasons": reasons, "evidence": evidence}


def classify_drop_nature_safe(
    symbol: str, curr_date: str, asset_type: str = "stock", anchor: str | None = None
) -> dict | None:
    """Classify blindado — o ponto de entrada ÚNICO de quem precisa da classificação
    ANTES da decisão. Devolve o dict de :func:`classify_drop_nature` (pode ser
    ``indefinido``) ou ``None`` só quando a própria classificação estoura. Nunca
    levanta: um erro vira ``None`` e o chamador segue com a mecânica de hoje."""
    try:
        return classify_drop_nature(symbol, curr_date, asset_type, anchor)
    except Exception as exc:  # noqa: BLE001 — jamais quebra quem depende da leitura
        logger.info("drop-nature classify_safe failed for %s: %s", symbol, exc)
        return None


# --------------------------------------------------------------- markdown ------
_HEAD = "**🩸 Natureza da queda (liquidação × fraqueza):**"


def render_drop_nature_line(res: dict | None, estado: str | None = None) -> str | None:
    """Texto pt-BR da natureza da queda a partir de uma classificação JÁ FEITA.

    SEM re-leitura/correção: quando esta linha aparece no relatório, o **Estado** já
    foi computado A PARTIR desta mesma classificação (:func:`classify_drop_nature`),
    então o texto só EXPLICA de onde o Estado veio — nunca contradiz nem sobrescreve.
    ``None`` quando não há queda a classificar (indefinido por ausência de queda) ou
    quando não há classificação — não polui o relatório."""
    if not res:
        return None
    cls = res.get("classification")
    reasons = res.get("reasons") or []
    # Sem queda relevante → não anexa (evita ruído quando não há o que classificar).
    if cls == "indefinido" and reasons and reasons[0].startswith("sem queda relevante"):
        return None

    why = "; ".join(reasons)
    if cls == "liquidacao_saudavel":
        tail = (
            " → **liquidação de longs (saudável): segue comprador no recuo à média**."
        )
        if estado in ("AGIR", "AGUARDAR"):
            tail += (
                f" As EMAs curtas do 4h invertem numa liquidação; por isso o Estado "
                f"acima é {estado} e não CAIXA — deriva desta classificação, não da "
                "pilha curta."
            )
        return f"{_HEAD} {why}{tail}"
    if cls == "fraqueza":
        return (
            f"{_HEAD} {why} → **fraqueza: evitar** — caixa é a posição, e o Estado "
            "acima é CAIXA por causa disto."
        )
    return f"{_HEAD} {why} → leitura indefinida (sem chute)."


def build_drop_nature_line(
    symbol: str, curr_date: str, asset_type: str = "stock", anchor: str | None = None,
    estado: str | None = None,
) -> str | None:
    """Conveniência: classifica (:func:`classify_drop_nature_safe`) e renderiza numa
    chamada. O motor NÃO usa mais este caminho — ele classifica ANTES (fonte única) e
    renderiza DEPOIS via :func:`render_drop_nature_line`. Mantido para chamadores que
    só querem o bloco sem separar as duas etapas."""
    return render_drop_nature_line(
        classify_drop_nature_safe(symbol, curr_date, asset_type, anchor), estado
    )


# --------------------------------------------------- guardrail de coerência ----
# A prosa do LLM (item 10 condicionado) às vezes escapa e contradiz o enum
# determinístico. O guardrail remove da prosa as frases que negam a classificação —
# a seção «🩸 Natureza da queda» é a fonte única, então a prosa nunca a contradiz.
_STRIP_CONTRADICTIONS = True
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
# Palavra de "queda" — precisa estar na frase pra ela ser SOBRE a queda em questão.
_DROP_WORDS = ("queda", "recuo", "correção", "correcao", "selloff", "sell-off", "pullback")
# Termos que CONTRADIZEM cada classe (a frase precisa ter uma drop_word + um destes).
_CONTRA_WORDS = {
    "liquidacao_saudavel": (
        "evitar", "não comprar", "nao comprar", "downtrend", "tendência de baixa",
        "tendencia de baixa", "fraqueza", "ficar de fora", "sair do ativo",
    ),
    "fraqueza": (
        "liquidação de longs", "liquidacao de longs", "oportunidade de compra",
        "comprável", "compravel", "combustível", "combustivel",
    ),
}
_CLASS_PT = {"liquidacao_saudavel": "liquidação saudável", "fraqueza": "fraqueza"}


def _contradicts(sent: str, contra: tuple[str, ...]) -> bool:
    """Uma frase contradiz a classe se fala DA queda (drop_word) E carrega um termo
    contrário. Casamento lexical simples — limite conhecido: pode pegar uma frase que
    NEGA a contradição ("não é fraqueza"); custo aceito (o enum é a fonte única)."""
    low = sent.lower()
    if not any(w in low for w in _DROP_WORDS):
        return False
    return any(c in low for c in contra)


def enforce_drop_nature_coherence(
    text: str | None, res: dict | None
) -> tuple[str | None, dict]:
    """Remove da prosa do LLM as frases que contradizem a classificação da queda.

    Só age em ``liquidacao_saudavel``/``fraqueza`` (``indefinido``/ausente não toca:
    não há o que contradizer). Com ``_STRIP_CONTRADICTIONS`` remove as frases e anexa
    uma nota; senão só sinaliza. Devolve ``(texto, flags)`` — ``flags`` conta o que
    foi removido/sinalizado e a classe, pra o campo estruturado e o juiz."""
    flags = {"classification": (res or {}).get("classification"), "removed": 0,
             "flagged": 0, "sentences": []}
    if not text or not res:
        return text, flags
    contra = _CONTRA_WORDS.get(res.get("classification"))
    if not contra:
        return text, flags  # indefinido / indisponível → prosa intacta

    offenders = [s for s in _SENT_SPLIT.split(text) if _contradicts(s, contra)]
    if not offenders:
        return text, flags
    flags["sentences"] = offenders
    if not _STRIP_CONTRADICTIONS:
        flags["flagged"] = len(offenders)
        return text, flags

    kept = [s for s in _SENT_SPLIT.split(text) if not _contradicts(s, contra)]
    flags["removed"] = len(offenders)
    cls_pt = _CLASS_PT.get(res.get("classification"), res.get("classification"))
    note = (
        f"\n\n> ⚠️ Coerência: {len(offenders)} trecho(s) contradiziam a classificação "
        f"({cls_pt}) e foram removidos. A seção «🩸 Natureza da queda» é a fonte única."
    )
    return (" ".join(s for s in kept if s).strip() + note), flags


def drop_nature_field(res: dict | None, coherence_flags: dict | None = None) -> dict:
    """Campo estruturado da natureza da queda (o que o juiz/UI leem — não a prosa).

    Só ``{classification, reasons, anchor:{name,beat_recent,trend}, coherence_flags}``
    — sem os snapshots crus (grandes). ``None`` → ``classification="indisponivel"``."""
    if not res:
        return {
            "classification": "indisponivel", "reasons": [], "anchor": {},
            "coherence_flags": coherence_flags or {},
        }
    anchor = (res.get("evidence") or {}).get("anchor") or {}
    return {
        "classification": res.get("classification"),
        "reasons": list(res.get("reasons") or []),
        "anchor": {
            "name": anchor.get("name"),
            "beat_recent": anchor.get("beat_recent"),
            "trend": anchor.get("trend"),
        },
        "coherence_flags": coherence_flags or {},
    }

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

logger = logging.getLogger(__name__)

# Queda mínima (vs máxima recente) para haver uma "queda" a classificar.
_DROP_MIN_PCT = 3.0
# Janela (barras) da máxima recente e da inclinação da MMS200.
_RECENT_BARS = 20
_SLOPE_BARS = 10
# Tolerância "testando a MMS200 por baixo" — preço até 7% abaixo de uma 200 que
# sobe ainda conta como tendência de fundo intacta (recuo profundo, não ruptura).
_MA200_TEST_TOL = 0.07
# "Bateu recente" do âncora: dentro de ~um trimestre (a data do Finnhub é o fim do
# período fiscal, ~8 semanas antes da divulgação, então a folga é generosa).
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


# --------------------------------------------------------------- markdown ------
_HEAD = "**🩸 Natureza da queda (liquidação × fraqueza):**"


def build_drop_nature_line(
    symbol: str, curr_date: str, asset_type: str = "stock", anchor: str | None = None,
    mechanical_estado: str | None = None,
) -> str | None:
    """Bloco pt-BR da natureza da queda para a seção do método. ``None`` quando não
    há queda a classificar (indefinido por ausência de queda) — não polui o relatório.

    Quando classifica LIQUIDAÇÃO SAUDÁVEL e o Estado mecânico é CAIXA/AGUARDAR (as
    EMAs curtas do 4h invertem na queda), acrescenta a RE-LEITURA: é recuo comprável,
    não downtrend a evitar — sem sobrescrever o enum determinístico, mas dando a regra."""
    try:
        res = classify_drop_nature(symbol, curr_date, asset_type, anchor)
    except Exception as exc:  # noqa: BLE001
        logger.info("drop-nature line failed for %s: %s", symbol, exc)
        return None

    cls = res["classification"]
    reasons = res.get("reasons") or []
    # Sem queda relevante → não anexa (evita ruído quando não há o que classificar).
    if cls == "indefinido" and reasons and reasons[0].startswith("sem queda relevante"):
        return None

    why = "; ".join(reasons)
    if cls == "liquidacao_saudavel":
        tail = (
            " → **liquidação de longs (saudável): segue comprador no recuo à média**."
        )
        if mechanical_estado in ("CAIXA", "AGUARDAR"):
            tail += (
                " Re-leitura: o Estado mecânico acima vem das EMAs curtas do 4h, que "
                "invertem na liquidação — pelo método isto é recuo COMPRÁVEL (some no "
                "recuo à média), não downtrend a evitar."
            )
        return f"{_HEAD} {why}{tail}"
    if cls == "fraqueza":
        return f"{_HEAD} {why} → **fraqueza: evitar** (caixa é a posição)."
    return f"{_HEAD} {why} → leitura indefinida (sem chute)."

"""Calendário de earnings — próxima data de resultado (fonte pública, keyless).

O eixo da análise do Erick é o EVENTO: "o resultado da NVDA sai quarta 26/08".
Sem saber quando é o evento, não dá pra posicionar antes ("evita aumentar antes
do balanço" — regra dele).

Fonte: ``yfinance`` (``Ticker.get_earnings_dates``), pública e sem chave. Se a
fonte cair/instável, declara INDISPONÍVEL — nunca inventa uma data. Passa pelo
cache (DA-058) e respeita o date_guard: a "próxima" data é a primeira ESTRITAMENTE
depois da data de análise (evento futuro conhecido/agendado naquela data), então
um backtest numa data passada só vê o próximo balanço agendado a partir dali, e
lê apenas a DATA (nunca o resultado, que não era conhecido).
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from tradingagents.datacache import cache

logger = logging.getLogger(__name__)

_CATEGORY = "earnings_next"
# Quantas linhas puxar do yfinance (cobre alguns trimestres à frente e atrás).
_LIMIT = 16
# Hora (fuso do papel) a partir da qual o release é "após o fechamento".
_AFTER_CLOSE_HOUR = 16


def _to_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (ValueError, TypeError):
        return None


def _fetch_next_earnings(symbol: str, base: date) -> dict | None:
    """Consulta o yfinance e devolve o próximo earnings > ``base``. None se nada."""
    import yfinance as yf

    tk = yf.Ticker(symbol)
    df = tk.get_earnings_dates(limit=_LIMIT)
    if df is None or getattr(df, "empty", True):
        return None

    best = None  # (date, row)
    for idx, row in df.iterrows():
        ts = _to_date(idx)
        if ts is None or ts <= base:
            continue
        if best is None or ts < best[0]:
            best = (ts, idx, row)

    if best is None:
        return None

    d, ts, row = best
    hour = getattr(ts, "hour", None)
    after_close = hour is not None and hour >= _AFTER_CLOSE_HOUR
    est = row.get("EPS Estimate") if hasattr(row, "get") else None
    try:
        est = float(est)
        if est != est:  # NaN
            est = None
    except (TypeError, ValueError):
        est = None

    return {
        "symbol": symbol.upper(),
        "date": d.isoformat(),
        "after_close": bool(after_close),
        "eps_estimate": est,
    }


def get_next_earnings(symbol: str, curr_date: str) -> dict | None:
    """Próxima data de resultado de ``symbol`` depois de ``curr_date``.

    Retorna ``{"symbol","date","after_close","eps_estimate"}`` ou ``None`` quando
    não há próximo evento conhecido OU a fonte está indisponível — o chamador
    trata ``None`` como "indisponível", jamais inventa data. Cacheado (DA-058) e
    date-guarded.
    """
    # Import tardio do guard: date_guard vive na camada de agents e importá-lo no
    # topo criaria um ciclo (agents.__init__ -> erick_analyst -> earnings_coverage
    # -> este módulo). No momento da CHAMADA a camada de agents já está carregada.
    from tradingagents.agents.utils.date_guard import clamp

    guarded = clamp(curr_date)
    base = _to_date(guarded)
    if base is None:
        base = datetime.now().date()

    k = cache.key(_CATEGORY, symbol.upper(), base.isoformat())
    hit = cache.get(_CATEGORY, k)
    if hit is not None:
        cache.record_hit(_CATEGORY, negative=(hit.get("kind") == "neg"))
        return hit.get("value")

    cache.record_net(_CATEGORY)
    try:
        result = _fetch_next_earnings(symbol, base)
    except Exception as exc:  # noqa: BLE001 — fonte instável degrada a "indisponível"
        logger.warning("earnings source failed for %s: %s", symbol, exc)
        cache.set_neg(_CATEGORY, k, value=None, error={"type": type(exc).__name__, "msg": str(exc)})
        return None

    if result is None:
        # Sem próximo evento conhecido: negativo de TTL curto (a agenda pode surgir).
        cache.set_neg(_CATEGORY, k, value=None)
        return None

    # Data passada de análise -> a "próxima data a partir dali" é fato histórico
    # estável (permanente); análise ao vivo expira no fim do dia.
    permanent = base < datetime.now().date()
    cache.set_ok(_CATEGORY, k, result, permanent)
    return result


def _fmt_event(ev: dict) -> str:
    when = " (após o fechamento)" if ev.get("after_close") else ""
    est = ""
    if ev.get("eps_estimate") is not None:
        est = f", EPS estimado {ev['eps_estimate']:.2f}".replace(".", ",")
    return f"{ev['date']}{when}{est}"


def build_earnings_section(
    symbol: str,
    curr_date: str,
    asset_type: str = "stock",
    anchor: str | None = None,
) -> str:
    """Seção markdown pt-BR: próximo earnings do ativo (e do âncora NVDA).

    Cripto não tem calendário de resultados — retorna vazio (o chamador não anexa).
    Fonte caída → "indisponível", nunca inventa data.
    """
    if asset_type == "crypto":
        return ""

    head = "## 📅 Calendário de earnings (risco de evento)"
    lines = [head, ""]

    ev = get_next_earnings(symbol, curr_date)
    if ev is None:
        lines.append(
            f"- **{symbol.upper()}**: próximo resultado indisponível "
            f"(fonte pública fora do ar ou sem agenda) — nenhuma data inventada."
        )
    else:
        lines.append(f"- **{symbol.upper()}**: próximo resultado em {_fmt_event(ev)}.")

    # O âncora (NVDA) é o eixo do evento na leitura do Erick — mostra sempre, a não
    # ser que o próprio ativo já seja o âncora.
    from .correlation import default_anchor

    anchor_name = (anchor or default_anchor(asset_type)).upper()
    if symbol.upper() != anchor_name:
        ev_a = get_next_earnings(anchor_name, curr_date)
        if ev_a is None:
            lines.append(
                f"- **{anchor_name}** (âncora): próximo resultado indisponível — "
                f"nenhuma data inventada."
            )
        else:
            lines.append(f"- **{anchor_name}** (âncora): próximo resultado em {_fmt_event(ev_a)}.")

    lines.append("")
    lines.append(
        "_Regra do método: evitar aumentar posição antes do balanço; o resultado do "
        "âncora arrasta os correlacionados._"
    )
    return "\n".join(lines)

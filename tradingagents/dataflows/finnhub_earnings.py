"""Resultado REPORTADO de earnings do âncora — o CATALISADOR que o Erick lê.

O `earnings_calendar` (yfinance) responde "quando é o próximo balanço". Mas a
leitura do Erick gira em torno do RESULTADO que já saiu: "a NVDA reportou e
**bateu** → a queda pós-balanço é liquidação de longs, não fraqueza". Esse dado —
reportado × estimado + surpresa — o calendário não dá, e o yfinance
`get_earnings_dates` anda STALE para o âncora (só devolve trimestres antigos). O
Finnhub tem, keyless-ish (uma chave grátis), no endpoint `/stock/earnings`
(histórico de surpresas por trimestre fiscal).

Fonte: Finnhub ``/stock/earnings`` (``actual``/``estimate``/``surprise``/
``surprisePercent`` por ``period`` = fim do trimestre fiscal). Sem
``FINNHUB_API_KEY`` → devolve ``None`` ("indisponível", nunca inventa). Passa pelo
cache (DA-058) e respeita o date_guard.

**Date-guard sem data de anúncio.** O ``/stock/earnings`` traz o FIM do trimestre
(``period``), não a data em que o resultado foi divulgado — e o
``/calendar/earnings`` (que traria a data) é limitado a near-term no plano grátis.
Para NÃO vazar futuro num backtest, um trimestre só entra quando é seguro que já
foi público: ``period + _ANNOUNCE_LAG_DAYS <= curr_date`` (large caps divulgam
dentro de ~8 semanas; a folga é conservadora). Num run AO VIVO (``curr_date`` >=
hoje) não há futuro a vazar, então mostra o resultado mais recente direto. Quando
o ``/calendar/earnings`` devolve a data real de anúncio (near-term), ela é usada e
a guarda vira exata. Nunca é inventada uma data: sem data real, o relatório cita o
trimestre fiscal encerrado, não uma data de divulgação fabricada.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta

import requests

from tradingagents.datacache import cache

logger = logging.getLogger(__name__)

_FINNHUB_BASE = "https://finnhub.io/api/v1"
_TIMEOUT = 12
_CATEGORY = "earnings_reported"

# Folga conservadora entre o fim do trimestre e a divulgação, usada como guarda
# anti-look-ahead quando não há data de anúncio real (large caps divulgam em
# ~4–8 semanas; 55d cobre o típico sem vazar o resultado antes de ser público).
_ANNOUNCE_LAG_DAYS = 55
# Janela (dias) pós-fim-de-trimestre onde procuramos a data real de anúncio no
# /calendar/earnings — um balanço sai dentro deste intervalo depois do período.
_ANNOUNCE_WINDOW_DAYS = 100
# "Bateu recente" para a leitura de liquidação: dentro de ~14 dias corridos da
# divulgação (≈10 pregões) o resultado ainda é o catalisador ativo da queda.
_RECENT_DAYS = 14


def _to_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (ValueError, TypeError):
        return None


def get_api_key() -> str | None:
    """Chave do Finnhub do ambiente (``.env`` central, SPEC-005). ``None`` = ausente."""
    key = os.getenv("FINNHUB_API_KEY")
    return key.strip() if key and key.strip() else None


# ------------------------------------------------------------ network seams -----
def _finnhub_get(path: str, params: dict) -> object:
    """GET num endpoint do Finnhub (o único ponto de rede — testes dão monkeypatch).

    Devolve o JSON decodificado (lista para ``/stock/earnings``, dict para
    ``/calendar/earnings``). Levanta em erro HTTP para o chamador degradar."""
    key = get_api_key()
    r = requests.get(
        f"{_FINNHUB_BASE}/{path}",
        params={**params, "token": key},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def _fetch_surprise_history(symbol: str) -> list[dict]:
    """Histórico de surpresas (``/stock/earnings``), mais recente primeiro."""
    data = _finnhub_get("stock/earnings", {"symbol": symbol.upper(), "limit": 12})
    if not isinstance(data, list):
        return []
    rows = [r for r in data if isinstance(r, dict) and r.get("actual") is not None]
    # period é o fim do trimestre fiscal (ISO); ordena desc por ele.
    rows.sort(key=lambda r: str(r.get("period") or ""), reverse=True)
    return rows


def _fetch_announce_date(symbol: str, period_end: date) -> date | None:
    """Data REAL de divulgação de um trimestre, do ``/calendar/earnings`` (near-term).

    Procura a linha do calendário com ``epsActual`` preenchido cuja ``date`` cai em
    ``(period_end, period_end + _ANNOUNCE_WINDOW_DAYS]``. Devolve ``None`` quando o
    plano grátis não cobre aquela janela (histórico costuma vir vazio) — o chamador
    então cai na guarda por folga e cita o trimestre, sem inventar data."""
    start = period_end + timedelta(days=1)
    end = period_end + timedelta(days=_ANNOUNCE_WINDOW_DAYS)
    try:
        data = _finnhub_get(
            "calendar/earnings",
            {"from": start.isoformat(), "to": end.isoformat(), "symbol": symbol.upper()},
        )
    except Exception as exc:  # noqa: BLE001 — data de anúncio é opcional; degrada p/ None
        logger.info("finnhub calendar unavailable for %s: %s", symbol, exc)
        return None
    rows = (data or {}).get("earningsCalendar") if isinstance(data, dict) else None
    best: date | None = None
    for row in rows or []:
        if not isinstance(row, dict) or row.get("epsActual") is None:
            continue
        d = _to_date(row.get("date"))
        if d is None or d <= period_end or d > end:
            continue
        if best is None or d < best:
            best = d
    return best


def _pct(actual: float, estimate: float) -> float | None:
    if estimate in (None, 0):
        return None
    return (actual - estimate) / abs(estimate) * 100.0


def get_reported_earnings(symbol: str, curr_date: str) -> dict | None:
    """Resultado reportado mais recente do ``symbol`` já público em ``curr_date``.

    Retorna ``{"symbol","period","announce_date","eps_actual","eps_estimate",
    "surprise","surprise_pct","beat","recent","days_since","quarter","year"}`` ou
    ``None`` quando não há chave/fonte/trimestre público — o chamador trata ``None``
    como "indisponível", jamais inventa. Cacheado (DA-058) e date-guarded.
    """
    from tradingagents.agents.utils.date_guard import clamp

    if get_api_key() is None:
        return None

    guarded = clamp(curr_date)
    base = _to_date(guarded) or datetime.now().date()
    is_live = base >= datetime.now().date()

    k = cache.key(_CATEGORY, symbol.upper(), base.isoformat())
    hit = cache.get(_CATEGORY, k)
    if hit is not None:
        cache.record_hit(_CATEGORY, negative=(hit.get("kind") == "neg"))
        return hit.get("value")

    cache.record_net(_CATEGORY)
    try:
        rows = _fetch_surprise_history(symbol)
    except Exception as exc:  # noqa: BLE001 — fonte instável degrada a "indisponível"
        logger.warning("finnhub earnings source failed for %s: %s", symbol, exc)
        cache.set_neg(_CATEGORY, k, value=None, error={"type": type(exc).__name__, "msg": str(exc)})
        return None

    result = _select_reported(symbol, rows, base, is_live)
    if result is None:
        cache.set_neg(_CATEGORY, k, value=None)
        return None

    permanent = base < datetime.now().date()
    cache.set_ok(_CATEGORY, k, result, permanent)
    return result


def _select_reported(
    symbol: str, rows: list[dict], base: date, is_live: bool
) -> dict | None:
    """Escolhe o trimestre reportado mais recente que já era público em ``base``.

    Percorre do mais recente ao mais antigo; para cada um resolve a data de anúncio
    (real, do calendário) ou aplica a guarda por folga. O primeiro que passa a
    guarda é o resultado — o catalisador ativo da leitura."""
    for row in rows:
        period_end = _to_date(row.get("period"))
        actual = row.get("actual")
        estimate = row.get("estimate")
        if period_end is None or actual is None:
            continue

        announce = _fetch_announce_date(symbol, period_end)
        if announce is not None:
            # Data real conhecida → guarda EXATA.
            if announce > base:
                continue
            eff_date, days_since = announce, (base - announce).days
        else:
            # Sem data real: guarda conservadora por folga (não vaza futuro).
            if not is_live and (period_end + timedelta(days=_ANNOUNCE_LAG_DAYS)) > base:
                continue
            eff_date, days_since = None, (base - period_end).days

        surprise = row.get("surprise")
        surprise_pct = row.get("surprisePercent")
        if surprise_pct is None and actual is not None and estimate is not None:
            surprise_pct = _pct(float(actual), float(estimate))
        beat = surprise_pct is not None and surprise_pct > 0
        return {
            "symbol": symbol.upper(),
            "period": period_end.isoformat(),
            "announce_date": eff_date.isoformat() if eff_date else None,
            "eps_actual": float(actual) if actual is not None else None,
            "eps_estimate": float(estimate) if estimate is not None else None,
            "surprise": float(surprise) if surprise is not None else None,
            "surprise_pct": float(surprise_pct) if surprise_pct is not None else None,
            "beat": bool(beat),
            "recent": days_since is not None and 0 <= days_since <= _RECENT_DAYS,
            "days_since": days_since,
            "quarter": row.get("quarter"),
            "year": row.get("year"),
        }
    return None


# --------------------------------------------------------------- markdown ------
def _fmt_num(v: float | None) -> str:
    return "n/d" if v is None else f"{v:.2f}".replace(".", ",")


def _fmt_pct(v: float | None) -> str:
    return "n/d" if v is None else f"{v:+.1f}%".replace(".", ",")


def format_reported_line(ev: dict) -> str:
    """Linha markdown pt-BR do resultado reportado (data|período + est×rep + surpresa)."""
    emoji = "✅" if ev.get("beat") else "❌"
    verb = "bateu" if ev.get("beat") else "ficou abaixo"
    when = (
        f"divulgado em {ev['announce_date']}"
        if ev.get("announce_date")
        else f"trimestre fiscal encerrado em {ev['period']}"
    )
    q = ev.get("quarter")
    y = ev.get("year")
    qlabel = f" (Q{q} {y})" if q and y else ""
    return (
        f"{emoji} último resultado ({when}{qlabel}): reportado "
        f"{_fmt_num(ev.get('eps_actual'))} × estimado {_fmt_num(ev.get('eps_estimate'))} "
        f"→ surpresa {_fmt_pct(ev.get('surprise_pct'))} — {verb} o consenso"
    )

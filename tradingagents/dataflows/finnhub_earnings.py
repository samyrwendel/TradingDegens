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
# Retrolook (dias) do /calendar/earnings ANCORADO em curr_date (não no fim de trimestre
# fiscal): pega o balanço mais recente já público em curr_date. > cadência trimestral
# (~91d) com folga, pra nunca perder o último report por 1 dia.
_ANNOUNCE_LOOKBACK_DAYS = 120
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


def _fetch_recent_announcement(symbol: str, base: date) -> dict | None:
    """Balanço mais recente JÁ público em ``base``, do ``/calendar/earnings``.

    ANCORADO NA DATA REAL DE DIVULGAÇÃO (janela ``[base - _ANNOUNCE_LOOKBACK_DAYS, base]``),
    NÃO no fim do trimestre fiscal — que engana em ano fiscal DESLOCADO. Ex.: a NVDA
    rotula o Q2 já reportado (ago/2026) como ``period='2027-06-30'`` (~1 ano à frente);
    filtrar por ``period_end <= curr_date`` rejeita o report certo e cai no de um ano
    atrás. Aqui a seleção é pela DATA de divulgação: só linhas com ``epsActual``
    (=reportado, o Finnhub só preenche em trimestre divulgado) e ``date <= base``
    (anti-look-ahead REAL) contam; devolve a MAIS RECENTE, com EPS **e RECEITA**
    (reportado × estimado). ``None`` quando o plano grátis não cobre a janela (backtest
    histórico) → o chamador cai na história de surpresas com a guarda por folga."""
    start = base - timedelta(days=_ANNOUNCE_LOOKBACK_DAYS)
    try:
        data = _finnhub_get(
            "calendar/earnings",
            {"from": start.isoformat(), "to": base.isoformat(), "symbol": symbol.upper()},
        )
    except Exception as exc:  # noqa: BLE001 — calendário é opcional; degrada p/ None
        logger.info("finnhub calendar unavailable for %s: %s", symbol, exc)
        return None
    rows = (data or {}).get("earningsCalendar") if isinstance(data, dict) else None
    best: dict | None = None
    best_d: date | None = None
    for row in rows or []:
        if not isinstance(row, dict) or row.get("epsActual") is None:
            continue
        d = _to_date(row.get("date"))
        if d is None or d > base:          # divulgação depois de base = futuro → não vaza
            continue
        if best_d is None or d > best_d:
            best, best_d = row, d
    if best is None:
        return None
    return {
        "date": best_d,
        "eps_actual": best.get("epsActual"),
        "eps_estimate": best.get("epsEstimate"),
        "revenue_actual": best.get("revenueActual"),
        "revenue_estimate": best.get("revenueEstimate"),
        "quarter": best.get("quarter"),
        "year": best.get("year"),
    }


def _match_history_row(rows: list[dict], quarter, year, eps_actual) -> dict | None:
    """Linha da história de surpresas do MESMO trimestre do anúncio — pra herdar o
    ``period`` fiscal e o ``surprise``/``surprisePercent`` oficiais. Casa por
    (quarter, year); se faltar, por EPS reportado. ``None`` se nada casa."""
    for r in rows:
        if (quarter is not None and year is not None
                and r.get("quarter") == quarter and r.get("year") == year):
            return r
    if eps_actual is not None:
        for r in rows:
            a = r.get("actual")
            if a is not None:
                try:
                    if abs(float(a) - float(eps_actual)) < 1e-6:
                        return r
                except (TypeError, ValueError):
                    continue
    return None


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

    # PRIMÁRIO: balanço mais recente pela DATA REAL de divulgação (calendário ancorado
    # em base) — corrige ano fiscal deslocado (NVDA) e traz a RECEITA. Só cobre near-term
    # no plano grátis; num backtest histórico volta None e caímos na história de surpresas.
    ann = None
    try:
        ann = _fetch_recent_announcement(symbol, base)
    except Exception as exc:  # noqa: BLE001 — anúncio é opcional; degrada p/ fallback
        logger.info("finnhub announcement lookup failed for %s: %s", symbol, exc)
    result = _build_from_announcement(symbol, ann, rows, base) if ann is not None else None
    # FALLBACK: sem anúncio no calendário → trimestre reportado mais recente da história
    # de surpresas, com a guarda por folga (anti-look-ahead do backtest de fiscal normal).
    if result is None:
        result = _select_reported(symbol, rows, base, is_live)
    if result is None:
        cache.set_neg(_CATEGORY, k, value=None)
        return None

    permanent = base < datetime.now().date()
    cache.set_ok(_CATEGORY, k, result, permanent)
    return result


def _build_from_announcement(
    symbol: str, ann: dict | None, rows: list[dict], base: date
) -> dict | None:
    """Monta o resultado a partir do anúncio REAL do calendário (data + EPS + receita),
    herdando ``period``/``surprise`` da história de surpresas do mesmo trimestre. A
    recência (``days_since``/``recent``) sai da DATA DE DIVULGAÇÃO — nunca do fim de
    trimestre fiscal (que engana em ano fiscal deslocado). ``None`` se o anúncio não
    tem EPS/data utilizáveis."""
    if not ann:
        return None
    actual = ann.get("eps_actual")
    announce = ann.get("date")
    if actual is None or announce is None:
        return None
    estimate = ann.get("eps_estimate")
    days_since = (base - announce).days
    match = _match_history_row(rows, ann.get("quarter"), ann.get("year"), actual)
    period = _to_date((match or {}).get("period")) if match else None
    surprise = (match or {}).get("surprise") if match else None
    surprise_pct = (match or {}).get("surprisePercent") if match else None
    if surprise_pct is None and estimate is not None:
        surprise_pct = _pct(float(actual), float(estimate))
    rev_a = ann.get("revenue_actual")
    rev_e = ann.get("revenue_estimate")
    rev_pct = (_pct(float(rev_a), float(rev_e))
               if rev_a is not None and rev_e is not None else None)
    beat = surprise_pct is not None and surprise_pct > 0
    return {
        "symbol": symbol.upper(),
        "period": period.isoformat() if period else None,
        "announce_date": announce.isoformat(),
        "eps_actual": float(actual),
        "eps_estimate": float(estimate) if estimate is not None else None,
        "surprise": float(surprise) if surprise is not None else None,
        "surprise_pct": float(surprise_pct) if surprise_pct is not None else None,
        "beat": bool(beat),
        "recent": 0 <= days_since <= _RECENT_DAYS,
        "days_since": days_since,
        "quarter": ann.get("quarter"),
        "year": ann.get("year"),
        # RECEITA (task 010): TradingView/Samyr olham receita, não só EPS. None quando
        # o calendário não trouxe (algumas linhas vêm sem revenue).
        "revenue_actual": float(rev_a) if rev_a is not None else None,
        "revenue_estimate": float(rev_e) if rev_e is not None else None,
        "revenue_surprise_pct": float(rev_pct) if rev_pct is not None else None,
    }


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
            # Receita não vem no /stock/earnings (só o calendário a traz, task 010):
            # no fallback fica ausente — schema consistente com o caminho primário.
            "revenue_actual": None,
            "revenue_estimate": None,
            "revenue_surprise_pct": None,
        }
    return None


# --------------------------------------------------------------- markdown ------
def _fmt_num(v: float | None) -> str:
    return "n/d" if v is None else f"{v:.2f}".replace(".", ",")


def _fmt_pct(v: float | None) -> str:
    return "n/d" if v is None else f"{v:+.1f}%".replace(".", ",")


def _fmt_big(v: float | None) -> str:
    """Valor grande (receita) em B/M pt-BR. 96_221_000_000 → '96,22 B'."""
    if v is None:
        return "n/d"
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.2f} B".replace(".", ",")
    if a >= 1e6:
        return f"{v / 1e6:.2f} M".replace(".", ",")
    return f"{v:,.0f}"


def format_reported_line(ev: dict) -> str:
    """Linha markdown pt-BR do resultado reportado (data|período + est×rep + surpresa).

    Quando o calendário trouxe a RECEITA (task 010), acrescenta reportada × estimada →
    surpresa — o TradingView/Samyr olham receita, não só EPS."""
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
    line = (
        f"{emoji} último resultado ({when}{qlabel}): reportado "
        f"{_fmt_num(ev.get('eps_actual'))} × estimado {_fmt_num(ev.get('eps_estimate'))} "
        f"→ surpresa {_fmt_pct(ev.get('surprise_pct'))} — {verb} o consenso"
    )
    if ev.get("revenue_actual") is not None:
        rev = f" · receita {_fmt_big(ev.get('revenue_actual'))}"
        if ev.get("revenue_estimate") is not None:
            rev += f" × est. {_fmt_big(ev.get('revenue_estimate'))}"
        if ev.get("revenue_surprise_pct") is not None:
            rev += f" → {_fmt_pct(ev.get('revenue_surprise_pct'))}"
        line += rev
    return line

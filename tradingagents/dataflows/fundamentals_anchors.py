"""Deterministic fundamentals anchors — one frozen reference price per run and
TTM aggregates summed straight from the quarterly table.

Two drifts this kills (both from :func:`y_finance.get_fundamentals` reading
yfinance's real-time ``info`` instead of the date-guarded series the rest of the
engine uses):

* **Reference price wandered between modules** — the cover read 113,15, the
  technical read 112,68, Erick 113,24 and the fundamentals block implied ~119 from a
  LIVE ``marketCap``. Here the price is the SAME date-guarded daily close the chart
  and the verdict use, so every consumer that anchors to this snapshot shares ONE
  price (and market cap = price × shares, 52-week low/high off the same series, not
  yfinance's live fields).

* **Agents summed the quarterly cash-flow table wrong** — the bear cited "FCF
  -887M TTM" when the four quarters add to ~-601M, and "-692M em quatro trimestres"
  actually summed five. TTM is DEFINED here as exactly the four most-recent
  date-guarded quarters, summed deterministically, so the agent cites the computed
  number instead of doing (wrong) mental arithmetic.

Every number is computed from real, date-guarded data; nothing is fabricated, and a
missing row/label degrades to "não disponível" rather than a guess.
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from tradingagents.datacache import cache

logger = logging.getLogger(__name__)

# ~252 trading days ≈ 52 weeks — the window for the low/high, off the same daily
# series the chart draws (not yfinance's live ``fiftyTwoWeekLow``, which drifts).
_TRADING_DAYS_52W = 252

# Cache DA-058 pros fetches da âncora (hoje é rede crua a cada run). Mesma
# disciplina do earnings_calendar: histórico (curr_date < hoje) é permanente —
# a tabela trimestral DE 27/08 não muda mais; run ao vivo expira à meia-noite.
_ANCHORS_CATEGORY = "fundamentals_anchors"


def _is_permanent(curr_date: str) -> bool:
    """Histórico (curr_date < hoje) é fato estável — cache permanente. Run ao
    vivo expira à meia-noite (mesma regra do cache.classify do DA-058)."""
    from datetime import date, datetime

    try:
        return datetime.fromisoformat(str(curr_date)[:10]).date() < date.today()
    except ValueError:
        return False


def _cached_call(name: str, key_parts: tuple, fetch):
    """Cacheia UMA chamada de fetch da âncora pelo DA-058 (positivo e negativo).

    ``fetch`` devolve ``(valor, meta)`` ou levanta. Falha → negativo com erro
    (TTL curto — a fonte pode voltar); sucesso → permanente quando histórico.
    Devolve o dict cacheado ``{"value","meta"}`` ou ``None``. Valores precisam
    ser JSON-serializable (DataFrames não são — cacheia-se a forma crua).
    """
    k = cache.key(name, *key_parts)
    hit = cache.get(name, k)
    if hit is not None:
        neg = hit.get("kind") == "neg"
        cache.record_hit(name, negative=neg)
        if not neg and hit.get("value") is not None:
            return hit["value"]
        return None
    cache.record_net(name)
    try:
        value, meta = fetch()
    except Exception as exc:  # noqa: BLE001 — falha de fonte vira negativo, não exceção
        cache.set_neg(name, k, value=None, error={"type": type(exc).__name__, "msg": str(exc)})
        return None
    if value is None:
        cache.set_neg(name, k, value=None)
        return None
    cache.set_ok(name, k, {"value": value, "meta": meta}, _is_permanent(key_parts[-1] if key_parts else ""))
    return {"value": value, "meta": meta}

# yfinance quarterly-cashflow row labels vary by vintage; match the first alias
# present (case-insensitive, exact after strip).
_FCF_ALIASES = ("Free Cash Flow", "FreeCashFlow")
_OCF_ALIASES = (
    "Operating Cash Flow",
    "Cash Flow From Continuing Operating Activities",
    "Total Cash From Operating Activities",
)
_CAPEX_ALIASES = ("Capital Expenditure", "Capital Expenditures")

# TTM is, by definition, the trailing FOUR quarters — no more, no less.
_TTM_QUARTERS = 4


def _row(quarterly: pd.DataFrame | None, aliases: tuple[str, ...]) -> pd.Series | None:
    """First row whose label matches one of ``aliases`` (case-insensitive)."""
    if quarterly is None or getattr(quarterly, "empty", True):
        return None
    lower = {str(idx).strip().lower(): idx for idx in quarterly.index}
    for alias in aliases:
        hit = lower.get(alias.strip().lower())
        if hit is not None:
            return quarterly.loc[hit]
    return None


def ttm_sum(
    quarterly: pd.DataFrame | None, aliases: tuple[str, ...], n: int = _TTM_QUARTERS
) -> tuple[float | None, list[str]]:
    """Sum the ``n`` most-recent quarters of the first matching row.

    Returns ``(sum, [quarter labels])``. Returns ``(None, quarters)`` when the row is
    absent OR there are fewer than ``n`` quarters — a partial year is NOT a "TTM"
    (that is exactly the "-692M em 4 tri that summed 5"/"887 vs 601" bug), so we
    refuse to emit a misleading aggregate instead of quietly summing whatever exists.
    """
    row = _row(quarterly, aliases)
    if row is None:
        return None, []
    # Order columns newest-first by period-end date, then take the trailing n.
    cols = sorted(
        row.index, key=lambda c: pd.to_datetime(c, errors="coerce"), reverse=True
    )
    picked = cols[:n]
    labels = [str(pd.to_datetime(c, errors="coerce").date()) for c in picked]
    values = [float(row[c]) for c in picked if pd.notna(row[c])]
    if len(values) < n:
        return None, labels
    return sum(values), labels


def compute_ttm_cashflow(quarterly: pd.DataFrame | None) -> dict[str, Any]:
    """Deterministic TTM cash-flow aggregates from the quarterly table."""
    fcf, quarters = ttm_sum(quarterly, _FCF_ALIASES)
    ocf, _ = ttm_sum(quarterly, _OCF_ALIASES)
    capex, _ = ttm_sum(quarterly, _CAPEX_ALIASES)
    return {"fcf_ttm": fcf, "ocf_ttm": ocf, "capex_ttm": capex, "quarters": quarters}


def price_snapshot(
    daily: pd.DataFrame | None, shares: float | None = None
) -> dict[str, Any] | None:
    """Frozen as_of snapshot from the date-guarded daily series.

    The price is the last date-guarded close — the SAME number the chart and the
    verdict use — so anchoring here removes the cross-module price drift. Market cap is
    ``price × shares`` (``None`` when shares are unknown); the 52-week low/high come
    off this series, not yfinance's live fields.
    """
    if daily is None or getattr(daily, "empty", True) or "Close" not in daily:
        return None
    d = daily.reset_index(drop=True)
    close_series = d["Close"].astype(float)
    close = float(close_series.iloc[-1])
    as_of = None
    if "Date" in d:
        as_of_ts = pd.to_datetime(d["Date"].iloc[-1], errors="coerce")
        as_of = None if pd.isna(as_of_ts) else str(as_of_ts.date())
    window = d.tail(_TRADING_DAYS_52W)
    low_52w = float(window["Low"].min()) if "Low" in window else None
    high_52w = float(window["High"].max()) if "High" in window else None
    market_cap = round(close * float(shares)) if shares else None
    # Canonical moving averages off the SAME date-guarded series the chart draws
    # (MMS50/MMS200), so the report carries ONE value per average instead of the
    # yfinance-live 50/200-day figures drifting from the chart (item 6: 126,48 vs
    # 127,60 / 98,22 vs 97,81). None until the window has enough bars.
    ma_50 = round(float(close_series.rolling(50).mean().iloc[-1]), 2) if len(d) >= 50 else None
    ma_200 = round(float(close_series.rolling(200).mean().iloc[-1]), 2) if len(d) >= 200 else None
    return {
        "as_of": as_of,
        "price": round(close, 2),
        "low_52w": round(low_52w, 2) if low_52w is not None else None,
        "high_52w": round(high_52w, 2) if high_52w is not None else None,
        "ma_50": ma_50,
        "ma_200": ma_200,
        "shares": shares,
        "market_cap": market_cap,
    }


def _money(v: float | None) -> str:
    """Formata um agregado em USD com a PALAVRA DE MAGNITUDE explícita (bug 014).

    O número cru (``136683000000``) deixava o agente re-escalar: o bull citou
    "136,683 trilhões" em vez de "136,68 bilhões" (1000×). Injetar a magnitude
    inline ("US$ 136.68 bilhões" / "-US$ 601.31 milhões") remove a ambiguidade — os
    agentes são instruídos a citar essa string exata. O separador decimal segue o
    resto do bloco de âncoras (en-US, ponto), pra casar com os preços ao lado.
    """
    if v is None:
        return "não disponível"
    sign = "-" if v < 0 else ""
    a = abs(float(v))
    if a >= 1e12:
        return f"{sign}US$ {a / 1e12:,.2f} trilhões"
    if a >= 1e9:
        return f"{sign}US$ {a / 1e9:,.2f} bilhões"
    if a >= 1e6:
        return f"{sign}US$ {a / 1e6:,.2f} milhões"
    return f"{sign}US$ {a:,.0f}"       # < 1 milhão: número cru (raro pra esses agregados)


def render_anchors_section(
    snapshot: dict[str, Any] | None, ttm: dict[str, Any] | None,
    fcf_xcheck: str | None = None,
) -> str | None:
    """Render the deterministic 'Âncoras' markdown section (pt-BR), or ``None`` when
    there is nothing real to anchor."""
    snapshot = snapshot or {}
    ttm = ttm or {}
    has_price = snapshot.get("price") is not None
    has_ttm = any(ttm.get(k) is not None for k in ("fcf_ttm", "ocf_ttm", "capex_ttm"))
    if not has_price and not has_ttm:
        return None

    as_of = snapshot.get("as_of") or "a data da análise"
    lines = [
        "## Âncoras determinísticas (preço de referência + agregados TTM)",
        "",
        f"_Calculado da série date-guarded e da tabela trimestral (até {as_of}). "
        "Cite APENAS estes números para preço atual, market cap, mínima/máxima de 52 "
        "semanas e agregados TTM — não leia uma cotação live diferente nem some os "
        "trimestres na mão. Os agregados (market cap, FCF/FCO/Capex TTM) já vêm com a "
        "PALAVRA DE MAGNITUDE (bilhões/milhões) — cite-a EXATAMENTE, não re-escale (um "
        "valor em bilhões NÃO é trilhões). TTM = soma dos 4 trimestres mais recentes._",
        "",
    ]
    if has_price:
        lines.append(f"- **Preço de referência (as_of {as_of})**: {snapshot['price']:,.2f}")
        if snapshot.get("market_cap") is not None:
            sh = snapshot.get("shares")
            sh_txt = f" (preço × {sh:,.0f} ações)" if sh else ""
            lines.append(f"- **Market cap**: {_money(snapshot['market_cap'])}{sh_txt}")
        if snapshot.get("low_52w") is not None or snapshot.get("high_52w") is not None:
            lo = snapshot.get("low_52w")
            hi = snapshot.get("high_52w")
            lo_t = "n/d" if lo is None else f"{lo:,.2f}"
            hi_t = "n/d" if hi is None else f"{hi:,.2f}"
            lines.append(f"- **Mínima 52 semanas**: {lo_t} · **Máxima 52 semanas**: {hi_t}")
        if snapshot.get("ma_50") is not None or snapshot.get("ma_200") is not None:
            m50 = snapshot.get("ma_50")
            m200 = snapshot.get("ma_200")
            m50_t = "n/d" if m50 is None else f"{m50:,.2f}"
            m200_t = "n/d" if m200 is None else f"{m200:,.2f}"
            lines.append(f"- **Média 50 dias (MMS50)**: {m50_t} · **Média 200 dias (MMS200)**: {m200_t}")
    if has_ttm:
        quarters = ttm.get("quarters") or []
        q_txt = f" (soma de {', '.join(quarters)})" if quarters else ""
        fonte = ttm.get("fonte")
        f_txt = f" [fonte: {fonte}]" if fonte else ""
        if ttm.get("fcf_ttm") is not None:
            lines.append(f"- **FCF TTM**{q_txt}{f_txt}: {_money(ttm['fcf_ttm'])}")
        if ttm.get("ocf_ttm") is not None:
            lines.append(f"- **Fluxo de caixa operacional TTM**: {_money(ttm['ocf_ttm'])}")
        if ttm.get("capex_ttm") is not None:
            lines.append(f"- **Capex TTM**: {_money(ttm['capex_ttm'])}")
    if fcf_xcheck:
        lines.append(fcf_xcheck)
    return "\n".join(lines)


def _av_quarterly_to_frame(payload: str | dict, curr_date: str) -> pd.DataFrame | None:
    """Reshape do CASH_FLOW do Alpha Vantage no DataFrame que ``ttm_sum`` espera.

    O AV não tem linha "Free Cash Flow" — deriva por trimestre como
    ``operatingCashflow − capitalExpenditures`` (a definição que a tabela do
    yfinance também usa). Colunas = datas dos trimestres (o filtro de date-guard
    do próprio módulo AV já derrubou os períodos posteriores a ``curr_date``).
    """
    import json

    data = payload if isinstance(payload, dict) else None
    if data is None:
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            return None
    reports = (data or {}).get("quarterlyReports") or []
    if len(reports) < _TTM_QUARTERS:
        return None
    fcf_row: dict[str, float] = {}
    ocf_row: dict[str, float] = {}
    capex_row: dict[str, float] = {}
    for r in reports:
        end = str(r.get("fiscalDateEnding") or "")[:10]
        if not end:
            continue
        try:
            ocf = float(r.get("operatingCashflow") or 0) or None
            capex = float(r.get("capitalExpenditures") or 0) or None
        except (TypeError, ValueError):
            continue
        if ocf is None:
            continue
        # AV reporta capex NEGATIVO (saída de caixa) — igual ao yfinance. FCF =
        # OCF − |capex|, imune ao sinal que a linha vier.
        fcf_row[end] = ocf - abs(capex or 0.0)
        ocf_row[end] = ocf
        capex_row[end] = capex if capex is not None else float("nan")
    if len(fcf_row) < _TTM_QUARTERS:
        return None
    # Shape do yfinance: ÍNDICE = labels ("Free Cash Flow"...), COLUNAS = datas
    # do trimestre. O dict-of-dicts monta transposto — por isso o .T.
    return pd.DataFrame(
        {
            "Free Cash Flow": fcf_row,
            "Operating Cash Flow": ocf_row,
            "Capital Expenditure": capex_row,
        }
    ).T


def _fetch_quarterly_cashflow(symbol: str, curr_date: str) -> tuple[pd.DataFrame | None, str | None]:
    """Tabela trimestral date-guarded, MULTI-FONTE (fail-open → (None, None)).

    Fonte 1: yfinance (canônica). Fonte 2: Alpha Vantage ``CASH_FLOW`` — só
    quando o yfinance falha/vazio e a chave ``ALPHA_VANTAGE_API_KEY`` existe.
    Devolve ``(frame, fonte)`` pra a âncora DECLARAR de onde veio o número —
    número de fundamentals sem fonte nomeada é o que tornou o ⚠️ do FCF um
    mistério até hoje.
    """
    try:
        import yfinance as yf

        from .stockstats_utils import filter_financials_by_date, yf_retry
        from .symbol_utils import normalize_symbol

        canonical = normalize_symbol(symbol)
        raw = yf_retry(lambda: yf.Ticker(canonical).quarterly_cashflow)
        if raw is not None and not raw.empty:
            frame = filter_financials_by_date(raw, curr_date)
            if frame is not None and not frame.empty:
                return frame, "yfinance"
    except Exception as exc:  # noqa: BLE001 — enrichment must never break the run
        logger.info("quarterly cash flow unavailable for %s: %s", symbol, exc)

    try:
        from .alpha_vantage_common import get_api_key
        from .alpha_vantage_fundamentals import get_cashflow

        if get_api_key() is None:
            return None, None
        payload = get_cashflow(symbol, curr_date=curr_date)
        frame = _av_quarterly_to_frame(payload, curr_date)
        if frame is not None:
            logger.info("quarterly cashflow fallback alpha_vantage usado para %s", symbol)
            return frame, "alpha_vantage"
    except Exception as exc:  # noqa: BLE001 — fallback também é fail-open
        logger.info("alpha vantage cashflow fallback failed for %s: %s", symbol, exc)
    return None, None


def _cached_ttm(symbol: str, curr_date: str) -> dict[str, Any] | None:
    """Agregados TTM + fonte, cacheados (DA-058). O DataFrame NÃO é cacheado —
    cacheia-se o resultado computado (JSON-serializable)."""
    out = _cached_call(_ANCHORS_CATEGORY, ("ttm", symbol.upper(), str(curr_date)[:10]),
                       lambda: _fetch_quarterly_cashflow_pair(symbol, curr_date))
    return (out or {}).get("value") if out else None


def _fetch_quarterly_cashflow_pair(symbol: str, curr_date: str) -> tuple[dict, dict]:
    """(meta, valor) pro cache: computa o TTM do frame multi-fonte."""
    frame, fonte = _fetch_quarterly_cashflow(symbol, curr_date)
    ttm = compute_ttm_cashflow(frame)
    if ttm.get("fcf_ttm") is None and ttm.get("ocf_ttm") is None:
        return None, {}
    if fonte:
        ttm["fonte"] = fonte
    return ttm, {}


def _fetch_shares(symbol: str, curr_date: str) -> tuple[float | None, str | None]:
    """Shares outstanding MULTI-FONTE (fail-open → (None, None)).

    Fonte 1: yfinance ``info``. Fonte 2: Finnhub ``profile2`` (chave já usada
    pelo earnings do âncora). O par ``(valor, fonte)`` permite declarar origem.
    Cacheado (DA-058) — shares mudam pouco; histórico é permanente.
    """
    out = _cached_call(
        _ANCHORS_CATEGORY, ("shares", symbol.upper(), str(curr_date)[:10]),
        lambda: _fetch_shares_pair(symbol),
    )
    if not out:
        return None, None
    value = out.get("value")
    return (float(value["shares"]), value["fonte"]) if value else (None, None)


def _fetch_shares_pair(symbol: str) -> tuple[dict, dict]:
    """(valor, meta) pro cache."""
    try:
        import yfinance as yf

        from .stockstats_utils import yf_retry
        from .symbol_utils import normalize_symbol

        info = yf_retry(lambda: yf.Ticker(normalize_symbol(symbol)).info) or {}
        shares = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")
        if shares:
            return {"shares": float(shares), "fonte": "yfinance"}, {}
    except Exception as exc:  # noqa: BLE001
        logger.info("shares outstanding unavailable for %s: %s", symbol, exc)

    try:
        from .finnhub_fundamentals import get_shares

        sh = get_shares(symbol)
        if sh is not None:
            logger.info("shares fallback finnhub usado para %s", symbol)
            return {"shares": float(sh), "fonte": "finnhub"}, {}
    except Exception as exc:  # noqa: BLE001 — idem: fail-open
        logger.info("finnhub shares fallback failed for %s: %s", symbol, exc)
    return None, {}


# Divergência que faz a linha de conferência aparecer. ARBITRÁRIA e declarada: o
# Finnhub free é live (não date-guarded) e deriva o FCF de razões — 15% absorve o
# drift natural de data sem engolir divergência de verdade. A calibrar com uso.
_XCHECK_TOL = 0.15
_XCHECK_NOTE = "limiar provisório de 15% — a calibrar com uso"


def _fcf_crosscheck_from(fcf_ttm: float | None, other: float | None) -> str | None:
    """A LINHA de conferência, pura (valor do Finnhub injetado — testável).

    ``None`` quando não há o que conferir (âncora ou conferência ausente) ou a
    divergência está dentro do limiar — silêncio é concordância, não erro.
    """
    if fcf_ttm is None or other is None or other == 0:
        return None
    if abs(other - fcf_ttm) / max(abs(fcf_ttm), 1.0) <= _XCHECK_TOL:
        return None
    return (f"- **⚠️ FCF TTM em conferência**: Finnhub ≈ {_money(other)} vs âncora "
            f"{_money(fcf_ttm)} — fontes divergem [{_XCHECK_NOTE}]. A âncora "
            "date-guarded continua sendo o número canônico; a divergência pode "
            "ser de data (o Finnhub free é live) — declarada, não escondida.")


def _fcf_crosscheck(symbol: str, fcf_ttm: float | None) -> str | None:
    """Linha de CONFERÊNCIA do FCF TTM: Finnhub (live) × âncora (date-guarded).

    A conferência nunca substitui a âncora — só RENDERIZA a divergência com os
    dois números lado a lado. Ausente (sem chave/endpoint) → None, sem ruído.
    """
    if fcf_ttm is None:
        return None
    try:
        from .finnhub_fundamentals import get_fcf_ttm

        other = get_fcf_ttm(symbol)
    except Exception as exc:  # noqa: BLE001 — conferência ausente não derruba nada
        logger.info("fcf crosscheck unavailable for %s: %s", symbol, exc)
        return None
    return _fcf_crosscheck_from(fcf_ttm, other)


def build_fundamentals_anchors_section(symbol: str, curr_date: str) -> str | None:
    """Fetch the date-guarded series + quarterly table and render the anchors section.

    Fail-open: any data hiccup yields ``None`` (the caller leaves the report as-is) so
    a fundamentals enrichment never blocks the analysis.
    """
    try:
        from .stockstats_utils import load_ohlcv

        daily = load_ohlcv(symbol, curr_date)
    except Exception as exc:  # noqa: BLE001
        logger.info("daily series unavailable for %s anchors: %s", symbol, exc)
        daily = None

    shares, shares_fonte = _fetch_shares(symbol, curr_date) if daily is not None else (None, None)
    snapshot = price_snapshot(daily, shares)
    if snapshot is not None and shares_fonte:
        snapshot["shares_fonte"] = shares_fonte
    ttm = _cached_ttm(symbol, curr_date) or {}
    xcheck = _fcf_crosscheck(symbol, ttm.get("fcf_ttm"))
    return render_anchors_section(snapshot, ttm, xcheck)


def as_of_reference_price(symbol: str, curr_date: str) -> float | None:
    """The single frozen reference price for a run — the date-guarded daily close.

    Shared lever for the instrument context so every module anchors the SAME price;
    identical to the value the actionable plan and the chart already use. Fail-open.
    """
    try:
        from .stockstats_utils import load_ohlcv

        snap = price_snapshot(load_ohlcv(symbol, curr_date))
        return snap["price"] if snap else None
    except Exception as exc:  # noqa: BLE001
        logger.info("reference price unavailable for %s: %s", symbol, exc)
        return None

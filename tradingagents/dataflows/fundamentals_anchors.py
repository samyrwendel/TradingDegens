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

logger = logging.getLogger(__name__)

# ~252 trading days ≈ 52 weeks — the window for the low/high, off the same daily
# series the chart draws (not yfinance's live ``fiftyTwoWeekLow``, which drifts).
_TRADING_DAYS_52W = 252

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
    return "não disponível" if v is None else f"{v:,.0f}"


def render_anchors_section(
    snapshot: dict[str, Any] | None, ttm: dict[str, Any] | None
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
        "trimestres na mão. TTM = soma dos 4 trimestres mais recentes._",
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
        if ttm.get("fcf_ttm") is not None:
            lines.append(f"- **FCF TTM**{q_txt}: {_money(ttm['fcf_ttm'])}")
        if ttm.get("ocf_ttm") is not None:
            lines.append(f"- **Fluxo de caixa operacional TTM**: {_money(ttm['ocf_ttm'])}")
        if ttm.get("capex_ttm") is not None:
            lines.append(f"- **Capex TTM**: {_money(ttm['capex_ttm'])}")
    return "\n".join(lines)


def _fetch_quarterly_cashflow(symbol: str, curr_date: str) -> pd.DataFrame | None:
    """Date-guarded quarterly cash-flow frame (fail-open → None)."""
    try:
        import yfinance as yf

        from .stockstats_utils import filter_financials_by_date, yf_retry
        from .symbol_utils import normalize_symbol

        canonical = normalize_symbol(symbol)
        raw = yf_retry(lambda: yf.Ticker(canonical).quarterly_cashflow)
        if raw is None or raw.empty:
            return None
        return filter_financials_by_date(raw, curr_date)
    except Exception as exc:  # noqa: BLE001 — enrichment must never break the run
        logger.info("quarterly cash flow unavailable for %s: %s", symbol, exc)
        return None


def _fetch_shares(symbol: str) -> float | None:
    """Shares outstanding for the market-cap anchor (fail-open → None)."""
    try:
        import yfinance as yf

        from .stockstats_utils import yf_retry
        from .symbol_utils import normalize_symbol

        info = yf_retry(lambda: yf.Ticker(normalize_symbol(symbol)).info) or {}
        shares = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")
        return float(shares) if shares else None
    except Exception as exc:  # noqa: BLE001
        logger.info("shares outstanding unavailable for %s: %s", symbol, exc)
        return None


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

    shares = _fetch_shares(symbol) if daily is not None else None
    snapshot = price_snapshot(daily, shares)
    ttm = compute_ttm_cashflow(_fetch_quarterly_cashflow(symbol, curr_date))
    return render_anchors_section(snapshot, ttm)


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

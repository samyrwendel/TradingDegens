"""Real crypto derivatives data — funding, open interest, liquidations (fork addition).

The upstream "crypto support" is just handing a ``BTC-USD`` ticker to yfinance
(the same thing the 131-star ``0x0funky/TradingAgents-crypto`` fork does — none
of its files even mention Binance or CoinGecko). For an equity that is enough;
for a perpetual-driven crypto asset it is blind, because it omits exactly what
moves the price:

1. **Funding rate** — who pays to hold the position (crowd positioning / carry).
2. **Open interest** — how much leveraged money is in the book.
3. **Liquidations** — the fuel of violent moves (forced closes).

Every source here is a **keyless public REST endpoint** (verified live), matching
what the house's degenbot already pulls from Hyperliquid — we reuse the approach,
not a bespoke client:

    signal          primary source                         fallback
    -------------   ------------------------------------   ----------------------
    funding         Hyperliquid ``metaAndAssetCtxs`` (1h)  Binance fundingRate (8h)
    open interest   Hyperliquid ``metaAndAssetCtxs``       Binance openInterest(Hist)
    liquidations    OKX ``public/liquidation-orders``      — (live/recent only)
    mark / 24h vol  Hyperliquid ``metaAndAssetCtxs``       Binance premiumIndex

Design rules honored:

* **24/7** — crypto never closes. Nothing here assumes a business day; every
  window is driven by millisecond UTC timestamps, and a weekend analysis date is
  a normal trading day.
* **No look-ahead** — for a backtest date (``curr_date`` < today) we never read a
  live "now" value: funding comes from Hyperliquid ``fundingHistory`` (Binance
  history as fallback) clamped to the requested day's end, and OI/liquidations
  that only expose a recent window degrade with an explicit notice rather than
  borrowing today's number. The tool wrapper also clamps ``curr_date`` via the
  date guard.
* **Exact values** — the raw funding rate, open interest and mark price are
  printed verbatim as the API returned them (``*_raw``); only clearly-derived
  figures (annualized %, USD notional) are approximations, marked ``~`` / ``≈``.
* **Never fabricate** — a source that errors or has no data for the requested
  date contributes an explicit "unavailable" line naming the source; it is never
  silently replaced by an invented figure.

Hyperliquid is the **primary** derivatives source (funding + OI + mark in one
call, plus its own historical-funding endpoint); Binance is fallback only. A coin
Hyperliquid does not list returns ``null`` / raises, which cleanly degrades to the
fallback rather than crashing.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from .errors import NoMarketDataError
from .symbol_utils import crypto_base

logger = logging.getLogger(__name__)

_TIMEOUT = 12
_HL_URL = "https://api.hyperliquid.xyz/info"
_BINANCE_FAPI = "https://fapi.binance.com"
_OKX_URL = "https://www.okx.com"

# Max liquidation orders to aggregate from OKX's recent-orders feed.
_LIQ_LIMIT = 100
# OKX openInterestHist / Binance openInterestHist only retain ~30 days; older
# backtest dates therefore cannot get historical OI or liquidations and must
# degrade rather than borrow a live value.
_HIST_WINDOW_DAYS = 30


# --------------------------------------------------------------------- http ---
def _get_json(url, params=None):
    r = requests.get(url, params=params, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _post_json(url, payload):
    r = requests.post(url, json=payload, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _parse_date(curr_date) -> datetime:
    """Parse ``curr_date`` (yyyy-mm-dd) to a UTC-midnight datetime."""
    if isinstance(curr_date, datetime):
        dt = curr_date
    else:
        dt = datetime.strptime(str(curr_date)[:10], "%Y-%m-%d")
    return dt.replace(tzinfo=timezone.utc)


def _end_of_day_ms(as_of: datetime) -> int:
    """Epoch-ms for 23:59:59.999 UTC on ``as_of`` — the inclusive window end."""
    eod = as_of.replace(hour=23, minute=59, second=59, microsecond=999000)
    return int(eod.timestamp() * 1000)


def _fmt_usd(v: float) -> str:
    a = abs(v)
    if a >= 1e9:
        return f"${v / 1e9:.2f}B"
    if a >= 1e6:
        return f"${v / 1e6:.2f}M"
    if a >= 1e3:
        return f"${v / 1e3:.1f}K"
    return f"${v:,.0f}"


def _annualize(rate: float, per_day: float) -> float:
    """Annualized % from a single-interval rate paid ``per_day`` times a day."""
    return rate * per_day * 365 * 100


# -------------------------------------------------------------- Hyperliquid ----
# Hyperliquid is the PRIMARY derivatives source (Samyr's call): funding, OI and
# mark price arrive in one keyless call, and historical funding has its own
# endpoint — so HL leads for both live and backtest, Binance is only fallback.
# The raw API strings are carried through verbatim so the report prints the
# exact value the API returned, never a prettied-up round number.
def _hl_ctx(base: str) -> dict:
    """Live per-asset context from Hyperliquid: funding(1h), OI, mark, 24h vol.

    Keeps the raw API strings (``*_raw``) alongside parsed floats so the report
    can cite the exact figure the endpoint returned.
    """
    data = _post_json(_HL_URL, {"type": "metaAndAssetCtxs"})
    universe = data[0]["universe"]
    ctxs = data[1]
    idx = next((i for i, u in enumerate(universe) if u.get("name") == base), None)
    if idx is None or idx >= len(ctxs):
        raise NoMarketDataError(base, None, "not listed on Hyperliquid")
    c = ctxs[idx]
    funding = float(c["funding"])          # hourly rate
    oi_coin = float(c["openInterest"])     # in coin units
    mark = float(c["markPx"])
    return {
        "funding_raw": str(c["funding"]),
        "funding_hourly": funding,
        "funding_annual_pct": _annualize(funding, 24),
        "oi_raw": str(c["openInterest"]),
        "oi_coin": oi_coin,
        "oi_usd": oi_coin * mark,
        "mark_raw": str(c["markPx"]),
        "mark": mark,
        "day_ntl_vlm": float(c.get("dayNtlVlm", 0) or 0),
    }


def _hl_funding_history(base: str, as_of: datetime) -> dict:
    """Last Hyperliquid funding (1h) at/before the requested day — for backtests.

    HL ``fundingHistory`` returns ``null`` for an unknown coin; we treat that as
    "not on Hyperliquid" so the caller degrades to Binance rather than crashing.
    """
    # A 3-day lookback window is plenty to contain the last hourly funding print.
    start = _end_of_day_ms(as_of) - 3 * 86_400_000
    rows = _post_json(
        _HL_URL,
        {"type": "fundingHistory", "coin": base,
         "startTime": start, "endTime": _end_of_day_ms(as_of)},
    )
    if not rows:
        raise NoMarketDataError(base, None, "no Hyperliquid funding history for that date")
    last = rows[-1]
    rate = float(last["fundingRate"])
    return {
        "funding_raw": str(last["fundingRate"]),
        "funding_hourly": rate,
        "funding_annual_pct": _annualize(rate, 24),
        "as_of": datetime.fromtimestamp(int(last["time"]) / 1000, tz=timezone.utc),
    }


# --------------------------------------------------------------- Binance -------
def _binance_funding(base: str, as_of: datetime, is_live: bool) -> dict:
    """Funding (8h) — live premiumIndex, or history clamped to the requested day."""
    sym = f"{base}USDT"
    if is_live:
        d = _get_json(f"{_BINANCE_FAPI}/fapi/v1/premiumIndex", {"symbol": sym})
        rate = float(d["lastFundingRate"])
        when = datetime.fromtimestamp(int(d["time"]) / 1000, tz=timezone.utc)
    else:
        rows = _get_json(
            f"{_BINANCE_FAPI}/fapi/v1/fundingRate",
            {"symbol": sym, "endTime": _end_of_day_ms(as_of), "limit": 1},
        )
        if not rows:
            raise NoMarketDataError(base, sym, "no Binance funding at that date")
        last = rows[-1]
        rate = float(last["fundingRate"])
        when = datetime.fromtimestamp(int(last["fundingTime"]) / 1000, tz=timezone.utc)
        d = last
    return {
        "funding_raw": str(d["lastFundingRate"] if is_live else d["fundingRate"]),
        "funding_8h": rate,
        "funding_annual_pct": _annualize(rate, 3),
        "as_of": when,
    }


def _binance_oi(base: str, as_of: datetime, is_live: bool) -> dict:
    """Open interest — live snapshot, or the last daily hist bar on/before the date."""
    sym = f"{base}USDT"
    if is_live:
        d = _get_json(f"{_BINANCE_FAPI}/fapi/v1/openInterest", {"symbol": sym})
        oi_coin = float(d["openInterest"])
        px = _get_json(f"{_BINANCE_FAPI}/fapi/v1/premiumIndex", {"symbol": sym})
        mark = float(px["markPrice"])
        return {"oi_raw": str(d["openInterest"]), "oi_coin": oi_coin, "oi_usd": oi_coin * mark}
    rows = _get_json(
        f"{_BINANCE_FAPI}/futures/data/openInterestHist",
        {"symbol": sym, "period": "1d", "endTime": _end_of_day_ms(as_of), "limit": 1},
    )
    if not rows:
        raise NoMarketDataError(base, sym, "no Binance OI history at that date")
    last = rows[-1]
    return {
        "oi_raw": str(last["sumOpenInterest"]),
        "oi_coin": float(last["sumOpenInterest"]),
        "oi_usd": float(last["sumOpenInterestValue"]),
    }


# ------------------------------------------------------------------- OKX -------
def _okx_ctval(inst_id: str) -> float:
    """Contract value (coin per contract) for an OKX SWAP, e.g. 0.01 BTC."""
    d = _get_json(
        f"{_OKX_URL}/api/v5/public/instruments",
        {"instType": "SWAP", "instId": inst_id},
    )
    return float(d["data"][0]["ctVal"])


def _okx_liquidations(base: str, as_of: datetime, is_live: bool) -> dict:
    """Aggregate OKX's recent liquidation-orders feed into a real, sourced total.

    OKX only exposes a *recent* window of filled liquidations, so this is a live
    signal: for a backtest date whose day is outside that window we raise, and
    the caller degrades with a notice rather than inventing a figure.
    """
    inst_family = f"{base}-USDT"
    inst_id = f"{base}-USDT-SWAP"
    resp = _get_json(
        f"{_OKX_URL}/api/v5/public/liquidation-orders",
        {
            "instType": "SWAP",
            "instFamily": inst_family,
            "state": "filled",
            "limit": _LIQ_LIMIT,
        },
    )
    blocks = resp.get("data") or []
    details = []
    for b in blocks:
        details.extend(b.get("details") or [])
    if not details:
        raise NoMarketDataError(base, inst_id, "no recent OKX liquidations")

    day_end_ms = _end_of_day_ms(as_of)
    day_start_ms = int(
        as_of.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
    )
    # For a backtest, keep only liquidations on the requested UTC day; live keeps all.
    if not is_live:
        details = [d for d in details if day_start_ms <= int(d["ts"]) <= day_end_ms]
        if not details:
            raise NoMarketDataError(
                base, inst_id, "OKX liquidation feed does not reach that date"
            )

    ctval = _okx_ctval(inst_id)
    long_usd = short_usd = 0.0
    long_n = short_n = 0
    tss = []
    for d in details:
        px = float(d["bkPx"])
        sz = float(d["sz"])
        notional = px * sz * ctval
        tss.append(int(d["ts"]))
        # posSide is the side being liquidated: a long liquidation is a forced sell.
        if d.get("posSide") == "long":
            long_usd += notional
            long_n += 1
        else:
            short_usd += notional
            short_n += 1
    span_lo = datetime.fromtimestamp(min(tss) / 1000, tz=timezone.utc)
    span_hi = datetime.fromtimestamp(max(tss) / 1000, tz=timezone.utc)
    return {
        "total_usd": long_usd + short_usd,
        "long_usd": long_usd,
        "short_usd": short_usd,
        "long_n": long_n,
        "short_n": short_n,
        "count": len(details),
        "span_lo": span_lo,
        "span_hi": span_hi,
        "span_hours": (max(tss) - min(tss)) / 3_600_000,
    }


# --------------------------------------------------------------- assembly ------
def _sign_text(rate: float) -> str:
    # long/short mantidos em inglês (vocabulário corrente do público degen);
    # só o texto descritivo em volta é pt-BR.
    return "longs pagam shorts — multidão long" if rate >= 0 else \
        "shorts pagam longs — multidão short"


def _funding_line(base, as_of, is_live) -> str:
    """Funding — Hyperliquid primary (live ctx or funding history), Binance fallback.

    The raw API rate is printed verbatim (``funding_raw``); the %/hr and %/yr are
    faithful transforms of it, the annualized one marked ``~`` since it compounds
    a single print forward.
    """
    try:
        hl = _hl_ctx(base) if is_live else _hl_funding_history(base, as_of)
        stamp = "" if is_live else f" @ {hl['as_of'].strftime('%Y-%m-%d %H:%MZ')}"
        return (
            f"- **Taxa de financiamento** (funding — quem está long paga p/ segurar a "
            f"posição; Hyperliquid perp, 1h{stamp}): {hl['funding_raw']} "
            f"= {hl['funding_hourly'] * 100:+.5f}%/hr → ~{hl['funding_annual_pct']:+.1f}%/ano. "
            f"{_sign_text(hl['funding_hourly'])}."
        )
    except Exception as exc:  # noqa: BLE001 — degrade to fallback, never fabricate
        logger.warning("Hyperliquid funding unavailable for %s: %s", base, exc)
    try:
        bf = _binance_funding(base, as_of, is_live)
        stamp = bf["as_of"].strftime("%Y-%m-%d %H:%MZ")
        return (
            f"- **Taxa de financiamento** (funding — quem está long paga p/ segurar a "
            f"posição; Binance perp fallback, 8h @ {stamp}): {bf['funding_raw']} "
            f"= {bf['funding_8h'] * 100:+.5f}%/8h → ~{bf['funding_annual_pct']:+.1f}%/ano. "
            f"{_sign_text(bf['funding_8h'])}."
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Binance funding unavailable for %s: %s", base, exc)
        return f"- **Taxa de financiamento** (funding): indisponível ({type(exc).__name__}); sem valor reportado."


def _oi_line(base, as_of, is_live) -> str:
    """Open interest — Hyperliquid primary (live), Binance fallback / history.

    ``oi_raw`` is the exact coin figure the API returned; the USD notional is a
    derived approximation, marked ``≈``.
    """
    if is_live:
        try:
            hl = _hl_ctx(base)
            return (
                f"- **Contratos em aberto** (open interest — capital alavancado "
                f"no book; Hyperliquid perp): {hl['oi_raw']} {base} "
                f"(≈ {_fmt_usd(hl['oi_usd'])} ao preço de marca {hl['mark_raw']})."
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Hyperliquid OI unavailable for %s: %s", base, exc)
    try:
        oi = _binance_oi(base, as_of, is_live)
        src = "Binance perp" if is_live else "Binance perp fallback"
        return (
            f"- **Contratos em aberto** (open interest — capital alavancado "
            f"no book; {src}): {oi['oi_raw']} {base} "
            f"(≈ {_fmt_usd(oi['oi_usd'])})."
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Binance OI unavailable for %s: %s", base, exc)
        note = "" if is_live else " (Hyperliquid não tem OI histórico; Binance retém ~30d)"
        return f"- **Contratos em aberto** (open interest): indisponível ({type(exc).__name__}){note}; sem valor reportado."


def _liq_line(base, as_of, is_live) -> str:
    try:
        lq = _okx_liquidations(base, as_of, is_live)
        window = (
            f"~{lq['span_hours']:.0f}h" if lq["span_hours"] >= 1
            else f"~{lq['span_hours'] * 60:.0f}min"
        )
        bias = (
            "dominado por longs — posições long liquidadas à "
            "força, lavagem em cascata (long flush)"
            if lq["long_usd"] > lq["short_usd"]
            else "dominado por shorts — posições short liquidadas "
            "à força, estrangulamento de baixa (short squeeze)"
        )
        # OKX carries liquidations because neither Hyperliquid's /info nor
        # Binance expose an aggregate liquidation feed without an API key.
        return (
            f"- **Liquidações** (liquidations — posições fechadas à força; OKX SWAP — HL/Binance "
            f"não têm feed de liquidação sem chave; últimas {lq['count']} ordens em "
            f"{window} até {lq['span_hi'].strftime('%Y-%m-%d %H:%MZ')}): "
            f"{_fmt_usd(lq['total_usd'])} no total — "
            f"longs {_fmt_usd(lq['long_usd'])} ({lq['long_n']}) / "
            f"shorts {_fmt_usd(lq['short_usd'])} ({lq['short_n']}). {bias}."
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("OKX liquidations unavailable for %s: %s", base, exc)
        note = "" if is_live else " (o feed é só ao vivo/recente, sem histórico p/ datas de backtest)"
        return f"- **Liquidações** (liquidations): indisponível ({type(exc).__name__}){note}; sem valor reportado."


def _mark_line(base, is_live) -> str:
    if not is_live:
        return ""
    try:
        hl = _hl_ctx(base)
        return (
            f"- **Preço de marcação / volume 24h** (mark price — preço de referência do perp; Hyperliquid): "
            f"{hl['mark_raw']} / ≈ {_fmt_usd(hl['day_ntl_vlm'])}."
        )
    except Exception:  # noqa: BLE001 — flavour line, drop silently
        return ""


def get_crypto_derivatives(symbol: str, curr_date: str) -> str:
    """Funding, open interest and liquidations for a crypto asset, real + sourced.

    ``symbol`` is any crypto form the pipeline holds (``BTC-USD``, ``BTCUSD``,
    ``BTC-USDT``); non-crypto symbols raise :class:`NoMarketDataError` so the
    router emits one clear "unavailable" signal instead of a fabricated report.
    """
    base = crypto_base(symbol)
    if not base:
        raise NoMarketDataError(symbol, None, "not a recognized crypto asset")

    as_of = _parse_date(curr_date)
    today = datetime.now(timezone.utc).date()
    is_live = as_of.date() >= today
    stale_days = (today - as_of.date()).days

    mode = "ao vivo" if is_live else f"backtest, {as_of.date()}"
    lines = [
        f"## Derivativos Cripto — {base} (em {as_of.date()}, {mode})",
        "",
        _funding_line(base, as_of, is_live),
        _oi_line(base, as_of, is_live),
        _liq_line(base, as_of, is_live),
    ]
    mark = _mark_line(base, is_live)
    if mark:
        lines.append(mark)

    if not is_live and stale_days > _HIST_WINDOW_DAYS:
        lines.append(
            f"- _Nota: {as_of.date()} foi há {stale_days}d; os feeds de OI e "
            f"liquidações só retêm ~{_HIST_WINDOW_DAYS}d, então esses degradam acima._"
        )
    lines.append("")
    lines.append(
        "_Fontes (todas sem chave): Hyperliquid (endpoint `info`), Binance USDⓈ-M "
        "(`fapi`), OKX (`liquidation-orders`). Uma fonte que falhou é marcada "
        "como indisponível na própria linha — nenhum valor de derivativo é jamais inventado._"
    )
    return "\n".join(lines)

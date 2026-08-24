"""Crypto network-context data — on-chain, spot-ETF flows, Fear & Greed (fork addition).

Three signals the equity-shaped pipeline is blind to but that the modeled
decision process (the ``@ericksekiama`` corpus, cross-checked by a second
independent modeling of the same channel: on-chain appears in 20 of 59 videos,
spot-ETF flow in 17, Fear & Greed in 5) treats as first-class for a crypto call:

    signal          keyless public source(s)                         backtest?
    -------------   ----------------------------------------------   ---------
    on-chain        mempool.space (hashrate / difficulty / halving), no*
                    blockchain.info (hashrate fallback), CoinGecko
                    (``/global`` dominance, stablecoins market cap)
    ETF flows       Farside Investors (daily net US$m, BTC & ETH)    yes
    Fear & Greed    alternative.me ``/fng`` (crypto-wide index)      yes

    * the keyless on-chain feeds are *now-only*; a past date declares them
      unavailable rather than borrowing today's number (see the no-look-ahead
      rule below).

Design rules honored — the same contract the derivatives vendor already keeps:

* **Crypto only** — a non-crypto symbol raises :class:`NoMarketDataError` so the
  router emits one clear "unavailable" instead of a fabricated report; equities
  never see these sections.
* **No look-ahead** — Fear & Greed and ETF flows carry real history and are
  clamped to the requested day (the newest observation on/before ``curr_date``).
  The on-chain live feeds (hashrate, dominance, stablecoin cap, halving
  countdown) only expose "now" and have no keyless history, so for a past date
  they degrade with an explicit notice — never today's value on a backtest date.
* **Exact values** — the API figure is printed verbatim; only clearly-derived
  quantities (halving ETA, human-readable magnitudes, aggregated sums) are
  marked ``~`` / ``≈``.
* **Never fabricate** — a source that is down, or a metric that only exists
  behind a paid key (MVRV / short-term-holder cost basis via
  Glassnode/CryptoQuant), is named and declared unavailable; it is never
  silently replaced by a proxy or an invented number.
* **pt-BR** — leading Portuguese term with the English original in parentheses
  on first use, matching the house translation rule.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import requests

from .errors import NoMarketDataError
from .symbol_utils import crypto_base

logger = logging.getLogger(__name__)

_TIMEOUT = 12
_MEMPOOL = "https://mempool.space/api"
_BLOCKCHAIN_INFO = "https://blockchain.info"
_COINGECKO = "https://api.coingecko.com/api/v3"
_FNG_URL = "https://api.alternative.me/fng/"
# Farside serves the same table the desk reads; a real browser UA is required or
# it answers 403. Only BTC and ETH have spot ETFs, so those are the only pages.
_FARSIDE = {
    "BTC": "https://farside.co.uk/btc/",
    "ETH": "https://farside.co.uk/eth/",
}
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

_HALVING_INTERVAL = 210_000      # blocks between Bitcoin halvings
_AVG_BLOCK_MIN = 10              # target block interval, minutes
# alternative.me publishes one reading per day since 2018; cap the history pull
# so a very old backtest date can't request an unbounded window.
_FNG_MAX_LIMIT = 3000

# Fear & Greed classification, English original -> pt-BR term.
_FNG_PT = {
    "Extreme Fear": "Medo Extremo",
    "Fear": "Medo",
    "Neutral": "Neutro",
    "Greed": "Ganância",
    "Extreme Greed": "Ganância Extrema",
}


# --------------------------------------------------------------------- http ----
def _get_json(url, params=None):
    r = requests.get(url, params=params, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _get_text(url):
    r = requests.get(url, headers={"User-Agent": _BROWSER_UA}, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.text


def _parse_date(curr_date) -> datetime:
    """Parse ``curr_date`` (yyyy-mm-dd) to a UTC-midnight datetime."""
    if isinstance(curr_date, datetime):
        return curr_date if curr_date.tzinfo else curr_date.replace(tzinfo=timezone.utc)
    dt = datetime.strptime(str(curr_date)[:10], "%Y-%m-%d")
    return dt.replace(tzinfo=timezone.utc)


def _fmt_usd(v: float) -> str:
    a = abs(v)
    if a >= 1e12:
        return f"${v / 1e12:.2f}T"
    if a >= 1e9:
        return f"${v / 1e9:.2f}B"
    if a >= 1e6:
        return f"${v / 1e6:.2f}M"
    if a >= 1e3:
        return f"${v / 1e3:.1f}K"
    return f"${v:,.0f}"


def _fmt_hashrate(h_s: float) -> str:
    """Human-readable hash rate from a raw hashes-per-second figure."""
    if h_s >= 1e18:
        return f"{h_s / 1e18:.2f} EH/s"
    if h_s >= 1e15:
        return f"{h_s / 1e15:.2f} PH/s"
    if h_s >= 1e12:
        return f"{h_s / 1e12:.2f} TH/s"
    return f"{h_s:,.0f} H/s"


def _is_live(as_of: datetime) -> bool:
    return as_of.date() >= datetime.now(timezone.utc).date()


# ============================================================ 1. on-chain ======
def _hashrate_line(base: str) -> str:
    if base != "BTC":
        return (
            f"- **Hashrate / dificuldade** (hashrate/difficulty — segurança da rede "
            f"proof-of-work): métrica específica do Bitcoin; não se aplica a {base}."
        )
    try:
        d = _get_json(f"{_MEMPOOL}/v1/mining/hashrate/3d")
        hr = float(d["currentHashrate"])
        diff = d["currentDifficulty"]
        return (
            f"- **Hashrate** (hashrate — poder de mineração da rede; mempool.space, "
            f"média 3d): {d['currentHashrate']} H/s ≈ {_fmt_hashrate(hr)}. "
            f"Dificuldade (difficulty): {diff}."
        )
    except Exception as exc:  # noqa: BLE001 — degrade to fallback, never fabricate
        logger.warning("mempool hashrate unavailable for %s: %s", base, exc)
    try:
        ghs = float(_get_text(f"{_BLOCKCHAIN_INFO}/q/hashrate").strip())
        return (
            f"- **Hashrate** (hashrate — poder de mineração da rede; blockchain.info "
            f"fallback): {ghs} GH/s ≈ {_fmt_hashrate(ghs * 1e9)}."
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("blockchain.info hashrate unavailable for %s: %s", base, exc)
        return (
            f"- **Hashrate** (hashrate): indisponível ({type(exc).__name__}); sem "
            f"valor reportado (fontes: mempool.space, blockchain.info)."
        )


def _halving_line(base: str) -> str:
    if base != "BTC":
        return (
            f"- **Halving** (halving): evento específico do Bitcoin; não se aplica a {base}."
        )
    try:
        height = int(_get_text(f"{_MEMPOOL}/blocks/tip/height").strip())
        epoch = height // _HALVING_INTERVAL
        next_halving = (epoch + 1) * _HALVING_INTERVAL
        remaining = next_halving - height
        eta_days = remaining * _AVG_BLOCK_MIN / 60 / 24
        return (
            f"- **Halving** (halving — corte do subsídio de bloco pela metade; "
            f"mempool.space): bloco atual {height}; próximo halving no bloco "
            f"{next_halving} (~{remaining} blocos, ≈ {eta_days:.0f} dias a ~10 min/bloco). "
            f"Já ocorreram {epoch} halvings."
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("mempool halving unavailable for %s: %s", base, exc)
        return (
            f"- **Halving** (halving): indisponível ({type(exc).__name__}); sem valor "
            f"reportado (fonte: mempool.space)."
        )


def _dominance_line(base: str) -> str:
    try:
        g = _get_json(f"{_COINGECKO}/global")["data"]
        pct = g.get("market_cap_percentage") or {}
        btc_dom = pct.get("btc")
        parts = []
        if btc_dom is not None:
            parts.append(f"dominância BTC (BTC dominance) {btc_dom:.2f}%")
        base_dom = pct.get(base.lower())
        if base.lower() != "btc" and base_dom is not None:
            parts.append(f"{base} {base_dom:.2f}%")
        total = g.get("total_market_cap", {}).get("usd")
        tail = f" Cap total do mercado cripto ≈ {_fmt_usd(total)}." if total else ""
        if not parts:
            raise ValueError("no dominance breakdown in payload")
        return (
            f"- **Dominância de mercado** (market dominance; CoinGecko `/global`): "
            + ", ".join(parts)
            + "."
            + tail
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("CoinGecko dominance unavailable for %s: %s", base, exc)
        return (
            f"- **Dominância de mercado** (market dominance): indisponível "
            f"({type(exc).__name__}); sem valor reportado (fonte: CoinGecko)."
        )


def _stablecoin_line() -> str:
    try:
        rows = _get_json(
            f"{_COINGECKO}/coins/markets",
            {
                "vs_currency": "usd",
                "category": "stablecoins",
                "order": "market_cap_desc",
                "per_page": 20,
                "page": 1,
            },
        )
        caps = [r["market_cap"] for r in rows if isinstance(r.get("market_cap"), (int, float))]
        if not caps:
            raise ValueError("no stablecoin market caps in payload")
        total = sum(caps)
        top = rows[0]
        return (
            f"- **Cap das stablecoins** (stablecoin market cap — pólvora seca, "
            f"liquidez à espera; CoinGecko, top {len(caps)}): ≈ {_fmt_usd(total)} "
            f"(maior: {str(top.get('symbol', '?')).upper()} {_fmt_usd(top['market_cap'])})."
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("CoinGecko stablecoin cap unavailable: %s", exc)
        return (
            f"- **Cap das stablecoins** (stablecoin market cap): indisponível "
            f"({type(exc).__name__}); sem valor reportado (fonte: CoinGecko)."
        )


def _mvrv_line() -> str:
    """MVRV / cost-basis is paid-key only — declare it, never proxy it (acceptance #6)."""
    return (
        "- **MVRV / preço médio do investidor de curto prazo** (MVRV / short-term "
        "holder cost basis — valuation on-chain): **indisponível sem chave paga** — "
        "só existe via Glassnode/CryptoQuant; não estimado nem substituído por proxy."
    )


def get_onchain_metrics(symbol: str, curr_date: str) -> str:
    """On-chain network context for a crypto asset — real, sourced, no look-ahead.

    Live: hashrate/difficulty and the halving countdown (Bitcoin) plus market
    dominance and the stablecoin market cap (CoinGecko). MVRV and the
    short-term-holder cost basis are declared unavailable (paid-key only), never
    fabricated. For a past ``curr_date`` the keyless live feeds have no history,
    so the section degrades with an explicit no-look-ahead notice instead of
    borrowing today's number.
    """
    base = crypto_base(symbol)
    if not base:
        raise NoMarketDataError(symbol, None, "not a recognized crypto asset")

    as_of = _parse_date(curr_date)
    is_live = _is_live(as_of)
    mode = "ao vivo" if is_live else f"backtest, {as_of.date()}"
    lines = [f"## On-chain — {base} (em {as_of.date()}, {mode})", ""]

    if not is_live:
        lines.append(
            f"- On-chain (on-chain): os indicadores aqui — hashrate, halving, "
            f"dominância, cap de stablecoins — vêm de feeds keyless que só expõem o "
            f"**estado atual** (mempool.space, CoinGecko); para {as_of.date()} (data "
            f"passada) são **indisponíveis** — não puxamos o valor de hoje, para não "
            f"vazar futuro (no look-ahead). Histórico on-chain exige chave paga."
        )
        lines.append(_mvrv_line())
    else:
        lines.append(_hashrate_line(base))
        lines.append(_halving_line(base))
        lines.append(_dominance_line(base))
        lines.append(_stablecoin_line())
        lines.append(_mvrv_line())

    lines.append("")
    lines.append(
        "_Fontes (sem chave): mempool.space, blockchain.info, CoinGecko. "
        "MVRV / cost-basis só via Glassnode/CryptoQuant (pagas) → declarados "
        "indisponíveis, nunca aproximados._"
    )
    return "\n".join(lines)


# ============================================================ 2. ETF flows =====
def _parse_flow_number(cell: str):
    """Parse a Farside flow cell to millions of USD, or None when it is blank.

    Farside prints negatives in parentheses — ``(53.6)`` — and groups thousands
    with commas; a dash means "no data that day". Anything that is not a clean
    number returns None so a layout change degrades loudly instead of coercing
    junk into a fabricated figure.
    """
    if cell is None:
        return None
    s = cell.strip()
    if s in ("", "-", "–", "—", "n/a", "N/A"):
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "").replace("$", "").strip()
    if not re.fullmatch(r"[+-]?\d+(\.\d+)?", s):
        return None
    value = float(s)
    return -value if negative else value


def _parse_farside_date(cell: str):
    try:
        return datetime.strptime(cell.strip(), "%d %b %Y").date()
    except (ValueError, AttributeError):
        return None


def _parse_farside_rows(html: str):
    """Return ``[(date, total_millions, raw_cell), ...]`` from a Farside flow page.

    Each data row is ``date | <one column per ETF> | Total``; the last cell is
    the daily net total in US$m. Rows whose first cell is not a date (headers,
    the cumulative ``Total`` footer) are skipped.
    """
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [
            re.sub(r"<[^>]*>", "", c).replace("\xa0", " ").strip()
            for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)
        ]
        if len(cells) < 2:
            continue
        day = _parse_farside_date(cells[0])
        if day is None:
            continue
        total = _parse_flow_number(cells[-1])
        if total is None:
            continue
        out.append((day, total, cells[-1].strip()))
    return out


def get_etf_flows(symbol: str, curr_date: str) -> str:
    """Spot-ETF daily net flow (US$m) for BTC/ETH from Farside — clamped to the day.

    Only Bitcoin and Ether have US spot ETFs; any other crypto returns an
    explicit "no spot ETF" line (declared absence, not a failure). The Farside
    table carries history, so a past ``curr_date`` selects the newest row on or
    before that day — real historical data, never a look-ahead. A layout change
    or an outage degrades with the named source, never a fabricated number.
    """
    base = crypto_base(symbol)
    if not base:
        raise NoMarketDataError(symbol, None, "not a recognized crypto asset")

    as_of = _parse_date(curr_date)
    header = f"## Fluxo de ETF spot (spot-ETF flow) — {base} (em {as_of.date()})"

    if base not in _FARSIDE:
        return (
            f"{header}\n\n- Não há ETF spot (spot ETF) para {base} — o produto só "
            f"existe para BTC e ETH; ausência declarada, não falha.\n\n"
            f"_Fonte (sem chave): Farside Investors._"
        )

    try:
        rows = _parse_farside_rows(_get_text(_FARSIDE[base]))
        eligible = [r for r in rows if r[0] <= as_of.date()]
        if not eligible:
            raise NoMarketDataError(
                base, None, f"Farside has no ETF flow row on/before {as_of.date()}"
            )
        day, total, raw = max(eligible, key=lambda r: r[0])
        if total > 0:
            direction = "entrada líquida (net inflow)"
        elif total < 0:
            direction = "saída líquida (net outflow)"
        else:
            direction = "fluxo neutro (flat)"
        line = (
            f"- **Fluxo de ETF spot** (spot-ETF flow — fluxo líquido diário dos ETFs "
            f"à vista, em US$ milhões; Farside Investors): {raw} em {day} — {direction}."
        )
    except Exception as exc:  # noqa: BLE001 — named source, never fabricate
        logger.warning("Farside ETF flow unavailable for %s: %s", base, exc)
        line = (
            f"- **Fluxo de ETF spot** (spot-ETF flow): indisponível "
            f"({type(exc).__name__}); sem valor reportado (fonte: Farside Investors)."
        )

    return f"{header}\n\n{line}\n\n_Fonte (sem chave): Farside Investors (US$ milhões)._"


# ============================================================ 3. Fear & Greed ==
def _fng_pt(classification: str) -> str:
    return _FNG_PT.get(classification, classification)


def get_fear_greed(symbol: str, curr_date: str) -> str:
    """Crypto Fear & Greed index (alternative.me) — clamped to ``curr_date``.

    The index has full daily history, so a past date selects the newest reading
    on or before it (never a look-ahead). A live date reads the current value.
    An outage degrades with the named source rather than an invented figure.
    """
    base = crypto_base(symbol)
    if not base:
        raise NoMarketDataError(symbol, None, "not a recognized crypto asset")

    as_of = _parse_date(curr_date)
    header = f"## Medo & Ganância (Fear & Greed) — sentimento cripto (em {as_of.date()})"

    try:
        if _is_live(as_of):
            limit = 2
        else:
            days = (datetime.now(timezone.utc).date() - as_of.date()).days + 3
            limit = max(2, min(days, _FNG_MAX_LIMIT))
        data = _get_json(_FNG_URL, {"limit": limit}).get("data") or []
        # Entries are newest-first; the first one dated on/before as_of is the
        # correct reading for the requested day.
        chosen = None
        for entry in data:
            edate = datetime.fromtimestamp(int(entry["timestamp"]), tz=timezone.utc).date()
            if edate <= as_of.date():
                chosen = (entry, edate)
                break
        if chosen is None:
            raise NoMarketDataError(base, None, f"no Fear & Greed reading on/before {as_of.date()}")
        entry, edate = chosen
        value = entry["value"]
        cls = entry.get("value_classification", "")
        line = (
            f"- **Medo & Ganância** (Fear & Greed — índice de sentimento do mercado "
            f"cripto, 0 = medo extremo … 100 = ganância extrema; alternative.me): "
            f"{value}/100 — {_fng_pt(cls)} ({cls}) em {edate}."
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Fear & Greed unavailable for %s: %s", base, exc)
        line = (
            f"- **Medo & Ganância** (Fear & Greed): indisponível "
            f"({type(exc).__name__}); sem valor reportado (fonte: alternative.me)."
        )

    return f"{header}\n\n{line}\n\n_Fonte (sem chave): alternative.me `/fng`._"

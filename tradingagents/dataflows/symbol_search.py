"""Keyless symbol search + company-name resolution (Yahoo Finance search).

Two jobs, one keyless source (``query1.finance.yahoo.com/v1/finance/search``):

* **name resolution** — a symbol -> its display name ("MSFT" -> "Microsoft
  Corporation", "BTC-USD" -> "Bitcoin USD"), so the UI can show ``TICKER ( Nome )``;
* **search / disambiguation** — a term (name OR ticker) -> candidate symbols
  ("Microsoft" -> MSFT, "Bitcoin" -> BTC-USD, "PBR" -> PBR), for the ticker field's
  autocomplete and for resolving a typed name before an analysis runs.

Honesty + resilience (fork brief 25/08, DA-058):

* **Cached — names are stable.** A resolved name and a search term's results are
  cached to disk (one JSON map each); only *non-empty* results are cached, so a
  transient Yahoo outage never poisons the cache with a permanent blank.
* **Never blocks, never invents.** Every network path is fail-open: a Yahoo hiccup
  yields ``None`` / ``[]`` and the caller falls back to the bare ticker — a name is
  never fabricated and the analysis is never held up.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading

from .config import get_config
from .symbol_utils import normalize_symbol

logger = logging.getLogger(__name__)

# One JSON map per concern, in the shared data cache dir. Names/searches are
# stable enough to keep forever; a threading lock guards the read-modify-write
# because the web server is multi-threaded.
_NAMES_FILE = "yahoo-names.json"      # SYMBOL(upper) -> display name
_SEARCH_FILE = "yahoo-search.json"    # term(lower)  -> [{symbol,name,type,exchange}]
_LOCK = threading.Lock()

# A plain ticker the user typed exactly (AAPL, BTC-USD, 0700.HK, GC=F) — resolution
# is skipped for these so a direct symbol never triggers a network round-trip and is
# never hijacked into a different symbol. A name ("Microsoft", "Bitcoin", "Apple")
# does not match, so it is sent to search and resolved to its ticker.
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,6}([.\-=^][A-Z0-9]{1,6})*$")


def looks_like_symbol(term: str) -> bool:
    """True when ``term`` is already a plain ticker (so name-resolution is skipped)."""
    t = (term or "").strip()
    if not t or " " in t:
        return False
    return bool(_SYMBOL_RE.match(t))


def _cache_path(fname: str) -> str:
    cache_dir = get_config()["data_cache_dir"]
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, fname)


def _load_map(fname: str) -> dict:
    path = _cache_path(fname)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — a corrupt cache is a miss, not a crash
        return {}


def _save_map(fname: str, data: dict) -> None:
    try:
        with open(_cache_path(fname), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:  # noqa: BLE001 — cache is best-effort
        pass


def _yahoo_search(term: str, count: int = 8) -> list[dict]:
    """Raw keyless Yahoo search quotes for ``term`` — the single network seam.

    Uses ``yfinance.Search`` (not a bare HTTP GET) so the cookie/crumb session the
    daily path already relies on is reused: Yahoo's raw ``/v1/finance/search`` 429s a
    keyless client under load, while the yfinance session negotiates it.
    """
    import yfinance as yf

    return yf.Search(term, max_results=count).quotes or []


def _quote_name(q: dict) -> str:
    """Compact display name — Yahoo's ``shortname`` (e.g. 'CDW Corp') preferred over
    the longer ``longname``, matching the reference format ``TICKER ( Nome )``."""
    return (q.get("shortname") or q.get("longname") or "").strip()


def _clean(q: dict) -> dict | None:
    sym = (q.get("symbol") or "").strip()
    if not sym:
        return None
    return {
        "symbol": sym,
        "name": _quote_name(q),
        "type": q.get("quoteType") or "",
        "exchange": q.get("exchange") or "",
    }


def search_symbols(term: str, limit: int = 8) -> list[dict]:
    """Candidate symbols for a name-or-ticker ``term`` (cached, fail-open).

    Returns ``[{symbol, name, type, exchange}, …]`` newest-relevance first, or an
    empty list when the term is blank or Yahoo is unreachable — never raises.
    """
    term = (term or "").strip()
    if not term:
        return []
    key = term.lower()
    with _LOCK:
        cached = _load_map(_SEARCH_FILE).get(key)
    if cached is not None:
        return cached[:limit]

    try:
        quotes = _yahoo_search(term, max(limit, 8))
    except Exception as exc:  # noqa: BLE001 — Yahoo down -> no suggestions, no crash
        logger.info("yahoo search failed for %r: %s", term, exc)
        return []

    cleaned = [c for c in (_clean(q) for q in quotes) if c and c["name"]]
    if cleaned:  # only cache a real answer — never poison with a transient blank
        with _LOCK:
            data = _load_map(_SEARCH_FILE)
            data[key] = cleaned
            _save_map(_SEARCH_FILE, data)
    return cleaned[:limit]


def resolve_name(symbol: str) -> str | None:
    """Display name for a ``symbol`` (e.g. 'Microsoft Corporation'), cached; ``None``
    when unknown or Yahoo is down so the caller shows the bare ticker."""
    if not symbol:
        return None
    canonical = normalize_symbol(symbol)
    key = canonical.upper()
    with _LOCK:
        cached = _load_map(_NAMES_FILE).get(key)
    if cached:
        return cached

    name = None
    for r in search_symbols(canonical, limit=10):
        if r["symbol"].upper() == key:
            name = r["name"]
            break
    if name:  # cache only a real hit; a miss/outage retries next time
        with _LOCK:
            data = _load_map(_NAMES_FILE)
            data[key] = name
            _save_map(_NAMES_FILE, data)
    return name or None


def resolve_names(symbols: list[str]) -> dict[str, str]:
    """Batch symbol -> name for the history chips (each cached individually)."""
    out: dict[str, str] = {}
    for s in symbols:
        if not s:
            continue
        name = resolve_name(s)
        if name:
            out[s.upper()] = name
    return out


def resolve_query_to_symbol(term: str) -> str | None:
    """A typed name-or-ticker ``term`` -> the symbol to analyse (fail-open).

    An exact symbol match is returned as-is (a direct ticker is never hijacked);
    otherwise the top search hit's symbol is used ("Microsoft" -> MSFT). ``None``
    when nothing matches or Yahoo is down, so the caller keeps the raw term.
    """
    term = (term or "").strip()
    if not term:
        return None
    results = search_symbols(term, limit=10)
    if not results:
        return None
    for r in results:
        if r["symbol"].upper() == term.upper():
            return r["symbol"]
    return results[0]["symbol"]

"""Disk cache + hit/network metrics for TradingAgents data governance.

Principle (DA-058): *historical data is immutable — fetch it once, keep it
forever; only the current day expires.* A request whose window ends **before
today** is served from disk forever; a window that reaches today expires at the
next local midnight; a known failure is cached briefly so a dead source is not
hammered (e.g. the Reddit 429 storm).

This module is **first-class code inside the fork** (`tradingagents.datacache`),
activated from ``tradingagents/__init__.py`` via :func:`hook.install`. It is pure
stdlib and imports nothing from the rest of ``tradingagents`` — so importing it
never drags in the heavy langchain/langgraph stack. The cache *data* still lives
under ``~/.tradingagents/datacache`` (user data, not source), overridable with
``TA_DATACACHE_DIR``.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta

HOME = os.path.expanduser("~/.tradingagents")
CACHE_DIR = os.environ.get("TA_DATACACHE_DIR", os.path.join(HOME, "datacache"))
# How long a *negative* entry (failure / empty / rate-limit) survives. "a few
# hours": long enough to kill a retry storm within and across same-day runs,
# short enough that a transient outage self-heals. Successful historical data is
# never expired.
NEG_TTL = int(os.environ.get("TA_DATACACHE_NEG_TTL", str(3 * 3600)))
DISABLED = os.environ.get("TA_DATACACHE_DISABLE", "").strip().lower() in ("1", "true", "yes")

# Match an ISO date embedded anywhere in an argument, not glued to other digits.
_DATE_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")

_lock = threading.Lock()
_metrics: dict[str, dict[str, int]] = {}  # category -> {"hit","net","neg_hit"}


def _now() -> float:
    return time.time()


# ---------------------------------------------------------------- date / TTL --
def _max_date_in(args, kwargs):
    """Largest ISO date found across the call arguments, or None."""
    best = None

    def scan(v):
        nonlocal best
        if isinstance(v, str):
            for y, mo, d in _DATE_RE.findall(v):
                try:
                    dt = datetime(int(y), int(mo), int(d)).date()
                except ValueError:
                    continue
                if best is None or dt > best:
                    best = dt

    for v in args:
        scan(v)
    for v in (kwargs or {}).values():
        scan(v)
    return best


def classify(args, kwargs):
    """'permanent' if the request window ends before today, else 'volatile'.

    No date in the arguments (e.g. insider transactions, prediction markets,
    live social) is treated as volatile — it reflects "now" and must expire.
    """
    md = _max_date_in(args, kwargs)
    if md is not None and md < datetime.now().date():
        return "permanent"
    return "volatile"


def _end_of_day_epoch() -> float:
    tomorrow = (datetime.now() + timedelta(days=1)).date()
    return datetime.combine(tomorrow, datetime.min.time()).timestamp()


# ---------------------------------------------------------------- key / paths --
def _stable(part) -> str:
    try:
        return json.dumps(part, sort_keys=True, default=str)
    except Exception:
        return repr(part)


def key(*parts) -> str:
    raw = "\x1f".join(_stable(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


# Backwards-friendly alias (used internally).
_key = key


def _paths(category, k):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(category))[:64] or "misc"
    d = os.path.join(CACHE_DIR, safe)
    return d, os.path.join(d, k + ".json")


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write(path, dirpath, entry):
    try:
        os.makedirs(dirpath, exist_ok=True)
        tmp = f"{path}.tmp{os.getpid()}.{threading.get_ident()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entry, f)
        os.replace(tmp, path)
    except OSError:
        pass


# ---------------------------------------------------------------- get / set ---
def get(category, k):
    """Return the live cache entry, or None on a miss (expired counts as miss)."""
    if DISABLED:
        return None
    _, path = _paths(category, k)
    e = _read(path)
    if e is None:
        return None
    exp = e.get("expires")
    if exp is not None and _now() >= exp:
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    return e


def set_ok(category, k, value, permanent):
    if DISABLED:
        return
    d, path = _paths(category, k)
    _write(path, d, {
        "kind": "ok",
        "created": _now(),
        "expires": None if permanent else _end_of_day_epoch(),
        "permanent": bool(permanent),
        "value": value,
    })


def set_neg(category, k, *, value=None, error=None):
    if DISABLED:
        return
    d, path = _paths(category, k)
    _write(path, d, {
        "kind": "neg",
        "created": _now(),
        "expires": _now() + NEG_TTL,
        "permanent": False,
        "value": value,
        "error": error,
    })


# ---------------------------------------------------------------- purge -------
def purge_category(category) -> int:
    """Apaga TODA entrada de uma categoria e devolve quantas foram. 0 se não existe.

    Serve à disciplina do ``_SEMANTICA_KEY``: bump da versão torna as entradas
    antigas INALCANÇÁVEIS (a chave é um hash — nada as encontra), mas elas ficam no
    disco pra sempre, e as permanentes nunca expiram. Como a chave não guarda a
    versão em claro, não dá pra apagar só as órfãs: apaga-se a categoria inteira. O
    custo é re-buscar o que ainda valia; o benefício é o disco não acumular resposta
    envenenada de uma semântica que já morreu. Idempotente e fail-open — cache é
    otimização, e falhar aqui nunca pode derrubar quem chamou.
    """
    d, _ = _paths(category, "x")
    n = 0
    try:
        nomes = os.listdir(d)
    except OSError:
        return 0
    for nome in nomes:
        if not nome.endswith(".json"):
            continue
        try:
            os.remove(os.path.join(d, nome))
            n += 1
        except OSError:
            pass
    return n


# ---------------------------------------------------------------- metrics -----
def _bump(category, field):
    with _lock:
        m = _metrics.setdefault(category, {"hit": 0, "net": 0, "neg_hit": 0})
        m[field] += 1


def record_hit(category, negative=False):
    _bump(category, "neg_hit" if negative else "hit")


def record_net(category):
    _bump(category, "net")


def snapshot():
    with _lock:
        return {c: dict(m) for c, m in _metrics.items()}


def summary_text():
    with _lock:
        if not _metrics:
            return ""
        rows = sorted(_metrics.items())
        th = tn = tg = 0
        lines = [
            "",
            "── TradingAgents data cache — served from cache × network ──",
            f"{'source':<24}{'cache':>7}{'net':>7}{'neg⤳':>7}",
        ]
        for cat, m in rows:
            th += m["hit"]
            tn += m["net"]
            tg += m["neg_hit"]
            lines.append(f"{cat:<24}{m['hit']:>7}{m['net']:>7}{m['neg_hit']:>7}")
        total = th + tn + tg
        served = th + tg
        rate = (100 * served / total) if total else 0.0
        lines.append(f"{'TOTAL':<24}{th:>7}{tn:>7}{tg:>7}")
        lines.append(
            f"cache-hit: {rate:.0f}%  ({served} from cache, {tn} network calls)"
        )
        lines.append("────────────────────────────────────────────────────────")
        return "\n".join(lines)


def print_summary():
    text = summary_text()
    if text:
        print(text, file=sys.stderr, flush=True)


# Every process that fetched anything prints the tally when it ends.
atexit.register(print_summary)

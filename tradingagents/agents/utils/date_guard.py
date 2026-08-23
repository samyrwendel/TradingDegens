"""Anti look-ahead date guard for date-taking data tools (fork addition).

A backtest at a past date must never fetch data dated *after* that date — if it
does, the run "sees the future" and every result it produces is invalid. Upstream
already filters some sources at the vendor layer (e.g. the yfinance news window),
but nothing stops an analyst LLM from passing ``end_date`` / ``curr_date`` =
*today* when the analysis date is months in the past. That silent look-ahead is
what makes an un-guarded backtest worthless.

This module pins a per-run **base date** (the analysis / trade date) in a
``ContextVar`` set once around the graph run (see
``TradingAgentsGraph._run_graph``), and gives tools a ``@guard_dates(...)``
decorator that clamps any named date argument down to that base. An LLM asking
for data "up to today" during a 2021 backtest is transparently corrected to 2021.

Concept ported — not copied — from ``TheLocalLab/TradingAgents-GUI@40e97f34``
``tradingagents/agents/utils/tool_guard.py``: their ``normalize_date`` accepts a
``today`` base and tidies loose/empty date strings, but leaves *enforcement* of
that base to the caller. Our design pushes the base into a ContextVar and makes
clamping automatic per tool, so no analyst prompt can opt out of it.

Live runs (``trade_date`` == today) set the base to today, so real "latest"
requests are unaffected. When no base is set (tool used outside a graph run) the
guard is a transparent no-op.
"""
from __future__ import annotations

import contextlib
import contextvars
import functools
import inspect
import logging
import re
from datetime import date, datetime

logger = logging.getLogger(__name__)

# The analysis "now" for the current run. None outside a run -> guard no-ops.
_base_date: contextvars.ContextVar[date | None] = contextvars.ContextVar(
    "ta_base_date", default=None
)

_ISO_RE = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*$")
_LOOSE_RE = re.compile(r"^\s*(\d{4})[/.](\d{1,2})[/.](\d{1,2})\s*$")
_EMPTYISH = {"", "none", "null", "today", "now"}


def _to_date(value) -> date | None:
    """Best-effort parse of ``value`` to a ``date``; None if unparseable/empty."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        value = str(value)
    m = _ISO_RE.match(value) or _LOOSE_RE.match(value)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


# ------------------------------------------------------------------ base date --
def set_base_date(value) -> contextvars.Token:
    """Pin the run's base (analysis) date; returns a token for :func:`reset`."""
    return _base_date.set(_to_date(value))


def reset_base_date(token: contextvars.Token) -> None:
    _base_date.reset(token)


def get_base_date() -> date | None:
    return _base_date.get()


@contextlib.contextmanager
def base_date(value):
    """Context manager pinning the base date for the duration of a run."""
    token = set_base_date(value)
    try:
        yield
    finally:
        reset_base_date(token)


# --------------------------------------------------------------------- clamp ---
def clamp(value):
    """Return ``value`` clamped so it never exceeds the current base date.

    - No base set → value returned unchanged (transparent no-op).
    - Unparseable value → returned unchanged (let the vendor surface the error).
    - Parseable and later than base → returned as the base date (yyyy-mm-dd).
    - Otherwise → returned unchanged.
    """
    base = get_base_date()
    if base is None:
        return value
    parsed = _to_date(value)
    if parsed is None:
        return value
    if parsed > base:
        clamped = base.isoformat()
        logger.warning(
            "date_guard: clamped look-ahead date %r to base date %s",
            value, clamped,
        )
        return clamped
    return value


def guard_dates(*param_names: str):
    """Decorator: clamp the named date arguments to the run's base date.

    Applied to the raw tool function *under* ``@tool``; ``functools.wraps``
    preserves the signature/annotations so LangChain's tool schema is unchanged
    (the LLM still sees the same parameters). Both positional and keyword forms
    are handled.
    """

    def deco(fn):
        try:
            positions = list(inspect.signature(fn).parameters)
        except (TypeError, ValueError):
            positions = []

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if get_base_date() is None:
                return fn(*args, **kwargs)
            args = list(args)
            for name in param_names:
                if name in kwargs:
                    if kwargs[name] is not None:
                        kwargs[name] = clamp(kwargs[name])
                    continue
                try:
                    idx = positions.index(name)
                except ValueError:
                    continue
                if idx < len(args) and args[idx] is not None:
                    args[idx] = clamp(args[idx])
            return fn(*args, **kwargs)

        return wrapper

    return deco

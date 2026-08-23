"""``tradingagents.datacache`` — data-governance cache for the fork.

Historical data is immutable: fetch once, keep forever; only the current day
expires (DA-058). Formerly an out-of-tree patch loaded via a venv ``.pth``; in
this fork it is first-class in-repo code, activated from
``tradingagents/__init__.py`` by calling :func:`install`. The cache still hooks
the (upstream) data modules *in memory* at import time, so a ``git pull`` from
``origin`` that changes their source never breaks it.

Disable at runtime with ``TA_DATACACHE_DISABLE=1`` to run vanilla upstream
behaviour. Point the on-disk store elsewhere with ``TA_DATACACHE_DIR``.
"""
from __future__ import annotations

from . import cache
from .hook import install

# Convenience re-exports for scripts that want to print/inspect the tally.
metrics_summary = cache.summary_text
print_metrics = cache.print_summary
snapshot = cache.snapshot

__all__ = ["install", "metrics_summary", "print_metrics", "snapshot", "cache"]

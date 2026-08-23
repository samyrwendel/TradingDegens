"""Post-import hook that applies the cache patches when TradingAgents modules load.

We must patch *after* each target module executes (its functions must exist) but
*without* eagerly importing the heavy tradingagents/langchain stack. A meta-path
finder gives us exactly that: for a target module we let the normal machinery
build the spec, then wrap its ``exec_module`` so our patch runs right after the
module body.

:func:`install` is called from ``tradingagents/__init__.py`` — so the hook is in
place before any ``tradingagents.dataflows.*`` submodule is imported, and it also
sweeps anything already loaded via :func:`patch.apply_to_loaded`.

Fail-open by construction: any error in finding, wrapping, or patching is
swallowed so a bug here can never block an import. Worst case, TradingAgents
runs uncached.
"""
from __future__ import annotations

import importlib.util
import logging
import sys

from . import cache, patch

logger = logging.getLogger("ta_datacache")

_INSTALLED = False


class _PatchFinder:
    """A meta-path finder that post-processes target modules after they exec."""

    def __init__(self):
        self._busy = set()

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in patch.TARGETS:
            return None
        if fullname in self._busy:
            # Re-entrant call from our own find_spec below — let the real
            # finders handle it.
            return None
        self._busy.add(fullname)
        try:
            spec = importlib.util.find_spec(fullname)
        except Exception:  # noqa: BLE001
            return None
        finally:
            self._busy.discard(fullname)

        if spec is None or spec.loader is None:
            return None
        loader = spec.loader
        if getattr(loader, "_ta_datacache_hooked", False):
            return spec
        orig_exec = getattr(loader, "exec_module", None)
        if orig_exec is None:
            return None

        def exec_module(module, _orig=orig_exec, _name=fullname):
            _orig(module)
            try:
                patch.apply(_name, module)
            except Exception as exc:  # noqa: BLE001
                logger.warning("ta_datacache: post-import patch %s failed: %s", _name, exc)

        try:
            loader.exec_module = exec_module  # type: ignore[method-assign]
            loader._ta_datacache_hooked = True
        except Exception:  # noqa: BLE001
            return spec
        return spec


def install():
    """Idempotently install the finder and patch anything already imported.

    Fully skipped (fast no-op) when ``TA_DATACACHE_DISABLE`` is set, so a user
    can run the fork with the vanilla upstream behaviour for A/B comparison.
    """
    global _INSTALLED
    if _INSTALLED or cache.DISABLED:
        return
    try:
        sys.meta_path.insert(0, _PatchFinder())
        _INSTALLED = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("ta_datacache: could not install import hook: %s", exc)
        return
    # Cover the case where a target module was imported before us.
    patch.apply_to_loaded()

"""On-disk descriptors for runs that are *in flight* — the seam that lets a
restart survive a mid-run kill.

The history :class:`~tradingagents.webui.store.HistoryStore` only records a run
when it *finishes*; a run killed mid-flight (a deploy `systemctl restart`, an
OOM, a power loss) leaves nothing there. This store fills that gap: the runner
drops a small JSON *descriptor* here the instant a run starts and deletes it the
instant the run reaches a terminal state. So whatever descriptors remain on the
next boot are exactly the runs that were interrupted — the resume queue.

A descriptor carries only what is needed to RE-RUN the analysis (ticker, date,
timeframe, method, analyst selection, asset type) plus non-secret LLM overrides
(provider/model/base_url). It NEVER holds an API key — a BYOK run's key lives
only in the browser and in the run's memory, so a key-bearing run is marked
non-resumable and honestly re-surfaced as "interrupted, rode de novo" instead of
being faked back to life. The per-node graph state that actually makes the
resume cheap lives in the LangGraph SQLite checkpoint (keyed by ticker+date+
graph-shape); this store only says *which* runs to resume.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from pathlib import Path
from typing import Any

from tradingagents.dataflows.utils import safe_ticker_component


class ActiveRunStore:
    """One JSON descriptor per in-flight run under ``<base>/``.

    ``put`` on start, ``remove`` on terminal, ``list_pending`` on boot. Writes are
    atomic (temp + rename) and lock-guarded so concurrent runs on the Tailscale
    network never corrupt a descriptor. Every method fails soft — a descriptor is
    a best-effort recovery aid, never allowed to crash a run.
    """

    def __init__(self, base_dir: str | os.PathLike):
        self.base = Path(base_dir)
        self._lock = threading.Lock()
        # dir de recuperação é best-effort; nunca impede a subida
        with contextlib.suppress(OSError):
            self.base.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> Path:
        # A run_id is our own timestamp+hex, but guard anyway so a crafted id can
        # never escape the directory when interpolated into the filename.
        safe = safe_ticker_component(str(run_id))
        return self.base / f"{safe}.json"

    def put(self, run_id: str, descriptor: dict[str, Any]) -> None:
        """Persist (or overwrite) the descriptor for an in-flight run."""
        path = self._path(run_id)
        with self._lock:
            try:
                tmp = path.with_suffix(path.suffix + ".tmp")
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(descriptor, fh, ensure_ascii=False, default=str)
                os.replace(tmp, path)
            except OSError:
                pass  # recovery aid only — never block the run

    def remove(self, run_id: str) -> None:
        """Drop the descriptor once the run reaches a terminal state."""
        # já removido / nunca escrito — idempotente por design
        with self._lock, contextlib.suppress(OSError):
            self._path(run_id).unlink()

    def list_pending(self) -> list[dict[str, Any]]:
        """Every descriptor still on disk — the interrupted runs to recover."""
        out: list[dict[str, Any]] = []
        with self._lock:
            if not self.base.exists():
                return out
            for path in sorted(self.base.glob("*.json")):
                try:
                    with open(path, encoding="utf-8") as fh:
                        rec = json.load(fh)
                    if isinstance(rec, dict) and rec.get("run_id"):
                        out.append(rec)
                except (OSError, json.JSONDecodeError):
                    continue
        return out

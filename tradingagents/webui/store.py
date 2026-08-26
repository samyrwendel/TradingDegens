"""On-disk history for completed analyses.

Each run is saved as one JSON file under ``<results_dir>/webui/runs/`` plus a
one-line summary appended to ``index.jsonl``. The UI lists summaries (cheap) and
re-opens a full run on demand. Writes are file-locked and atomic (temp + rename)
so concurrent runs on the Tailscale network don't corrupt the index.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

_SUMMARY_KEYS = (
    "run_id", "ticker", "date", "asset_type", "status",
    "verdict", "verdict_timeframe", "method", "cost_usd", "elapsed", "finished_at",
)


class HistoryStore:
    """JSON-file history keyed by run_id, newest-first on read."""

    def __init__(self, base_dir: str | os.PathLike):
        self.base = Path(base_dir)
        self.runs_dir = self.base / "runs"
        self.index_path = self.base / "index.jsonl"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def save(self, record: dict[str, Any]) -> None:
        """Persist a full run record and append its summary to the index."""
        run_id = record["run_id"]
        with self._lock:
            self._atomic_write(self.runs_dir / f"{run_id}.json", record)
            summary = {k: record.get(k) for k in _SUMMARY_KEYS}
            with open(self.index_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(summary, default=str) + "\n")

    def recent(self, limit: int = 25) -> list[dict[str, Any]]:
        """Return up to ``limit`` most-recent run summaries, newest first."""
        if not self.index_path.exists():
            return []
        with self._lock:
            with open(self.index_path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        out: list[dict[str, Any]] = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(out) >= limit:
                break
        return out

    def delete_ticker(self, ticker: str) -> int:
        """Remove every run of ``ticker`` from the index and delete its JSON files.

        The sidebar is a per-asset list, so the UI's "×" removes the whole asset.
        Rewrites ``index.jsonl`` atomically keeping the other lines; unlinks each
        removed run file best-effort. Returns how many index entries were dropped.
        Unreadable index lines are preserved untouched.
        """
        target = (ticker or "").strip().upper()
        if not target:
            return 0
        with self._lock:
            if not self.index_path.exists():
                return 0
            with open(self.index_path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
            kept: list[str] = []
            removed_ids: set[str] = set()
            removed = 0
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    rec = json.loads(stripped)
                except json.JSONDecodeError:
                    kept.append(line if line.endswith("\n") else line + "\n")
                    continue
                if (rec.get("ticker") or "").strip().upper() == target:
                    removed += 1
                    rid = rec.get("run_id")
                    if rid:
                        removed_ids.add(str(rid))
                else:
                    kept.append(line if line.endswith("\n") else line + "\n")
            if not removed:
                return 0
            tmp = self.index_path.with_suffix(self.index_path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.writelines(kept)
            os.replace(tmp, self.index_path)
            for rid in removed_ids:
                try:
                    (self.runs_dir / f"{rid}.json").unlink()
                except OSError:
                    pass
        return removed

    def get(self, run_id: str) -> dict[str, Any] | None:
        """Load a full run record by id, or ``None`` if unknown."""
        path = self.runs_dir / f"{run_id}.json"
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _atomic_write(path: Path, data: dict[str, Any]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, default=str)
        os.replace(tmp, path)

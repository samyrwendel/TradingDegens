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
    "setup_state",
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
        with self._lock, open(self.index_path, encoding="utf-8") as fh:
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

    def watchlist(self) -> list[dict[str, Any]]:
        """Lista de observação: UM resumo por TICKER único já pesquisado — TODOS,
        persistente, a lista **só cresce** (só o ``delete_ticker`` remove).

        Ao contrário do ``recent(limit)`` (janela dos N runs mais recentes, onde um
        ativo pesquisado há tempo SOME quando seus runs saem da janela), aqui o index
        INTEIRO é varrido e devolve-se, por ticker, o run MAIS RECENTE dele + a
        contagem de análises (``count``). Ordenado pela atividade mais recente (o
        ticker pesquisado por último no topo). Linhas ilegíveis são ignoradas.
        """
        if not self.index_path.exists():
            return []
        with self._lock, open(self.index_path, encoding="utf-8") as fh:
            lines = fh.readlines()
        latest: dict[str, dict[str, Any]] = {}
        counts: dict[str, int] = {}
        order: list[str] = []
        for line in reversed(lines):          # mais novo primeiro
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = (rec.get("ticker") or "").strip().upper()
            if not t:
                continue
            if t not in latest:               # 1ª ocorrência (varrendo do fim) = mais recente
                latest[t] = rec
                order.append(t)
            counts[t] = counts.get(t, 0) + 1
        out: list[dict[str, Any]] = []
        for t in order:
            rec = dict(latest[t])
            rec["count"] = counts[t]
            out.append(rec)
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
            with open(self.index_path, encoding="utf-8") as fh:
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
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _atomic_write(path: Path, data: dict[str, Any]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, default=str)
        os.replace(tmp, path)


class WatchlistStore:
    """Watchlist MANUAL do scan de portfólio — editável na webui (owner-only).

    Distinta da ``HistoryStore.watchlist()`` (derivada do histórico, só cresce):
    esta é a lista CURADA pelo dono — o que ele quer vigiar, não tudo que já
    analisou. Na PRIMEIRA leitura sem arquivo, semeia com os tickers do
    histórico (a lista derivada) pra nascer útil; depois o dono edita.
    Persistência no mesmo molde: JSON atômico + lock.
    """

    def __init__(self, base_dir: str | os.PathLike, history: HistoryStore):
        self.path = Path(base_dir) / "watchlist.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._history = history
        self._lock = threading.Lock()

    def get(self) -> list[dict[str, Any]]:
        """``[{ticker, name, asset_type, count?}]`` — ordem de inserção."""
        with self._lock:
            if self.path.exists():
                try:
                    with open(self.path, encoding="utf-8") as fh:
                        data = json.load(fh)
                    return list(data.get("tickers") or [])
                except (OSError, json.JSONDecodeError):
                    pass  # arquivo corrompido → cai na semente
        # Semente única (na primeira vez ou se o arquivo for ilegível): os
        # tickers que o dono já analisou, mais recente primeiro.
        seeded = [{"ticker": w.get("ticker"), "name": w.get("name"),
                   "asset_type": w.get("asset_type"), "count": w.get("count")}
                  for w in self._history.watchlist()[:30]]
        if seeded:
            self._write(seeded)
        return seeded

    def set(self, tickers: list[str]) -> list[dict[str, Any]]:
        """Substitui a lista inteira (normaliza: upper, dedup, máx 100)."""
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for t in tickers:
            up = (str(t) or "").strip().upper()
            if up and up not in seen:
                seen.add(up)
                out.append({"ticker": up})
            if len(out) >= 100:
                break
        self._write(out)
        return out

    def add(self, ticker: str) -> list[dict[str, Any]]:
        up = (str(ticker) or "").strip().upper()
        current = self.get()
        if up and not any(w.get("ticker") == up for w in current):
            current.insert(0, {"ticker": up})
            # Truncar UMA vez e devolver a MESMA lista que foi pro disco: antes
            # gravava ``current[:100]`` e devolvia ``current`` inteiro, então o 101º
            # ticker aparecia na UI e sumia no reload — a tela mentia sobre o que
            # tinha sido salvo.
            current = current[:100]
            self._write(current)
        return current

    def remove(self, ticker: str) -> list[dict[str, Any]]:
        up = (str(ticker) or "").strip().upper()
        current = [w for w in self.get() if w.get("ticker") != up]
        self._write(current)
        return current

    def _write(self, tickers: list[dict[str, Any]]) -> None:
        with self._lock:
            HistoryStore._atomic_write(self.path, {"tickers": tickers})


class ScanSnapshotStore:
    """O último scan COMPLETO em disco — pra a tela nascer com informação.

    A varredura da watchlist custa 8–20s (20 ativos × 3 frames, e o Storm somou
    trabalho). O ``_scan_memo`` do runner dura 5s e o ``_live_cache`` do scanner
    30s; nenhum dos dois sobrevive a um restart, e o ``scans.jsonl`` é
    append-only só dos ``em_gatilho`` — nenhum deles é "o último resultado
    completo". Sem isso, quem abre o painel encara a tela VAZIA a varredura
    inteira: a task 014 fez o resultado anterior sobreviver DENTRO da sessão do
    navegador, e na primeira carga não existe anterior nenhum.

    Aqui ele passa a sobreviver ao navegador E ao processo: um arquivo só
    (``last_scan.json``), escrita atômica com lock, na mesma disciplina do
    :class:`HistoryStore` (temp + rename) — a última varredura sobrescreve a
    anterior, porque o que se quer é o estado mais recente, não um histórico.

    Duas regras que a leitura da tela depende:

    * **Só varredura COMPLETA entra.** A passada agendada varre um subconjunto
      (só o que o mercado justifica varrer agora) — gravá-la aqui faria a
      abertura mostrar meia watchlist como se fosse a lista toda.
    * **Vazio não se grava.** Watchlist vazia (ou varredura que não devolveu
      ativo nenhum) não carrega informação: guardá-la faria a abertura pintar
      uma lista vazia que se lê como "não há nada em gatilho".

    Fail-open em toda leitura: arquivo ausente, ilegível ou corrompido devolve
    ``{}`` e a tela cai no comportamento de primeira carga.
    """

    def __init__(self, base_dir: str | os.PathLike):
        self.path = Path(base_dir) / "last_scan.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def save(self, resultado: dict[str, Any]) -> bool:
        """Grava ``resultado`` como o último scan completo. ``False`` = não gravou.

        O resultado vai inteiro, com o ``gerado_em`` que o
        :func:`scanner.scan_watchlist` carimbou — é ele que a tela exibe pra
        dizer DE QUANDO é o que está mostrando.
        """
        if not (resultado or {}).get("ativos"):
            return False
        with self._lock:
            HistoryStore._atomic_write(self.path, resultado)
        return True

    def get(self) -> dict[str, Any]:
        """O último scan completo salvo, ou ``{}`` se não houver (ou não ler)."""
        with self._lock:
            if not self.path.exists():
                return {}
            try:
                with open(self.path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                return {}
        return data if isinstance(data, dict) else {}

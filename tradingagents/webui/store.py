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
    """O ÚLTIMO CONHECIDO da watchlist — por ATIVO, em disco, alimentado por TODA
    varredura bem-sucedida (a da tela e a da agenda).

    **Por que existe.** A varredura completa custa 8–20s e o painel abria vazio esse
    tempo todo. O ``_scan_memo`` do runner dura 5s e o ``_live_cache`` do scanner 30s
    — os dois em memória, os dois mortos no restart —, e o ``scans.jsonl`` é
    append-only só dos ``em_gatilho`` (é um ledger de gatilhos, não uma varredura).
    Enquanto isso a agenda já varria de hora em hora e **jogava o resultado fora**,
    guardando só as contagens: o dado que a tela precisava estava sendo produzido e
    descartado.

    **Por que por ATIVO, e não "o último scan completo".** A passada agendada é
    PARCIAL por desenho — cripto sempre, ação só com o pregão aberto, porque fora
    dele a ação repete o mesmo candle. Guardar a passada inteira como "o último scan"
    faria a abertura de madrugada mostrar meia watchlist sem dizer. Aqui cada ativo
    guarda a leitura DELE com a hora DELE: a passada nova sobrescreve quem ela
    varreu, e quem ficou de fora permanece com o dado anterior e o carimbo anterior.
    Nunca some, nunca finge ser de agora.

    **O que a passada declara** (``ultima_passada``): quando foi, se foi COMPLETA (a
    watchlist inteira) ou parcial, qual era a sessão de mercado e quais tickers ela
    cobriu. É com isso que a tela diz o que está mostrando em vez de deixar o leitor
    supor.

    **Universo.** Todo registro passa a lista atual da watchlist: ativo removido dela
    sai do arquivo na gravação seguinte. Sem isso um ticker apagado ficaria para
    sempre, porque uma passada parcial nunca o mencionaria de novo.

    Escrita atômica com lock (temp + rename), na mesma disciplina do
    :class:`HistoryStore`. Fail-open em toda leitura: arquivo ausente, ilegível ou
    corrompido devolve ``{}`` e a tela cai no comportamento de primeira carga.
    """

    def __init__(self, base_dir: str | os.PathLike):
        self.path = Path(base_dir) / "last_scan.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def registrar(self, resultado: dict[str, Any], *,
                  universo: list[str] | None = None,
                  completa: bool = True,
                  sessao: str | None = None,
                  ordenar_e_resumir=None) -> dict[str, Any]:
        """Funde uma varredura no último conhecido e devolve o estado resultante.

        ``resultado`` é o que :func:`scanner.scan_watchlist` produziu (completo ou de
        uma passada parcial). ``universo`` é a watchlist ATUAL — quem não estiver nela
        sai. ``ordenar_e_resumir`` é injetado pelo chamador (:mod:`scanner`) para que
        a lista mesclada saia na MESMA ordem e com a MESMA contagem da varredura viva;
        sem ele a fusão preserva a ordem de chegada e recontagem simples.

        Devolve ``{}`` sem gravar quando não há o que guardar — nem ativo novo nem
        ativo anterior. Uma lista vazia guardada se leria na tela como "não há nada em
        gatilho", que é uma afirmação, não uma ausência de dado.
        """
        gerado_em = resultado.get("gerado_em")
        novos = {a.get("ticker"): a for a in (resultado.get("ativos") or []) if a.get("ticker")}
        with self._lock:
            atual = self._ler()
            guardados = {a.get("ticker"): a for a in (atual.get("ativos") or []) if a.get("ticker")}
            # O ativo varrido AGORA sobrescreve o anterior e leva o carimbo desta
            # passada. Quem não foi varrido fica exatamente como estava — com o
            # carimbo da passada em que ELE foi lido.
            for ticker, row in novos.items():
                guardados[ticker] = {**row, "gerado_em": gerado_em}
            if universo is not None:
                permitidos = {str(t).strip().upper() for t in universo}
                guardados = {t: r for t, r in guardados.items() if t.upper() in permitidos}
            if not guardados:
                return {}
            ativos = list(guardados.values())
            if ordenar_e_resumir is not None:
                ativos, resumo = ordenar_e_resumir(ativos)
            else:
                resumo = {}
                for a in ativos:
                    estado = (a.get("melhor") or {}).get("estado")
                    if estado:
                        resumo[estado] = resumo.get(estado, 0) + 1
            estado = {
                "date": resultado.get("date") or atual.get("date"),
                "frames": resultado.get("frames") or atual.get("frames") or [],
                # O carimbo do TOPO é o da passada mais recente. Cada ativo carrega o
                # dele; é a comparação entre os dois que a tela usa pra marcar quem
                # ficou para trás.
                "gerado_em": gerado_em or atual.get("gerado_em"),
                "ultima_passada": {
                    "gerado_em": gerado_em,
                    "completa": bool(completa),
                    "sessao": sessao,
                    "tickers": sorted(novos),
                    "universo": len(guardados),
                },
                "resumo": resumo,
                "ativos": ativos,
            }
            self._escrever(estado)
            return estado

    def get(self) -> dict[str, Any]:
        """O último conhecido, ou ``{}`` se não houver (ou não der pra ler)."""
        with self._lock:
            return self._ler()

    # -- privados (sempre chamados com o lock tomado) ---------------------------
    def _ler(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _escrever(self, estado: dict[str, Any]) -> None:
        HistoryStore._atomic_write(self.path, estado)


class PaperWalletStore:
    """O MARCO da carteira virtual do paper trading (DA-155) — a data a partir da
    qual os gatilhos do ledger contam pro saldo simulado.

    NÃO é um saldo: o saldo é sempre DERIVADO, lendo o ``scans.jsonl`` inteiro
    (``scanner._carteira_paper``) — persistir um número aqui do lado abriria a
    porta pra ele divergir do ledger que o sustenta. O que este arquivo guarda é
    só a FRONTEIRA de tempo.

    Isso é o que resolve "resetar a simulação" sem violar o ledger append-only
    (disciplina da task 008): reiniciar não apaga nada, só empurra o marco pra
    AGORA — os gatilhos de antes continuam gravados, só saem da leitura da
    carteira. Um arquivo ausente (nunca resetado) lê como ``None`` — "desde
    sempre", o ledger inteiro conta.
    """

    def __init__(self, base_dir: str | os.PathLike):
        self.path = Path(base_dir) / "paper_wallet.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def marco(self) -> str | None:
        """O ``ts`` (mesmo formato do ledger) a partir do qual a carteira conta;
        ``None`` quando nunca foi resetada — a carteira lê o ledger inteiro."""
        with self._lock:
            if not self.path.exists():
                return None
            try:
                with open(self.path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                return None
            return (data or {}).get("marco") or None

    def resetar(self, marco: str) -> str:
        """Grava o novo marco (o chamador carimba ``marco`` — mesmo formato de
        ``ScanLog.record``, pra a comparação lexicográfica com ``ts`` valer)."""
        with self._lock:
            HistoryStore._atomic_write(self.path, {"marco": marco})
        return marco


class EstrategiaStore:
    """Flag de estratégia por setup — visibilidade na TELA (DA-184), owner-only.

    NÃO desliga o motor: o scan agendado (``AnalysisRunner.scan_agendado``) e o
    dry-run MT5 nunca leem este arquivo — só o servidor, ao montar a resposta pro
    front, decide o que mostrar. Ausência de arquivo = o padrão da DA-184 (Setup123
    ligado, Storm123 desligado). Mesmo molde do :class:`WatchlistStore`: JSON
    atômico + lock, fail-open pro padrão em qualquer leitura ruim.
    """

    PADRAO = {"123": True, "storm": False}

    def __init__(self, base_dir: str | os.PathLike):
        self.path = Path(base_dir) / "estrategias.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def get(self) -> dict[str, bool]:
        with self._lock:
            if self.path.exists():
                try:
                    with open(self.path, encoding="utf-8") as fh:
                        data = json.load(fh)
                    if isinstance(data, dict):
                        out = dict(self.PADRAO)
                        out.update({k: bool(v) for k, v in data.items() if k in self.PADRAO})
                        return out
                except (OSError, json.JSONDecodeError):
                    pass  # arquivo corrompido → cai no padrão
        return dict(self.PADRAO)

    def set(self, nome: str, ativo: bool) -> dict[str, bool]:
        nome = (nome or "").strip().lower()
        if nome not in self.PADRAO:
            raise ValueError(f"estratégia desconhecida: {nome!r}")
        atual = self.get()
        atual[nome] = bool(ativo)
        with self._lock:
            HistoryStore._atomic_write(self.path, atual)
        return atual

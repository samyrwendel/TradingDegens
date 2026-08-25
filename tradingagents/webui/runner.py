"""Drives the TradingAgents engine for the web UI — one analysis per run.

This module owns no analysis logic. It builds a
:class:`~tradingagents.graph.trading_graph.TradingAgentsGraph`, attaches the
usage + progress callbacks, calls ``graph.propagate(...)`` on a worker thread,
and exposes a pollable status snapshot (progress, live cost, and — when done —
the extracted result). Each run gets its own graph instance so its token/cost
tracking and progress are isolated even when two people on the Tailscale network
run analyses at once.
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from typing import Any

from langchain_core.callbacks import UsageMetadataCallbackHandler

from tradingagents.webui import ask as ask_module, timeutil
from tradingagents.webui.compare import (
    build_column,
    confront_pair_valid,
    detect_method,
    deterministic_meta,
)
from tradingagents.webui.pricing import cost_breakdown
from tradingagents.webui.progress import ProgressCallbackHandler, ProgressTracker
from tradingagents.webui.store import HistoryStore

# Default analyst order; crypto drops fundamentals (no balance sheet for a coin).
_ANALYST_ORDER = ("market", "social", "news", "fundamentals")

# The chart's timeframe selector, ordered widest→narrowest (semanal · diário · 4h
# · 1h · 15m). The weekly frame is resampled in memory from the daily series; the
# intraday ladder is a real keyless candle for BOTH asset types now — crypto from
# the exchange (native), equity from yfinance (15m/1h native, 4h resampled per
# session). A frame with no source for a given symbol/date degrades honestly
# on demand ("intradiário indisponível") rather than inventing a bar, so the
# buttons are operable for every asset. Daily (``_DEFAULT_TIMEFRAME``) stays the
# frame every analysis is first computed on — the selector re-derives the rest.
_CRYPTO_TIMEFRAMES = ("1w", "1d", "4h", "1h", "15m")
_STOCK_TIMEFRAMES = ("1w", "1d", "4h", "1h", "15m")
_DEFAULT_TIMEFRAME = "1d"

# Teto da pergunta do Q&A ancorado (/api/ask): corta enrolação, segura o custo.
_MAX_QUESTION_CHARS = 500


def select_analysts_for_asset(asset_type: str, include_erick: bool = False) -> list[str]:
    """Analyst wire-keys to run for an asset type (crypto has no fundamentals).

    ``include_erick`` appends the on-demand Erick-method analyst at the end of the
    analyst chain (Modo Erick). Default off — the Padrão analysis is untouched.
    """
    if asset_type == "crypto":
        base = [a for a in _ANALYST_ORDER if a != "fundamentals"]
    else:
        base = list(_ANALYST_ORDER)
    if include_erick:
        base.append("erick")
    return base


def timeframes_for_asset(asset_type: str) -> list[str]:
    """Operable chart timeframes for an asset type — the full intraday ladder for
    both crypto and equity (each frame has a real keyless source; a symbol/date with
    no candle degrades honestly on demand). The single source of truth the UI
    selector and the ``/api/chart`` validation both read, so they never disagree."""
    return list(_CRYPTO_TIMEFRAMES if asset_type == "crypto" else _STOCK_TIMEFRAMES)


def extract_result(final_state: dict[str, Any], signal: str) -> dict[str, Any]:
    """Pull the display fields out of a completed run's final state.

    The two theses (bull/bear) are the headline: the brief measures the debate
    text as more useful than the verdict itself, so they are surfaced whole and
    side by side. Every field defaults to empty string — a degraded source shows
    "indisponível" in the report text the engine already wrote; nothing is
    fabricated here.
    """
    debate = final_state.get("investment_debate_state") or {}
    risk = final_state.get("risk_debate_state") or {}
    return {
        "verdict": signal,
        # Sources that degraded (failed after the auto-retry) — the UI names them,
        # says the analysis ran without them, and offers a "reavaliar" control.
        "degraded": list(final_state.get("degraded_sources") or []),
        "final_trade_decision": final_state.get("final_trade_decision", "") or "",
        "bull": debate.get("bull_history", "") or "",
        "bear": debate.get("bear_history", "") or "",
        "research_manager": debate.get("judge_decision", "") or "",
        "investment_plan": final_state.get("investment_plan", "") or "",
        "trader_plan": final_state.get("trader_investment_plan", "") or "",
        "risk_decision": risk.get("judge_decision", "") or "",
        "market_report": final_state.get("market_report", "") or "",
        "sentiment_report": final_state.get("sentiment_report", "") or "",
        "news_report": final_state.get("news_report", "") or "",
        "fundamentals_report": final_state.get("fundamentals_report", "") or "",
        # On-demand "Modo Erick": empty string unless the erick analyst ran.
        "erick_report": final_state.get("erick_report", "") or "",
        # Filled by the runner for crypto from the engine's deterministic
        # derivatives data path (named source, "unavailable" not fabricated).
        "derivatives_report": "",
        # Filled by the runner for every asset: candles + moving averages +
        # detected setup markers for the chart (deterministic, cached series).
        "price_chart": {},
        # Filled by the runner for every asset: operable levels beside the verdict
        # (price @ analysis, horizon, timeframe, buy/realize/pullback zones), all
        # from the same cached series — "sem nível definido", never a fake number.
        "actionable": {},
    }


def fetch_derivatives_report(ticker: str, date: str) -> str:
    """Deterministic funding / OI / liquidations report straight from the vendor.

    The market analyst's prose can drop the source name; this pulls the same data
    the engine fetched (cached, so free) so the UI can always show funding, open
    interest and liquidations *with the source named* — and "unavailable" instead
    of a fabricated number when a feed is down. Fail-open: returns "" on error so
    a vendor hiccup never blocks the result.
    """
    try:
        from tradingagents.dataflows.interface import route_to_vendor
        return route_to_vendor("get_crypto_derivatives", ticker, date) or ""
    except Exception:
        return ""


def fetch_price_chart(ticker: str, date: str, timeframe: str = _DEFAULT_TIMEFRAME,
                      method: str = "padrao") -> dict[str, Any]:
    """Candle + moving-average + setup-marker payload for the UI chart.

    ``timeframe`` selects the frame (daily default, or ``"4h"``/``"1h"``/``"15m"``
    intraday for crypto — real keyless exchange candles). ``method`` picks the
    average family the setup MARKERS key on (Padrão MMS / Erick EMA 8/21) so the two
    confront columns draw different structures. Reuses the same cached, date-guarded
    series the detector runs on, so it is free and cannot see a future candle.
    Fail-open: returns an empty payload on any error so a chart hiccup never blocks
    the analysis result.
    """
    try:
        from tradingagents.dataflows.price_structure import build_price_chart
        return build_price_chart(ticker, date, timeframe=timeframe, method=method)
    except Exception:
        return {}


def fetch_actionable_plan(ticker: str, date: str, timeframe: str = _DEFAULT_TIMEFRAME,
                          method: str = "padrao") -> dict[str, Any]:
    """Operable levels for the verdict header (price @ analysis, horizon,
    timeframe, buy/realize/pullback zones).

    ``timeframe`` selects the frame (daily default, or an intraday frame for
    crypto). ``method`` picks the average family the recuo/zones key on (Padrão MMS
    / Erick EMA 8/21). Reuses the same cached, date-guarded series and the detector's
    own structure, so it is free and cannot see a future candle. Fail-open: returns
    ``{}`` on any error so this enrichment never blocks the analysis result.
    """
    try:
        from tradingagents.dataflows.price_structure import build_actionable_plan_dict
        return build_actionable_plan_dict(ticker, date, timeframe=timeframe, method=method)
    except Exception:
        return {}


def fetch_symbol_search(term: str, limit: int = 8) -> list[dict[str, Any]]:
    """Autocomplete candidates for a name-or-ticker term (fail-open -> [])."""
    try:
        from tradingagents.dataflows.symbol_search import search_symbols
        return search_symbols(term, limit=limit)
    except Exception:
        return []


def fetch_symbol_names(symbols: list[str]) -> dict[str, str]:
    """Batch symbol -> display name for the history chips (fail-open -> {})."""
    try:
        from tradingagents.dataflows.symbol_search import resolve_names
        return resolve_names(symbols)
    except Exception:
        return {}


def resolve_ticker_query(term: str) -> str | None:
    """A typed name-or-ticker -> the symbol to analyse (fail-open).

    A plain ticker (``looks_like_symbol``) passes through with NO network call and is
    never hijacked; a name ("Microsoft"/"Bitcoin") is resolved to its symbol via
    Yahoo search. ``None`` when the term is already a symbol, or Yahoo is down.
    """
    try:
        from tradingagents.dataflows.symbol_search import (
            looks_like_symbol,
            resolve_query_to_symbol,
        )
        if not term or looks_like_symbol(term):
            return None
        return resolve_query_to_symbol(term)
    except Exception:
        return None


class _Run:
    """In-memory state for a single analysis run."""

    def __init__(self, run_id: str, ticker: str, date: str, asset_type: str,
                 selected_analysts: list[str], timeframe: str = _DEFAULT_TIMEFRAME):
        self.run_id = run_id
        self.ticker = ticker
        self.date = date
        self.asset_type = asset_type
        self.selected_analysts = selected_analysts
        # Reference frame the market analyst reads for THIS run (task 012). Stamped
        # on the verdict and used as the chart's opening frame.
        self.timeframe = timeframe or _DEFAULT_TIMEFRAME
        self.status = "running"           # running | done | error
        self.error: str | None = None
        self.result: dict[str, Any] | None = None
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.finished_stamp: str | None = None  # Manaus ISO, set on completion
        self.usage_cb = UsageMetadataCallbackHandler()
        self.tracker = ProgressTracker(selected_analysts)

    def cost(self) -> dict[str, Any]:
        return cost_breakdown(self.usage_cb.usage_metadata)

    def snapshot(self) -> dict[str, Any]:
        elapsed = (self.finished_at or time.time()) - self.started_at
        return {
            "run_id": self.run_id,
            "ticker": self.ticker,
            "date": self.date,
            "asset_type": self.asset_type,
            "status": self.status,
            "error": self.error,
            "verdict_timeframe": self.timeframe,
            "progress": self.tracker.snapshot(),
            "cost": self.cost(),
            "elapsed": round(elapsed, 1),
            "finished_at": self.finished_stamp,
            "result": self.result,
        }


class _SimpleProgress:
    """Progresso do confronto como TRILHA de 3 etapas: Análise Padrão → Análise
    método Erick → Comparação (meta-juiz). Cada etapa tem estado próprio
    (``pending``/``running``/``done``/``reused``) para o front desenhar o stepper —
    o motor já roda as 3 em série, isto só EXPÕE o que rodou e o que veio do cache
    (um lado reaproveitado aparece ``reused``, sem fingir que rodou; DA-058).

    Expõe a mesma forma de ``snapshot()`` que :class:`ProgressTracker`, mais o campo
    ``compare_steps``, para fluir por ``status``/``active_runs`` sem mudança."""

    # (key, rótulo) na ordem em que o worker as executa.
    STEPS = (
        ("padrao", "Análise Padrão"),
        ("erick", "Análise método Erick"),
        ("meta", "Comparação (meta-juiz)"),
    )

    def __init__(self):
        self._phase = "Inicializando"
        self._label = "Preparando comparação…"
        self._pct = 0
        self._started = time.monotonic()
        self._state = {key: "pending" for key, _ in self.STEPS}

    def set(self, phase: str, label: str, pct: int) -> None:
        self._phase, self._label, self._pct = phase, label, pct

    def step(self, key: str, state: str) -> None:
        """Marca o estado de uma etapa (running/done/reused/pending)."""
        if key in self._state:
            self._state[key] = state

    def done(self) -> None:
        # qualquer etapa que ficou 'running' na conclusão vira 'done'
        for key, st in self._state.items():
            if st == "running":
                self._state[key] = "done"
        self.set("Concluído", "Comparação concluída", 100)

    def snapshot(self) -> dict[str, Any]:
        steps = [
            {"key": key, "label": label, "state": self._state[key]}
            for key, label in self.STEPS
        ]
        return {
            "phase": self._phase, "label": self._label, "percent": self._pct,
            "index": 0, "total": len(self.STEPS),
            "elapsed": round(time.monotonic() - self._started, 1),
            "plan": [], "reached": [],
            "compare_steps": steps,
        }


class _CompareRun:
    """In-memory state for a Padrão × Erick comparison run.

    Duck-types the pieces of :class:`_Run` that ``status``/``active_runs``/
    ``_running_summary`` read, so it lives in the same ``_runs`` table and reuses
    the polling + history plumbing. Its ``result`` carries a ``compare`` block the
    UI renders side by side.
    """

    def __init__(self, run_id, ticker, date, asset_type, timeframe):
        self.run_id = run_id
        self.ticker = ticker
        self.date = date
        self.asset_type = asset_type
        self.timeframe = timeframe
        self.status = "running"
        self.error: str | None = None
        self.result: dict[str, Any] | None = None
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.finished_stamp: str | None = None
        self.tracker = _SimpleProgress()

    def cost(self) -> dict[str, Any]:
        cols = (self.result or {}).get("compare") or {}
        total = 0.0
        for k in ("a", "b"):
            total += float(((cols.get(k) or {}).get("cost") or {}).get("usd") or 0)
        return {"usd": round(total, 6), "complete": True}

    def snapshot(self) -> dict[str, Any]:
        elapsed = (self.finished_at or time.time()) - self.started_at
        return {
            "run_id": self.run_id,
            "ticker": self.ticker,
            "date": self.date,
            "asset_type": self.asset_type,
            "status": self.status,
            "error": self.error,
            "verdict_timeframe": self.timeframe,
            "compare": True,
            "progress": self.tracker.snapshot(),
            "cost": self.cost(),
            "elapsed": round(elapsed, 1),
            "finished_at": self.finished_stamp,
            "result": self.result,
        }


class AnalysisRunner:
    """Starts analyses and tracks their live status."""

    def __init__(self, base_config: dict[str, Any] | None = None,
                 store: HistoryStore | None = None,
                 graph_factory=None, meta_judge=None):
        # Imported lazily so unit tests can construct the runner (and exercise
        # the pure helpers) without importing the heavy engine / config.
        if base_config is None:
            from tradingagents.default_config import DEFAULT_CONFIG
            base_config = DEFAULT_CONFIG
        self.base_config = dict(base_config)
        if store is None:
            from pathlib import Path
            store = HistoryStore(Path(self.base_config["results_dir"]) / "webui")
        self.store = store
        # graph_factory(config, selected_analysts, callbacks) -> engine graph.
        # Injectable so tests can drive a fake engine.
        self._graph_factory = graph_factory or self._default_graph_factory
        # meta_judge(padrao_col, erick_col, asset_type) -> comparison dict.
        # Deterministic by default (anchored, free, keeps the run at 2 pipelines);
        # injectable so tests drive it and a future LLM narrative can slot in.
        self._meta_judge = meta_judge or (lambda p, e, at: deterministic_meta(p, e))
        self._runs: dict[str, Any] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _default_graph_factory(config, selected_analysts, callbacks):
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        return TradingAgentsGraph(
            selected_analysts=selected_analysts,
            debug=False,
            config=config,
            callbacks=callbacks,
        )

    @staticmethod
    def detect_asset_type(ticker: str) -> str:
        from cli.utils import detect_asset_type
        return detect_asset_type(ticker).value

    def start(self, ticker: str, date: str, method: str = "padrao",
              timeframe: str = _DEFAULT_TIMEFRAME) -> str:
        """Kick off an analysis; returns a run_id to poll immediately.

        ``method="erick"`` adds the on-demand Erick-method analyst to the run
        (Modo Erick); any other value runs the Padrão selection unchanged.

        ``timeframe`` is the reference frame the market analyst reads (task 012);
        it must be one of the asset's operable frames (``timeframes_for_asset``) —
        an intraday frame on an equity is rejected here, mirroring ``/api/chart``.
        Each (ticker, date, timeframe) is a distinct, cacheable run whose verdict
        may differ; only the technical read changes, the fundamental thesis holds.

        ``ticker`` may be a company NAME ("Microsoft", "Bitcoin"): it is resolved to
        the symbol via Yahoo search first (a plain ticker passes through untouched,
        no network); if Yahoo is down the raw term is kept and the data path decides.
        """
        raw = (ticker or "").strip()
        if not raw:
            raise ValueError("ticker vazio")
        ticker = (resolve_ticker_query(raw) or raw).strip().upper()
        # Default to *today in Manaus*, not the process/UTC date: at 21:30 Manaus
        # it is already the next day in UTC and the run would carry tomorrow.
        date = (date or "").strip() or timeutil.today()
        asset_type = self.detect_asset_type(ticker)
        timeframe = (timeframe or _DEFAULT_TIMEFRAME).strip() or _DEFAULT_TIMEFRAME
        allowed = timeframes_for_asset(asset_type)
        if timeframe not in allowed:
            raise ValueError(
                f"timeframe {timeframe!r} indisponível para {asset_type} "
                f"(disponíveis: {', '.join(allowed)})"
            )
        selected = select_analysts_for_asset(
            asset_type, include_erick=(method == "erick")
        )
        run_id = timeutil.run_id_stamp() + "-" + uuid.uuid4().hex[:6]
        run = _Run(run_id, ticker, date, asset_type, selected, timeframe=timeframe)
        with self._lock:
            self._runs[run_id] = run
        threading.Thread(target=self._worker, args=(run,), daemon=True).start()
        return run_id

    def _worker(self, run: _Run) -> None:
        # ``final_status`` is flipped onto the run only after the history write,
        # so any poller that sees a terminal status also sees both the result and
        # the persisted history row (no read-before-write race).
        final_status = self._execute(run)
        self._persist(run, final_status)
        run.status = final_status

    def _execute(self, run: _Run) -> str:
        """Run the analysis pipeline for ``run`` in the CURRENT thread.

        Fills ``run.result`` / ``run.error`` and stamps ``finished_at`` /
        ``finished_stamp``; returns ``"done"`` or ``"error"``. Does NOT persist or
        flip ``run.status`` — the caller owns those so it can compose (the compare
        orchestrator runs two of these inline before persisting).
        """
        final_status = "error"
        try:
            config = dict(self.base_config)
            progress_cb = ProgressCallbackHandler(run.tracker)
            graph = self._graph_factory(
                config, run.selected_analysts, [run.usage_cb, progress_cb]
            )
            final_state, signal = graph.propagate(
                run.ticker, run.date, asset_type=run.asset_type,
                timeframe=run.timeframe
            )
            run.result = extract_result(final_state, signal)
            if run.asset_type == "crypto":
                run.result["derivatives_report"] = fetch_derivatives_report(
                    run.ticker, run.date
                )
            # A estrutura desenhada é CIENTE DO MÉTODO (task 031): a coluna Erick lê o
            # recuo/1-2-3 na EMA 8/21 (a média do método), o Padrão nas MMS — assim o
            # confronto mostra estruturas de verdade diferentes, não o mesmo overlay 2x.
            method = "erick" if "erick" in run.selected_analysts else "padrao"
            # The chart opens on the SAME frame the verdict was computed on, so the
            # picture matches the stamp; the UI can still recalc other frames via
            # /api/chart. Persist the ladder + the shown frame + the verdict frame.
            run.result["price_chart"] = fetch_price_chart(
                run.ticker, run.date, run.timeframe, method
            )
            run.result["actionable"] = fetch_actionable_plan(
                run.ticker, run.date, run.timeframe, method
            )
            run.result["timeframe"] = run.timeframe
            run.result["verdict_timeframe"] = run.timeframe
            run.result["timeframes"] = timeframes_for_asset(run.asset_type)
            run.tracker.mark_done()
            final_status = "done"
        except Exception as exc:  # surface, never crash the server
            run.error = f"{type(exc).__name__}: {exc}"
            run.result = {"trace": traceback.format_exc()[-3000:]}
        run.finished_at = time.time()
        run.finished_stamp = timeutil.stamp()
        return final_status

    def _persist(self, run: _Run, status: str) -> None:
        try:
            cost = run.cost()
            elapsed = round((run.finished_at or time.time()) - run.started_at, 1)
            record = {
                "run_id": run.run_id,
                "ticker": run.ticker,
                "date": run.date,
                "asset_type": run.asset_type,
                "status": status,
                "error": run.error,
                "verdict": (run.result or {}).get("verdict") if status == "done" else None,
                "verdict_timeframe": run.timeframe,
                # Method for the manual-confront picker (task 018): reliable from the
                # analyst selection, done or errored.
                "method": "erick" if "erick" in run.selected_analysts else "padrao",
                "cost_usd": cost["usd"],
                "elapsed": elapsed,
                # Manaus wall-clock with explicit -04:00 offset, so the UI can
                # show it verbatim regardless of the browser's timezone.
                "finished_at": run.finished_stamp or timeutil.stamp(),
                "result": run.result,
                "cost": cost,
            }
            self.store.save(record)
        except Exception:
            pass  # history is best-effort; a failed write must not kill the run

    # ----------------------------------------------------- compare (Fase 3) ----
    def start_compare(self, ticker: str, date: str,
                      timeframe: str = _DEFAULT_TIMEFRAME) -> str:
        """Kick off a Padrão × Erick comparison; returns a run_id to poll.

        Runs both readings (reusing a cached prior run for either side when one
        exists for the same ticker/date/timeframe) and confronts them with the
        meta-judge. Cost is up to two pipelines — a cached side costs nothing.

        ``ticker`` may be a company name — resolved to its symbol first (see
        :meth:`start`), so "Microsoft" and "MSFT" both confront the same run.
        """
        raw = (ticker or "").strip()
        if not raw:
            raise ValueError("ticker vazio")
        ticker = (resolve_ticker_query(raw) or raw).strip().upper()
        date = (date or "").strip() or timeutil.today()
        asset_type = self.detect_asset_type(ticker)
        timeframe = (timeframe or _DEFAULT_TIMEFRAME).strip() or _DEFAULT_TIMEFRAME
        allowed = timeframes_for_asset(asset_type)
        if timeframe not in allowed:
            raise ValueError(
                f"timeframe {timeframe!r} indisponível para {asset_type} "
                f"(disponíveis: {', '.join(allowed)})"
            )
        run_id = timeutil.run_id_stamp() + "-cmp" + uuid.uuid4().hex[:4]
        crun = _CompareRun(run_id, ticker, date, asset_type, timeframe)
        with self._lock:
            self._runs[run_id] = crun
        threading.Thread(target=self._compare_worker, args=(crun,), daemon=True).start()
        return run_id

    def _compare_worker(self, crun: _CompareRun) -> None:
        final_status = "error"
        tr = crun.tracker
        try:
            # Etapa 1 — Padrão. Marca 'running' antes de resolver; se voltou do cache
            # (DA-058: lado já existente NÃO re-roda), a etapa vira 'reused'.
            tr.step("padrao", "running")
            tr.set("Padrão", "Resolvendo a leitura Padrão…", 8)
            padrao_rec = self._resolve_side(crun, want_erick=False)
            tr.step("padrao", "reused" if padrao_rec.get("_reused") else "done")
            # Etapa 2 — método Erick (mesma regra de cache).
            tr.step("erick", "running")
            tr.set("Erick", "Resolvendo a leitura pelo método Erick…", 55)
            erick_rec = self._resolve_side(crun, want_erick=True)
            tr.step("erick", "reused" if erick_rec.get("_reused") else "done")
            # Etapa 3 — meta-juiz (sempre roda: é o confronto das duas leituras).
            tr.step("meta", "running")
            tr.set("Meta-juiz", "Confrontando as duas leituras…", 92)
            col_a = build_column(padrao_rec, "padrao")
            col_b = build_column(erick_rec, "erick")
            meta = self._meta_judge(col_a, col_b, crun.asset_type)
            tr.step("meta", "done")
            crun.result = {
                "compare": {"a": col_a, "b": col_b, "meta": meta},
                "verdict": meta.get("verdict"),
                "verdict_timeframe": crun.timeframe,
                "asset_type": crun.asset_type,
            }
            crun.tracker.done()
            final_status = "done"
        except Exception as exc:  # surface, never crash the server
            crun.error = f"{type(exc).__name__}: {exc}"
            crun.result = {"trace": traceback.format_exc()[-3000:]}
        crun.finished_at = time.time()
        crun.finished_stamp = timeutil.stamp()
        self._persist_compare(crun, final_status)
        crun.status = final_status

    def _resolve_side(self, crun: _CompareRun, want_erick: bool) -> dict[str, Any]:
        """Return the full record for one side of the comparison — reusing a
        cached prior run for (ticker, date, timeframe, method) when present, else
        running that pipeline fresh (inline, in this worker thread)."""
        existing = self._find_reusable(
            crun.ticker, crun.date, crun.timeframe, want_erick
        )
        if existing is not None:
            existing = dict(existing)
            existing["_reused"] = True
            return existing
        selected = select_analysts_for_asset(crun.asset_type, include_erick=want_erick)
        sub_id = timeutil.run_id_stamp() + "-" + uuid.uuid4().hex[:6]
        sub = _Run(sub_id, crun.ticker, crun.date, crun.asset_type, selected,
                   timeframe=crun.timeframe)
        # Not registered in ``_runs``: the compare run's coarse progress already
        # covers it, so it should not appear as a second live item. It is still
        # persisted below, so it shows in history and is reusable afterwards.
        status = self._execute(sub)
        self._persist(sub, status)
        sub.status = status
        return self.store.get(sub_id) or {
            "run_id": sub_id, "ticker": crun.ticker, "date": crun.date,
            "asset_type": crun.asset_type, "status": status, "error": sub.error,
            "verdict_timeframe": crun.timeframe, "result": sub.result,
            "cost": sub.cost(),
        }

    def _find_reusable(self, ticker: str, date: str, timeframe: str,
                       want_erick: bool) -> dict[str, Any] | None:
        """Most-recent DONE plain run matching (ticker, date, timeframe) whose
        Erick-presence matches ``want_erick`` — or ``None``. Compare runs are
        skipped (they are not a plain padrão/erick reading)."""
        for summ in self.store.recent(30):
            if summ.get("status") != "done":
                continue
            if (summ.get("ticker") or "").upper() != ticker.upper():
                continue
            if (summ.get("date") or "") != date:
                continue
            if (summ.get("verdict_timeframe") or _DEFAULT_TIMEFRAME) != timeframe:
                continue
            rec = self.store.get(summ["run_id"])
            if not rec or rec.get("status") != "done":
                continue
            res = rec.get("result") or {}
            if res.get("compare"):
                continue  # a comparison record is not a single-method reading
            has_erick = bool((res.get("erick_report") or "").strip())
            if has_erick == want_erick:
                return rec
        return None

    def _persist_compare(self, crun: _CompareRun, status: str) -> None:
        try:
            cost = crun.cost()
            elapsed = round((crun.finished_at or time.time()) - crun.started_at, 1)
            record = {
                "run_id": crun.run_id,
                "ticker": crun.ticker,
                "date": crun.date,
                "asset_type": crun.asset_type,
                "status": status,
                "error": crun.error,
                "verdict": (crun.result or {}).get("verdict") if status == "done" else None,
                "verdict_timeframe": crun.timeframe,
                "method": "compare",  # not a single reading — excluded from confront picker
                "cost_usd": cost["usd"],
                "elapsed": elapsed,
                "finished_at": crun.finished_stamp or timeutil.stamp(),
                "result": crun.result,
                "cost": cost,
                "compare": True,  # marks this record as a comparison
            }
            self.store.save(record)
        except Exception:
            pass

    def _load_record(self, run_id: str) -> dict[str, Any] | None:
        """Full record for a run — from the live table (its snapshot) or disk."""
        with self._lock:
            run = self._runs.get(run_id)
        if run is not None:
            snap = run.snapshot()
            return {
                "run_id": snap.get("run_id"), "ticker": snap.get("ticker"),
                "date": snap.get("date"), "asset_type": snap.get("asset_type"),
                "status": snap.get("status"), "error": snap.get("error"),
                "verdict_timeframe": snap.get("verdict_timeframe"),
                "verdict": (snap.get("result") or {}).get("verdict"),
                "cost": snap.get("cost"), "elapsed": snap.get("elapsed"),
                "result": snap.get("result"),
            }
        return self.store.get(run_id)

    def confront(self, id_a: str, id_b: str) -> dict[str, Any]:
        """Confront two analyses of the same ticker — ALWAYS Padrão × Erick on the
        same timeframe/date (Samyr's rule, task 024). Never 'método contra ele mesmo'.

        If the two picked runs already are a valid Padrão × Erick pair on the same
        frame and date, meta-judge them directly (free, no re-run, the exact runs
        chosen). Anything else — two of the same method, or mismatched frames/dates —
        is NOT a confront: it reroutes through :meth:`start_compare`, anchored on the
        open run A, which reuses the cached side and runs ONLY the missing method so
        the outcome is a true Padrão × Erick.

        Returns a done snapshot (``result.compare`` populated) for the direct case,
        or ``{"run_id": ..., "rerouted": True}`` for the rerouted (async) case — the
        caller polls that run_id like any other.
        """
        if not id_a or not id_b:
            raise ValueError("selecione duas análises")
        if id_a == id_b:
            raise ValueError("selecione duas análises diferentes")
        rec_a, rec_b = self._load_record(id_a), self._load_record(id_b)
        if rec_a is None or rec_b is None:
            raise ValueError("análise não encontrada")
        ta = (rec_a.get("ticker") or "").upper()
        tb = (rec_b.get("ticker") or "").upper()
        if ta != tb or not ta:
            raise ValueError("as duas análises precisam ser do mesmo ativo")
        if detect_method(rec_a) == "compare" or detect_method(rec_b) == "compare":
            raise ValueError("selecione análises simples, não uma comparação")

        col_a = build_column(rec_a, detect_method(rec_a))
        col_b = build_column(rec_b, detect_method(rec_b))

        # Not a real confront (same method, or different frame/date) → reroute to a
        # true Padrão × Erick compare, anchored on run A (ticker/date/frame the user
        # is looking at). start_compare reuses the cached side and runs only the
        # missing method — impossible to produce método×ele-mesmo by this door.
        if not confront_pair_valid(col_a, col_b):
            run_id = self.start_compare(
                ta, rec_a.get("date") or "",
                timeframe=col_a.get("timeframe") or _DEFAULT_TIMEFRAME,
            )
            return {"run_id": run_id, "rerouted": True, "ticker": ta,
                    "status": "running"}

        # Valid pair: keep Padrão first / Erick second so the header always reads
        # "Padrão · X × Método Erick · X" regardless of which side was picked first.
        if col_a.get("method") != "padrao":
            col_a, col_b = col_b, col_a
        asset_type = rec_a.get("asset_type") or rec_b.get("asset_type") or "stock"
        meta = self._meta_judge(col_a, col_b, asset_type)

        run_id = timeutil.run_id_stamp() + "-cmp" + uuid.uuid4().hex[:4]
        crun = _CompareRun(run_id, ta, col_a.get("date") or rec_a.get("date") or "",
                           asset_type, col_a.get("timeframe") or _DEFAULT_TIMEFRAME)
        crun.result = {
            "compare": {"a": col_a, "b": col_b, "meta": meta, "manual": True},
            "verdict": meta.get("verdict"),
            "verdict_timeframe": crun.timeframe,
            "asset_type": asset_type,
        }
        crun.tracker.done()
        crun.finished_at = time.time()
        crun.finished_stamp = timeutil.stamp()
        crun.status = "done"
        self._persist_compare(crun, "done")
        return crun.snapshot()

    # ------------------------------------------------------- Q&A ancorado ----
    def ask(self, run_id: str, question: str) -> dict[str, Any] | None:
        """Responde uma pergunta ANCORADA nos dados JÁ computados de uma run.

        Não re-roda a análise nem toca em dado externo: monta o contexto
        (price_structure + veredito + relatórios que a run já cacheou) e chama o
        modelo BARATO (``quick_think_llm``) UMA vez, medindo o custo. O grounding
        e a honestidade ("não dá pra afirmar") vivem no prompt de
        :mod:`tradingagents.webui.ask`. Retorna ``None`` se a run é desconhecida;
        levanta ``ValueError`` se a pergunta vier vazia."""
        question = (question or "").strip()
        if not question:
            raise ValueError("faça uma pergunta")
        question = question[:_MAX_QUESTION_CHARS]
        record = self._load_record(run_id)
        if record is None:
            return None

        messages, meta = ask_module.build_messages(record, question)
        usage_cb = UsageMetadataCallbackHandler()
        llm = self._answer_llm([usage_cb])
        reply = llm.invoke(messages)
        answer = getattr(reply, "content", reply)
        # Alguns provedores devolvem o conteúdo em "partes" (lista) em vez de texto.
        if isinstance(answer, list):
            answer = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in answer
            )
        return {
            "run_id": record.get("run_id") or run_id,
            "question": question,
            "answer": str(answer).strip(),
            "cost": cost_breakdown(usage_cb.usage_metadata),
            "model": self.base_config.get("quick_think_llm"),
            "mode": meta.get("mode"),
            "grounded": bool(meta.get("has_numbers")),
            "timeframe": meta.get("timeframe"),
            "as_of": meta.get("as_of"),
        }

    def _answer_llm(self, callbacks: list):
        """Chat client BARATO pra responder perguntas (``quick_think_llm``), com os
        mesmos knobs de provedor da run e os callbacks de uso pra medir o custo."""
        from tradingagents.llm_clients import create_llm_client
        cfg = self.base_config
        kwargs: dict[str, Any] = {"callbacks": callbacks}
        provider = (cfg.get("llm_provider") or "openai").lower()
        temperature = cfg.get("temperature")
        if temperature is not None and temperature != "":
            kwargs["temperature"] = float(temperature)
        if provider == "openai" and cfg.get("openai_reasoning_effort"):
            kwargs["reasoning_effort"] = cfg["openai_reasoning_effort"]
        elif provider == "anthropic" and cfg.get("anthropic_effort"):
            kwargs["effort"] = cfg["anthropic_effort"]
        elif provider == "google" and cfg.get("google_thinking_level"):
            kwargs["thinking_level"] = cfg["google_thinking_level"]
        max_retries = cfg.get("llm_max_retries")
        if max_retries is not None and max_retries != "":
            kwargs["max_retries"] = int(max_retries)
        client = create_llm_client(
            provider=provider,
            model=cfg["quick_think_llm"],
            base_url=cfg.get("backend_url"),
            **kwargs,
        )
        return client.get_llm()

    def status(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            run = self._runs.get(run_id)
        if run is not None:
            return run.snapshot()
        # fall back to persisted history for a run this process didn't start
        record = self.store.get(run_id)
        if record is None:
            return None
        return {
            "run_id": record["run_id"],
            "ticker": record.get("ticker"),
            "date": record.get("date"),
            "asset_type": record.get("asset_type"),
            "status": record.get("status"),
            "error": record.get("error"),
            "verdict_timeframe": record.get("verdict_timeframe")
                or (record.get("result") or {}).get("verdict_timeframe")
                or _DEFAULT_TIMEFRAME,
            "progress": {"percent": 100, "phase": "Concluído", "label": "Do histórico",
                          "index": 0, "total": 0, "elapsed": record.get("elapsed"),
                          "plan": [], "reached": []},
            "cost": record.get("cost", {"usd": record.get("cost_usd", 0)}),
            "elapsed": record.get("elapsed"),
            "result": record.get("result"),
            "from_history": True,
        }

    @staticmethod
    def _running_summary(run: _Run) -> dict[str, Any]:
        """History-shaped summary for a run still executing in this process.

        Same keys the on-disk index carries (so the UI renders it with the same
        code path) plus a light ``progress`` (percent + phase) for the live
        em-andamento marker. The full progress feed still comes from
        ``/api/status/<run_id>`` when the run is opened.
        """
        prog = run.tracker.snapshot()
        cost = run.cost()
        return {
            "run_id": run.run_id,
            "ticker": run.ticker,
            "date": run.date,
            "asset_type": run.asset_type,
            "status": "running",
            "verdict": None,
            "cost_usd": cost.get("usd", 0),
            "elapsed": round(time.time() - run.started_at, 1),
            "finished_at": None,
            "progress": {"percent": prog.get("percent", 0), "phase": prog.get("phase", "")},
        }

    def active_runs(self) -> list[dict[str, Any]]:
        """Summaries for the runs still executing in THIS process, newest-first.

        Each analysis runs on its own daemon thread and keeps computing even after
        the client navigates to another ticker, reloads the page, or closes the
        tab. Those live runs are not in the on-disk history yet (that write happens
        only on completion), so without surfacing them the UI has no way to show an
        "em andamento" marker or re-open a running analysis. Merged in front of the
        persisted history by :meth:`history`.
        """
        with self._lock:
            live = [r for r in self._runs.values() if r.status == "running"]
        live.sort(key=lambda r: r.started_at, reverse=True)
        return [self._running_summary(r) for r in live]

    def history(self, limit: int = 25) -> list[dict[str, Any]]:
        """Recent run summaries newest-first, with the live in-process runs
        (status ``running``) merged in front.

        A running run is not on disk yet, so it can only come from the in-memory
        table; a run that just finished is deduped out of the live set so it is
        not listed twice while it is briefly in both places.
        """
        live = self.active_runs()
        seen = {r["run_id"] for r in live}
        persisted = [r for r in self.store.recent(limit) if r.get("run_id") not in seen]
        return live + persisted

    def search_symbols(self, term: str, limit: int = 8) -> list[dict[str, Any]]:
        """Autocomplete candidates for a name-or-ticker term (fail-open)."""
        return fetch_symbol_search(term, limit)

    def resolve_names(self, symbols: list[str]) -> dict[str, str]:
        """Batch symbol -> display name for the UI chips/header (fail-open)."""
        return fetch_symbol_names(symbols)

    def config_info(self) -> dict[str, Any]:
        """Client bootstrap: the authoritative *Manaus* today + tz for the UI.

        The date selector defaults to this, not to the browser's clock, so a
        laptop set to another timezone still runs under Samyr's day.
        """
        return {
            "today": timeutil.today(),
            "now": timeutil.stamp(),
            "tz": timeutil.TZ_NAME,
            "tz_label": timeutil.TZ_LABEL,
        }

    def timeframe_view(self, ticker: str, date: str, timeframe: str,
                       method: str = "padrao") -> dict[str, Any]:
        """Recompute the chart + actionable plan for ``timeframe`` on demand.

        Backs the ``/api/chart`` endpoint the timeframe selector calls: the region,
        1-2-3 and bands are re-detected on the chosen frame's own series (daily from
        the cached yfinance series; 4h/1h/15m from the keyless intraday source —
        the exchange for crypto, yfinance for an equity). ``method`` keeps the
        structure family consistent with the open analysis (Erick EMA 8/21 / Padrão
        MMS) when the user flips timeframe. Everything reuses the DA-058 caches, so
        flipping back to a frame already fetched costs zero network.

        Honesty guards:

        * an unsupported frame (e.g. a ``3m`` that no asset offers) is a
          ``ValueError`` — the UI only renders the operable ladder;
        * an intraday **source outage** (feed down → empty candles, ``sem_dado``
          plan) is NOT fabricated: the view degrades to the daily frame and flags
          ``degraded`` with a pt-BR ``notice`` so the UI can say so plainly;
        * a frame the source genuinely has no candle for (``intradiario_indisponivel``
          — e.g. an equity backtest beyond yfinance's ~60-day intraday window) is
          left on the requested frame with an honest "indisponível" plan, not swapped.
        """
        ticker = (ticker or "").strip().upper()
        date = (date or "").strip() or timeutil.today()
        if not ticker:
            raise ValueError("ticker vazio")
        asset_type = self.detect_asset_type(ticker)
        allowed = timeframes_for_asset(asset_type)
        if timeframe not in allowed:
            raise ValueError(
                f"timeframe {timeframe!r} indisponível para {asset_type} "
                f"(disponíveis: {', '.join(allowed)})"
            )

        chart = fetch_price_chart(ticker, date, timeframe, method)
        plan = fetch_actionable_plan(ticker, date, timeframe, method)

        degraded = False
        notice: str | None = None
        requested = timeframe
        has_candles = bool((chart or {}).get("candles"))
        # An intraday frame that came back empty with a ``sem_dado`` plan means the
        # feed is momentarily down → degrade to the daily. A genuine
        # "intradiario_indisponivel" plan (the source truly has no candle for this
        # symbol/date, e.g. an out-of-window equity backtest) is expected and left
        # on the requested frame — the UI shows "indisponível", nothing is invented.
        if timeframe != _DEFAULT_TIMEFRAME and not has_candles \
                and (plan or {}).get("setup_state") != "intradiario_indisponivel":
            degraded = True
            timeframe = _DEFAULT_TIMEFRAME
            notice = (
                "Fonte intradiária indisponível agora — mostrando o diário. "
                "Nenhuma barra inventada."
            )
            chart = fetch_price_chart(ticker, date, timeframe, method)
            plan = fetch_actionable_plan(ticker, date, timeframe, method)

        return {
            "ticker": ticker,
            "date": date,
            "asset_type": asset_type,
            "timeframe": timeframe,
            "requested": requested,
            "timeframes": allowed,
            "degraded": degraded,
            "notice": notice,
            "price_chart": chart,
            "actionable": plan,
        }

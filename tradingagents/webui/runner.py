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

from tradingagents.webui import timeutil
from tradingagents.webui.pricing import cost_breakdown
from tradingagents.webui.progress import ProgressCallbackHandler, ProgressTracker
from tradingagents.webui.store import HistoryStore

# Default analyst order; crypto drops fundamentals (no balance sheet for a coin).
_ANALYST_ORDER = ("market", "social", "news", "fundamentals")

# The chart's timeframe selector, by asset type, ordered widest→narrowest
# (semanal · diário · 4h · 1h · 15m). The weekly frame is resampled in memory from
# the daily series, so it is operable for BOTH stocks and crypto. Crypto also gets
# the intraday ladder (real keyless exchange candles); an equity has no keyless
# intraday feed, so those buttons show disabled ("indisponível para ação") rather
# than inventing a bar. Daily (``_DEFAULT_TIMEFRAME``) stays the frame every
# analysis is first computed on — the selector re-derives the others on demand.
_CRYPTO_TIMEFRAMES = ("1w", "1d", "4h", "1h", "15m")
_STOCK_TIMEFRAMES = ("1w", "1d")
_DEFAULT_TIMEFRAME = "1d"


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
    """Operable chart timeframes for an asset type (crypto has the intraday ladder;
    an equity only the daily). The single source of truth the UI selector and the
    ``/api/chart`` validation both read, so they can never disagree."""
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


def fetch_price_chart(ticker: str, date: str, timeframe: str = _DEFAULT_TIMEFRAME) -> dict[str, Any]:
    """Candle + moving-average + setup-marker payload for the UI chart.

    ``timeframe`` selects the frame (daily default, or ``"4h"``/``"1h"``/``"15m"``
    intraday for crypto — real keyless exchange candles). Reuses the same cached,
    date-guarded series the detector runs on, so it is free and cannot see a future
    candle. Fail-open: returns an empty payload on any error so a chart hiccup never
    blocks the analysis result.
    """
    try:
        from tradingagents.dataflows.price_structure import build_price_chart
        return build_price_chart(ticker, date, timeframe=timeframe)
    except Exception:
        return {}


def fetch_actionable_plan(ticker: str, date: str, timeframe: str = _DEFAULT_TIMEFRAME) -> dict[str, Any]:
    """Operable levels for the verdict header (price @ analysis, horizon,
    timeframe, buy/realize/pullback zones).

    ``timeframe`` selects the frame (daily default, or an intraday frame for
    crypto). Reuses the same cached, date-guarded series and the detector's own
    structure, so it is free and cannot see a future candle. Fail-open: returns
    ``{}`` on any error so this enrichment never blocks the analysis result.
    """
    try:
        from tradingagents.dataflows.price_structure import build_actionable_plan_dict
        return build_actionable_plan_dict(ticker, date, timeframe=timeframe)
    except Exception:
        return {}


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


class AnalysisRunner:
    """Starts analyses and tracks their live status."""

    def __init__(self, base_config: dict[str, Any] | None = None,
                 store: HistoryStore | None = None,
                 graph_factory=None):
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
        self._runs: dict[str, _Run] = {}
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
        """
        ticker = (ticker or "").strip().upper()
        if not ticker:
            raise ValueError("ticker vazio")
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
            # The chart opens on the SAME frame the verdict was computed on, so the
            # picture matches the stamp; the UI can still recalc other frames via
            # /api/chart. Persist the ladder + the shown frame + the verdict frame.
            run.result["price_chart"] = fetch_price_chart(
                run.ticker, run.date, run.timeframe
            )
            run.result["actionable"] = fetch_actionable_plan(
                run.ticker, run.date, run.timeframe
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
        self._persist(run, final_status)
        run.status = final_status

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

    def timeframe_view(self, ticker: str, date: str, timeframe: str) -> dict[str, Any]:
        """Recompute the chart + actionable plan for ``timeframe`` on demand.

        Backs the ``/api/chart`` endpoint the timeframe selector calls: the region,
        1-2-3 and bands are re-detected on the chosen frame's own series (daily from
        the cached yfinance series; 4h/1h/15m from the keyless exchange for crypto).
        Everything reuses the DA-058 caches, so flipping back to a frame already
        fetched costs zero network.

        Honesty guards:

        * an unsupported frame for the asset (e.g. ``15m`` on an equity) is a
          ``ValueError`` — the UI already disables those buttons;
        * a crypto intraday **source outage** (feed down → no candle) is NOT
          fabricated: the view degrades to the daily frame and flags ``degraded``
          with a pt-BR ``notice`` so the UI can say so plainly.
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

        chart = fetch_price_chart(ticker, date, timeframe)
        plan = fetch_actionable_plan(ticker, date, timeframe)

        degraded = False
        notice: str | None = None
        requested = timeframe
        has_candles = bool((chart or {}).get("candles"))
        # A crypto intraday frame that came back empty means the exchange feed is
        # down (a non-crypto frame would have been rejected above; a genuine
        # "intradiário indisponível para ação" plan is expected and left alone).
        if timeframe != _DEFAULT_TIMEFRAME and not has_candles \
                and (plan or {}).get("setup_state") != "intradiario_indisponivel":
            degraded = True
            timeframe = _DEFAULT_TIMEFRAME
            notice = (
                "Fonte intradiária indisponível agora — mostrando o diário. "
                "Nenhuma barra inventada."
            )
            chart = fetch_price_chart(ticker, date, timeframe)
            plan = fetch_actionable_plan(ticker, date, timeframe)

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

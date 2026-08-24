"""Padrão × Erick comparison + meta-judge (Fase 3, task 017).

The meta-judge is deterministic and anchored in the two real verdicts/timeframes;
the orchestrator runs both readings, reuses a cached side, and reports a combined
cost. A fake engine stands in for the LLM pipeline.
"""

import time

import pytest

import tradingagents.webui.runner as rm
from tradingagents.webui.compare import build_column, deterministic_meta
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.store import HistoryStore
from tests.test_webui_runner import FINAL_STATE, _FakeGraph


@pytest.fixture(autouse=True)
def _stub_fetches(monkeypatch):
    monkeypatch.setattr(rm, "fetch_price_chart", lambda t, d, tf="1d": {})
    monkeypatch.setattr(rm, "fetch_actionable_plan",
                        lambda t, d, tf="1d": {"setup_state": "aguardar_pullback", "timeframe": tf})
    monkeypatch.setattr(rm, "fetch_derivatives_report", lambda t, d: "")


def _dual_factory():
    """Padrão → Buy; Erick (analyst present) → Hold with an erick_report, so the
    two sides genuinely diverge and the Erick side is cache-reusable."""
    def make(config, selected, callbacks):
        if "erick" in selected:
            fs = {**FINAL_STATE, "erick_report": "Erick: aguardar o recuo à média (EMA 8/21)."}
            return _FakeGraph(callbacks, fs, "Hold")
        return _FakeGraph(callbacks, FINAL_STATE, "Buy")
    return make


def _wait(runner, run_id, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = runner.status(run_id)
        if s and s["status"] != "running":
            return s
        time.sleep(0.03)
    raise AssertionError("compare did not finish in time")


# ---------------------------------------------------------- meta-judge units ---
def test_meta_agree():
    m = deterministic_meta(
        {"verdict": "Buy", "timeframe": "1d"},
        {"verdict": "Buy", "timeframe": "4h", "actionable": {}},
    )
    assert m["agreement"] == "concordam"
    assert m["verdict"] == "Buy"
    assert "COMPRAR" in m["headline"]


def test_meta_diverge_picks_conservative_and_explains():
    m = deterministic_meta(
        {"verdict": "Buy", "timeframe": "1d"},
        {"verdict": "Hold", "timeframe": "4h", "actionable": {"setup_state": "aguardar_pullback"}},
    )
    assert m["agreement"] == "divergem"
    assert m["verdict"] == "Hold"          # more conservative side
    assert "Divergem" in m["headline"]
    assert "timing" in m["significado"].lower()   # anchored decision meaning, not vague
    assert "Padrão" in m["divergencia"] and "Erick" in m["divergencia"]


def test_meta_partial_when_one_side_missing():
    m = deterministic_meta({"verdict": "Buy", "timeframe": "1d"},
                           {"verdict": None, "timeframe": "1d"})
    assert m["agreement"] == "parcial"


def test_build_column_reads_record():
    rec = {
        "run_id": "x", "verdict": "Buy", "verdict_timeframe": "4h",
        "cost": {"usd": 0.02}, "elapsed": 10, "status": "done",
        "result": {"verdict": "Buy", "verdict_timeframe": "4h",
                   "trader_plan": "entrar metade", "erick_report": "",
                   "actionable": {}, "degraded": []},
    }
    c = build_column(rec, "padrao")
    assert c["method"] == "padrao" and c["verdict"] == "Buy" and c["timeframe"] == "4h"
    assert c["trader_plan"] == "entrar metade" and c["reused"] is False


# ------------------------------------------------------- runner integration ---
def test_start_compare_two_columns_and_meta(tmp_path):
    r = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                       store=HistoryStore(tmp_path), graph_factory=_dual_factory())
    snap = _wait(r, r.start_compare("BTC-USD", "2026-08-22", timeframe="1d"))
    assert snap["status"] == "done"
    cmp = snap["result"]["compare"]
    assert cmp["padrao"]["verdict"] == "Buy"
    assert cmp["erick"]["verdict"] == "Hold"
    assert cmp["erick"]["erick_report"]
    assert cmp["meta"]["agreement"] == "divergem"
    assert snap["cost"]["usd"] > 0                 # two pipelines summed
    assert r.store.get(snap["run_id"])["compare"] is True   # persisted + marked


def test_compare_reuses_cached_sides(tmp_path):
    r = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                       store=HistoryStore(tmp_path), graph_factory=_dual_factory())
    _wait(r, r.start_compare("BTC-USD", "2026-08-22"))          # populates cache
    snap2 = _wait(r, r.start_compare("BTC-USD", "2026-08-22"))  # should reuse both
    cmp = snap2["result"]["compare"]
    assert cmp["padrao"]["reused"] is True
    assert cmp["erick"]["reused"] is True


def test_simple_analysis_still_works(tmp_path):
    """Compare is additive — the plain single-method run is untouched."""
    r = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                       store=HistoryStore(tmp_path), graph_factory=_dual_factory())
    snap = _wait(r, r.start("BTC-USD", "2026-08-22", method="padrao"))
    assert snap["status"] == "done"
    assert "compare" not in (snap["result"] or {})

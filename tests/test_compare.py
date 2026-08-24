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
        {"verdict": "Buy", "timeframe": "1d", "method": "padrao", "date": "2026-08-22"},
        {"verdict": "Buy", "timeframe": "4h", "method": "erick", "date": "2026-08-22", "actionable": {}},
    )
    assert m["agreement"] == "concordam"
    assert m["verdict"] == "Buy"
    assert "COMPRAR" in m["headline"]


def test_meta_diverge_picks_conservative_and_explains():
    m = deterministic_meta(
        {"verdict": "Buy", "timeframe": "1d", "method": "padrao", "date": "2026-08-22"},
        {"verdict": "Hold", "timeframe": "4h", "method": "erick", "date": "2026-08-22",
         "actionable": {"setup_state": "aguardar_pullback"}},
    )
    assert m["agreement"] == "divergem"
    assert m["verdict"] == "Hold"          # more conservative side
    assert "Divergem" in m["headline"]
    assert "timing" in m["significado"].lower()   # anchored decision meaning, not vague
    # names WHICH two are being compared (criterion 4)
    assert "Padrão" in m["divergencia"] and "Erick" in m["divergencia"]
    assert "Padrão" in m["label_a"] and "Erick" in m["label_b"]


def test_meta_same_method_is_error_never_concordam():
    """Same method against itself is NOT a confront (Samyr's rule, task 024): it is
    an error state, and must never be labelled 'Concordam' — even when both agree."""
    m = deterministic_meta(
        {"verdict": "Hold", "timeframe": "1d", "method": "padrao", "date": "2026-08-22"},
        {"verdict": "Hold", "timeframe": "1d", "method": "padrao", "date": "2026-08-22"},
    )
    assert m["agreement"] == "invalido"
    assert "Concordam" not in m["headline"]
    assert "Padrão × Erick" in m["headline"]
    # two timeframes of the same method is equally invalid (no trend/timing story)
    m2 = deterministic_meta(
        {"verdict": "Buy", "timeframe": "1d", "method": "padrao", "date": "2026-08-22"},
        {"verdict": "Hold", "timeframe": "4h", "method": "padrao", "date": "2026-08-22"},
    )
    assert m2["agreement"] == "invalido"


def test_meta_partial_when_one_side_missing():
    m = deterministic_meta({"verdict": "Buy", "timeframe": "1d", "method": "padrao"},
                           {"verdict": None, "timeframe": "1d", "method": "erick"})
    assert m["agreement"] == "parcial"


def test_build_column_reads_record():
    rec = {
        "run_id": "x", "ticker": "BTC-USD", "date": "2026-08-22",
        "verdict": "Buy", "verdict_timeframe": "4h",
        "cost": {"usd": 0.02}, "elapsed": 10, "status": "done",
        "result": {"verdict": "Buy", "verdict_timeframe": "4h",
                   "trader_plan": "entrar metade", "erick_report": "",
                   "actionable": {}, "degraded": []},
    }
    c = build_column(rec, "padrao")
    assert c["method"] == "padrao" and c["verdict"] == "Buy" and c["timeframe"] == "4h"
    assert c["trader_plan"] == "entrar metade" and c["reused"] is False
    assert "Padrão" in c["label"] and c["date"] == "2026-08-22"


# ------------------------------------------------------- runner integration ---
def test_start_compare_two_columns_and_meta(tmp_path):
    r = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                       store=HistoryStore(tmp_path), graph_factory=_dual_factory())
    snap = _wait(r, r.start_compare("BTC-USD", "2026-08-22", timeframe="1d"))
    assert snap["status"] == "done"
    cmp = snap["result"]["compare"]
    assert cmp["a"]["method"] == "padrao" and cmp["a"]["verdict"] == "Buy"
    assert cmp["b"]["method"] == "erick" and cmp["b"]["verdict"] == "Hold"
    assert cmp["b"]["erick_report"]
    assert cmp["meta"]["agreement"] == "divergem"
    assert snap["cost"]["usd"] > 0                 # two pipelines summed
    assert r.store.get(snap["run_id"])["compare"] is True   # persisted + marked


def test_compare_reuses_cached_sides(tmp_path):
    r = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                       store=HistoryStore(tmp_path), graph_factory=_dual_factory())
    _wait(r, r.start_compare("BTC-USD", "2026-08-22"))          # populates cache
    snap2 = _wait(r, r.start_compare("BTC-USD", "2026-08-22"))  # should reuse both
    cmp = snap2["result"]["compare"]
    assert cmp["a"]["reused"] is True
    assert cmp["b"]["reused"] is True


# ------------------------------------------------ manual confront (task 018) ---
def test_confront_two_existing_runs(tmp_path):
    r = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                       store=HistoryStore(tmp_path), graph_factory=_dual_factory())
    rid_p = r.start("BTC-USD", "2026-08-22", method="padrao"); _wait(r, rid_p)
    rid_e = r.start("BTC-USD", "2026-08-22", method="erick"); _wait(r, rid_e)
    snap = r.confront(rid_p, rid_e)
    assert snap["status"] == "done"
    cmp = snap["result"]["compare"]
    assert cmp["manual"] is True
    assert cmp["a"]["method"] == "padrao" and cmp["b"]["method"] == "erick"
    assert cmp["meta"]["agreement"] == "divergem"
    # names exactly which two readings (criterion 4)
    assert "Padrão" in cmp["meta"]["label_a"] and "Erick" in cmp["meta"]["label_b"]
    # persisted as a compare record, openable
    assert r.store.get(snap["run_id"])["compare"] is True


def test_confront_valid_pair_orders_padrao_first(tmp_path):
    """A valid Padrão × Erick pair is confronted directly (free, no re-run); the
    header always reads Padrão first even if Erick was picked as side A."""
    r = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                       store=HistoryStore(tmp_path), graph_factory=_dual_factory())
    rid_e = r.start("BTC-USD", "2026-08-22", method="erick"); _wait(r, rid_e)
    rid_p = r.start("BTC-USD", "2026-08-22", method="padrao"); _wait(r, rid_p)
    out = r.confront(rid_e, rid_p)              # Erick picked first
    assert "rerouted" not in out               # direct: it was already a valid pair
    cmp = out["result"]["compare"]
    assert cmp["manual"] is True
    assert cmp["a"]["method"] == "padrao" and cmp["b"]["method"] == "erick"


def test_confront_same_method_reroutes_to_padrao_erick(tmp_path):
    """SPCX bug (task 024): confronting two runs of the SAME method can no longer
    produce 'Padrão × Padrão'. It reroutes to a real Padrão × Erick compare that
    reuses the cached side and runs only the missing method."""
    r = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                       store=HistoryStore(tmp_path), graph_factory=_dual_factory())
    # history has ONLY Padrão readings — exactly the SPCX situation
    rid_a = r.start("SPCX", "2026-08-22", method="padrao"); _wait(r, rid_a)
    rid_b = r.start("SPCX", "2026-08-22", method="padrao"); _wait(r, rid_b)
    out = r.confront(rid_a, rid_b)
    # not a direct confront: rerouted to an async Padrão × Erick run
    assert out.get("rerouted") is True and "run_id" in out
    assert "compare" not in (out.get("result") or {})
    snap = _wait(r, out["run_id"])
    cmp = snap["result"]["compare"]
    assert cmp["a"]["method"] == "padrao" and cmp["b"]["method"] == "erick"
    assert cmp["b"]["erick_report"]                 # the missing method actually ran
    assert cmp["a"]["reused"] is True               # the existing Padrão was reused
    assert cmp["meta"]["agreement"] != "invalido"   # a real confront, not método×ele-mesmo


def test_confront_mismatched_timeframe_reroutes(tmp_path):
    """Padrão × Erick but on DIFFERENT frames is not 'same timeframe' → reroute to a
    fresh same-frame Padrão × Erick anchored on the open run A."""
    r = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                       store=HistoryStore(tmp_path), graph_factory=_dual_factory())
    rid_p1d = r.start("BTC-USD", "2026-08-22", method="padrao", timeframe="1d"); _wait(r, rid_p1d)
    rid_e4h = r.start("BTC-USD", "2026-08-22", method="erick", timeframe="4h"); _wait(r, rid_e4h)
    out = r.confront(rid_p1d, rid_e4h)
    assert out.get("rerouted") is True
    snap = _wait(r, out["run_id"])
    cmp = snap["result"]["compare"]
    # anchored on A (1d): both sides on the same frame now
    assert cmp["a"]["timeframe"] == "1d" and cmp["b"]["timeframe"] == "1d"
    assert cmp["a"]["method"] == "padrao" and cmp["b"]["method"] == "erick"


def test_confront_rejects_different_tickers(tmp_path):
    r = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                       store=HistoryStore(tmp_path), graph_factory=_dual_factory())
    rid1 = r.start("BTC-USD", "2026-08-22"); _wait(r, rid1)
    rid2 = r.start("ETH-USD", "2026-08-22"); _wait(r, rid2)
    with pytest.raises(ValueError):
        r.confront(rid1, rid2)


def test_confront_rejects_same_run(tmp_path):
    r = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                       store=HistoryStore(tmp_path), graph_factory=_dual_factory())
    rid = r.start("BTC-USD", "2026-08-22"); _wait(r, rid)
    with pytest.raises(ValueError):
        r.confront(rid, rid)


def test_simple_analysis_still_works(tmp_path):
    """Compare is additive — the plain single-method run is untouched."""
    r = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                       store=HistoryStore(tmp_path), graph_factory=_dual_factory())
    snap = _wait(r, r.start("BTC-USD", "2026-08-22", method="padrao"))
    assert snap["status"] == "done"
    assert "compare" not in (snap["result"] or {})

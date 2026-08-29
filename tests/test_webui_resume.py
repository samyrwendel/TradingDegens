"""Checkpoint/resume + cross-run reuse (task 022).

A deploy/restart must never throw away a run's work, and re-analysing the same
ticker+date+timeframe+method must reuse the completed one HONESTLY (marked
``reused``, cost zero) instead of paying for the pipeline again — but only while
the inputs are still identical (DA-058 same-day freshness).

A fake graph stands in for TradingAgentsGraph so these tests never call an LLM.
The real per-node LangGraph resume is covered by test_checkpoint_resume.py; here
we drive the runner-level ORCHESTRATION (reuse decision, resume descriptors,
boot re-enqueue) with a counting fake so "did the pipeline run again?" is a hard
assertion, not an inference.
"""

import threading

import pytest

import tradingagents.webui.runner as runner_module
from tests.test_webui_runner import FINAL_STATE, _blocking_factory, _FakeGraph, _wait
from tradingagents.webui.runner import AnalysisRunner, timeutil
from tradingagents.webui.store import HistoryStore


@pytest.fixture(autouse=True)
def _stub_enrich(monkeypatch):
    """Keep the worker hermetic (no network): the post-graph chart/plan/derivatives
    enrichments are stubbed — their own coverage lives elsewhere."""
    monkeypatch.setattr(runner_module, "fetch_price_chart",
                        lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_actionable_plan",
                        lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_derivatives_report", lambda t, d: "")


def _counting_factory(calls, final_state=FINAL_STATE, signal="Buy"):
    """A graph factory that records every time the pipeline is actually built/run,
    so a reused analysis can be proven to have skipped it entirely."""
    def make(config, selected_analysts, callbacks):
        calls.append({"selected": tuple(selected_analysts)})
        return _FakeGraph(callbacks, final_state, signal)
    return make


def _runner(tmp_path, calls):
    return AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                          store=HistoryStore(tmp_path),
                          graph_factory=_counting_factory(calls))


# --------------------------------------------------- reuse across runs (P3) ---
def test_identical_rerun_reuses_completed(tmp_path):
    """Two identical runs in a row: the 2nd returns the 1st's result intact,
    marked reused, at zero cost, WITHOUT running the pipeline again."""
    calls: list = []
    runner = _runner(tmp_path, calls)

    r1 = runner.start("AAPL", "2020-01-02")          # historical → immutable data
    s1 = _wait(runner, r1)
    assert s1["status"] == "done" and s1["reused"] is False
    assert len(calls) == 1

    r2 = runner.start("AAPL", "2020-01-02")
    s2 = _wait(runner, r2)
    assert s2["status"] == "done"
    assert s2["reused"] is True
    assert s2["result"]["reused"] is True
    assert s2["result"]["reused_from"] == r1
    assert s2["result"]["verdict"] == s1["result"]["verdict"]
    assert s2["cost"]["usd"] == 0                     # reuse costs nothing
    assert len(calls) == 1                            # pipeline did NOT run twice


def test_force_fresh_bypasses_reuse(tmp_path):
    """``reuse=False`` (the UI's 'reanalisar do zero') recomputes even when an
    identical completed run exists."""
    calls: list = []
    runner = _runner(tmp_path, calls)
    r1 = runner.start("AAPL", "2020-01-02")
    _wait(runner, r1)
    r2 = runner.start("AAPL", "2020-01-02", reuse=False)
    s2 = _wait(runner, r2)
    assert s2["reused"] is False
    assert len(calls) == 2                            # forced a real re-run


def test_different_method_does_not_reuse(tmp_path):
    """Padrão and Erick are different reads of the same ticker/date — one must not
    be served as the other."""
    calls: list = []
    runner = _runner(tmp_path, calls)
    r1 = runner.start("AAPL", "2020-01-02", method="padrao")
    _wait(runner, r1)
    r2 = runner.start("AAPL", "2020-01-02", method="erick")
    s2 = _wait(runner, r2)
    assert s2["reused"] is False
    assert len(calls) == 2


# --------------------------- reuse freshness — both sides (P3 correção, P4) ---
def test_same_day_reuses_within_freshness_window(tmp_path):
    """Same-day re-run inside the freshness window reuses (the live bar hasn't
    changed) — the identical-input side of the cache correctness rule."""
    calls: list = []
    runner = _runner(tmp_path, calls)
    runner.reuse_same_day_ttl = 10_000               # comfortably fresh
    today = timeutil.today()
    r1 = runner.start("AAPL", today)
    _wait(runner, r1)
    r2 = runner.start("AAPL", today)
    s2 = _wait(runner, r2)
    assert s2["reused"] is True
    assert len(calls) == 1


def test_same_day_recomputes_when_stale(tmp_path):
    """Same-day re-run past the freshness window recomputes: the live data may have
    refreshed (DA-058), so a stale judgment must NOT be reused — the changed-input
    side of the rule."""
    calls: list = []
    runner = _runner(tmp_path, calls)
    runner.reuse_same_day_ttl = 0                     # any same-day run is stale now
    today = timeutil.today()
    r1 = runner.start("AAPL", today)
    _wait(runner, r1)
    r2 = runner.start("AAPL", today)
    s2 = _wait(runner, r2)
    assert s2["reused"] is False
    assert len(calls) == 2                            # recomputed, not reused


def test_historical_always_reuses_regardless_of_ttl(tmp_path):
    """A past date is immutable data — reuse holds even with a zero same-day TTL."""
    calls: list = []
    runner = _runner(tmp_path, calls)
    runner.reuse_same_day_ttl = 0
    r1 = runner.start("AAPL", "2020-01-02")
    _wait(runner, r1)
    r2 = runner.start("AAPL", "2020-01-02")
    s2 = _wait(runner, r2)
    assert s2["reused"] is True
    assert len(calls) == 1


# ----------------------- invalidação de 1º deploy: erick pré-coerência (task 005) ---
def _save_erick_record(store, run_id, with_drop):
    """Grava um registro erick DONE — com ou sem o campo ``drop_nature`` (pré-fix)."""
    result = {"verdict": "Hold", "verdict_timeframe": "1d", "erick_report": "## método"}
    if with_drop:
        result["drop_nature"] = {"classification": "liquidacao_saudavel"}
    store.save({
        "run_id": run_id, "ticker": "AAPL", "date": "2020-01-02", "asset_type": "stock",
        "status": "done", "verdict": "Hold", "verdict_timeframe": "1d", "method": "erick",
        "cost_usd": 0.0, "elapsed": 1, "finished_at": "2020-01-02T00:00:00",
        "result": result,
    })


def test_prefix_erick_record_without_drop_nature_is_invalidated(tmp_path):
    """Um registro erick gravado ANTES da coerência (sem ``drop_nature``) NÃO é
    reusado — reapareceria com o Estado antigo, contraditório. O novo (com o campo)
    volta a reusar normalmente. Padrão nunca é afetado."""
    store = HistoryStore(tmp_path)
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)}, store=store,
                            graph_factory=lambda *a, **k: None)
    _save_erick_record(store, "old", with_drop=False)
    # nenhum dos dois caminhos de reúso (single-run e confronto) devolve o pré-fix
    assert runner._find_reusable_completed("AAPL", "2020-01-02", "1d", "erick") is None
    assert runner._find_reusable("AAPL", "2020-01-02", "1d", want_erick=True) is None
    # já um registro pós-fix (com o campo) reusa
    _save_erick_record(store, "new", with_drop=True)
    rec = runner._find_reusable_completed("AAPL", "2020-01-02", "1d", "erick")
    assert rec and rec["run_id"] == "new"
    rec2 = runner._find_reusable("AAPL", "2020-01-02", "1d", want_erick=True)
    assert rec2 and rec2["run_id"] == "new"


# ------------------- o atalho 1-2-3 não se disfarça de Padrão no confronto (A1) ---
def _save_setup123_record(store, run_id="s123"):
    """Registro do atalho como ele é gravado de verdade: sem relatório, sem veredito."""
    store.save({
        "run_id": run_id, "ticker": "AAPL", "date": "2020-01-02", "asset_type": "stock",
        "status": "done", "verdict": None, "verdict_timeframe": "1d",
        "method": "setup123", "cost_usd": 0.0, "elapsed": 1,
        "finished_at": "2020-01-02T00:00:00",
        "result": {"verdict": None, "erick_report": "", "market_report": "",
                   "setup123": True, "actionable": {"setup_state": "ativo"}},
    })


def test_registro_setup123_nao_vira_o_lado_padrao_do_confronto(tmp_path):
    """O 1-2-3 grava ``erick_report`` VAZIO — e a detecção por ausência o dava como
    "padrao". O confronto reusava esse registro EM BRANCO como a coluna Padrão e o
    meta-juiz julgava nada contra um Erick de verdade. Agora ele é recusado."""
    store = HistoryStore(tmp_path)
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)}, store=store,
                            graph_factory=lambda *a, **k: None)
    _save_setup123_record(store)
    assert runner._find_reusable("AAPL", "2020-01-02", "1d", want_erick=False) is None
    assert runner._find_reusable("AAPL", "2020-01-02", "1d", want_erick=True) is None
    # e o reúso single-run também não o entrega como padrão
    assert runner._find_reusable_completed("AAPL", "2020-01-02", "1d", "padrao") is None


def test_detect_method_identifica_o_atalho_em_vez_de_chutar_padrao(tmp_path):
    """``detect_method`` inferia por AUSÊNCIA de erick_report; o atalho caía em
    "padrao" e entrava num par de confronto que ele não é."""
    from tradingagents.webui.compare import confront_pair_valid, detect_method

    rec = {"result": {"setup123": True, "erick_report": ""}}
    assert detect_method(rec) == "setup123"
    # e um par com ele deixa de ser um confronto válido (Padrão × Erick, só)
    assert confront_pair_valid({"method": "setup123", "timeframe": "1d", "date": "d"},
                               {"method": "erick", "timeframe": "1d", "date": "d"}) is False


def test_padrao_de_verdade_continua_reusavel(tmp_path):
    """Contra-prova: recusar o atalho não pode recusar um Padrão legítimo."""
    store = HistoryStore(tmp_path)
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)}, store=store,
                            graph_factory=lambda *a, **k: None)
    store.save({
        "run_id": "p1", "ticker": "AAPL", "date": "2020-01-02", "asset_type": "stock",
        "status": "done", "verdict": "Hold", "verdict_timeframe": "1d",
        "method": "padrao", "cost_usd": 0.1, "elapsed": 9,
        "finished_at": "2020-01-02T00:00:00",
        "result": {"verdict": "Hold", "erick_report": "", "market_report": "## técnico"},
    })
    rec = runner._find_reusable("AAPL", "2020-01-02", "1d", want_erick=False)
    assert rec and rec["run_id"] == "p1"


# ----------------------------------- resume descriptors + boot resume (P1/2/5) ---
def test_active_descriptor_written_while_running_and_cleared(tmp_path):
    """A run drops a resume descriptor the moment it starts and clears it on
    terminal — whatever remains after a kill is the recovery queue. Also feeds
    active_run_ids (the /api/health drain signal)."""
    gate = threading.Event()
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path),
                            graph_factory=_blocking_factory(gate))
    r = runner.start("AAPL", "2020-01-02")
    try:
        assert r in runner.active_run_ids()
        assert any(d["run_id"] == r for d in runner.active.list_pending())
    finally:
        gate.set()
    _wait(runner, r)
    assert r not in runner.active_run_ids()
    assert all(d["run_id"] != r for d in runner.active.list_pending())


def test_resume_interrupted_reenqueues_resumable(tmp_path):
    """A descriptor left behind by a killed process (server-key run) is re-run on
    boot — it completes and persists, and the descriptor is cleared."""
    calls: list = []
    runner = _runner(tmp_path, calls)
    runner.active.put("dead-01", {
        "run_id": "dead-01", "ticker": "AAPL", "date": "2020-01-02",
        "asset_type": "stock", "timeframe": "1d", "method": "padrao",
        "selected_analysts": ["market", "social", "news", "fundamentals"],
        "started_at": "2026-08-26T10:00:00-04:00", "resumable": True,
        "overrides": {"allow_server_key": True},
    })
    n = runner.resume_interrupted()
    assert n == 1
    snap = _wait(runner, "dead-01")
    assert snap["status"] == "done"
    assert snap["resuming"] is True
    assert len(calls) == 1                            # the resumed run actually ran
    assert runner.store.get("dead-01")["status"] == "done"
    assert all(d["run_id"] != "dead-01" for d in runner.active.list_pending())


def test_resume_interrupted_marks_non_resumable(tmp_path):
    """A BYOK run (user key, not persisted) can't be resumed — it surfaces as an
    honest 'interrupted' error record, never faked as complete or re-run."""
    calls: list = []
    runner = _runner(tmp_path, calls)
    runner.active.put("byok-01", {
        "run_id": "byok-01", "ticker": "AAPL", "date": "2020-01-02",
        "asset_type": "stock", "timeframe": "1d", "method": "padrao",
        "selected_analysts": ["market"], "started_at": "2026-08-26T10:00:00-04:00",
        "resumable": False, "overrides": {},
    })
    n = runner.resume_interrupted()
    assert n == 0
    assert len(calls) == 0                            # nothing re-run
    rec = runner.store.get("byok-01")
    assert rec["status"] == "error" and rec["error_code"] == "interrupted"
    assert all(d["run_id"] != "byok-01" for d in runner.active.list_pending())


def test_resume_skips_already_finished_descriptor(tmp_path):
    """A descriptor whose run already finished (crash between persist and cleanup)
    is just dropped — never re-run."""
    calls: list = []
    runner = _runner(tmp_path, calls)
    runner.store.save({
        "run_id": "fin-01", "ticker": "AAPL", "date": "2020-01-02",
        "asset_type": "stock", "status": "done", "method": "padrao",
        "verdict": "Buy", "verdict_timeframe": "1d", "result": {"verdict": "Buy"},
        "cost": {"usd": 0}, "finished_at": "2026-08-26T10:00:00-04:00",
    })
    runner.active.put("fin-01", {
        "run_id": "fin-01", "ticker": "AAPL", "date": "2020-01-02",
        "asset_type": "stock", "timeframe": "1d", "method": "padrao",
        "resumable": True, "overrides": {"allow_server_key": True},
    })
    n = runner.resume_interrupted()
    assert n == 0
    assert len(calls) == 0
    assert all(d["run_id"] != "fin-01" for d in runner.active.list_pending())

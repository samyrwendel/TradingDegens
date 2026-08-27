"""Escalonamento de etapa com outro LLM (task 027 parte B).

Numa run RESUMÍVEL (dono/servidor) que falhou/degradou, o dono ESCALA a etapa com
outro provedor+modelo: re-roda SÓ ela reaproveitando o checkpoint (022). Um fake
de grafo captura a config efetiva, então "o nível escalado chegou ao motor?" é
asserção dura. Run BYOK (não resumível) → indisponível honesto, sem re-rodar.
"""
import pytest

import tradingagents.webui.runner as runner_module
from tests.test_webui_runner import FINAL_STATE, _FakeGraph, _wait
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.store import HistoryStore


@pytest.fixture(autouse=True)
def _stub_enrich(monkeypatch):
    monkeypatch.setattr(runner_module, "fetch_price_chart",
                        lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_actionable_plan",
                        lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_derivatives_report", lambda t, d: "")


def _capturing_factory(captured):
    def make(config, selected_analysts, callbacks):
        captured.append(dict(config))
        return _FakeGraph(callbacks, FINAL_STATE, "Buy")
    return make


def _runner(tmp_path, captured):
    return AnalysisRunner(base_config={"results_dir": str(tmp_path), "llm_provider": "openai"},
                          store=HistoryStore(tmp_path),
                          graph_factory=_capturing_factory(captured))


def _put_resumable(runner, rid, overrides=None):
    runner.active.put(rid, {
        "run_id": rid, "ticker": "AAPL", "date": "2020-01-02",
        "asset_type": "stock", "timeframe": "1d", "method": "padrao",
        "selected_analysts": ["market", "social", "news", "fundamentals"],
        "started_at": "2026-08-26T10:00:00-04:00", "resumable": True,
        "overrides": overrides if overrides is not None else {"allow_server_key": True},
    })


# ------------------------------------------------------------------ deep -------
def test_escalate_deep_level_reaches_engine(tmp_path):
    captured: list = []
    runner = _runner(tmp_path, captured)
    _put_resumable(runner, "err-01")
    res = runner.escalate("err-01", "deep", provider="claude-cli", model="claude-opus-4-8")
    assert res["ok"] is True and res["level"] == "deep"
    _wait(runner, "err-01")
    assert len(captured) == 1
    cfg = captured[0]
    assert cfg["deep_think_provider"] == "claude-cli"
    assert cfg["deep_think_llm"] == "claude-opus-4-8"
    assert cfg["llm_provider"] == "claude-cli"       # base = pesado
    # o nível RÁPIDO não foi tocado (só o pesado escalou)
    assert "quick_think_provider" not in cfg


def test_escalate_quick_level_only_touches_quick(tmp_path):
    captured: list = []
    runner = _runner(tmp_path, captured)
    _put_resumable(runner, "err-02")
    res = runner.escalate("err-02", "quick", provider="openai", model="gpt-5.4-mini")
    assert res["ok"] is True and res["level"] == "quick"
    _wait(runner, "err-02")
    cfg = captured[0]
    assert cfg["quick_think_provider"] == "openai"
    assert cfg["quick_think_llm"] == "gpt-5.4-mini"


# --------------------------------------------------------------- guardas -------
def test_escalate_byok_run_is_unavailable_honest(tmp_path):
    captured: list = []
    runner = _runner(tmp_path, captured)
    # descritor BYOK (não resumível): nada re-roda, mensagem honesta.
    runner.active.put("byok-01", {
        "run_id": "byok-01", "ticker": "AAPL", "date": "2020-01-02",
        "asset_type": "stock", "timeframe": "1d", "method": "padrao",
        "selected_analysts": ["market"], "resumable": False, "overrides": {},
    })
    res = runner.escalate("byok-01", "deep", provider="claude-cli", model="x")
    assert res["ok"] is False
    assert res["code"] == "not_resumable"
    assert "retomável" in res["error"].lower()
    assert captured == []                              # nada re-rodou


def test_escalate_bad_level_rejected(tmp_path):
    runner = _runner(tmp_path, [])
    _put_resumable(runner, "err-03")
    res = runner.escalate("err-03", "juiz", provider="openai")
    assert res["ok"] is False and res["code"] == "bad_level"


def test_escalate_unknown_run_is_none(tmp_path):
    runner = _runner(tmp_path, [])
    assert runner.escalate("nope", "deep", provider="openai") is None


def test_escalate_without_target_rejected(tmp_path):
    runner = _runner(tmp_path, [])
    _put_resumable(runner, "err-04")
    res = runner.escalate("err-04", "deep")
    assert res["ok"] is False and res["code"] == "no_target"


def test_resumable_error_keeps_descriptor_for_escalation(tmp_path):
    """Uma run RESUMÍVEL que ERRA mantém o descritor pra escalar; uma BYOK não."""
    # Fábrica que erra (levanta) → status error.
    def boom_factory():
        def make(config, selected, callbacks):
            return _FakeGraph(callbacks, FINAL_STATE, "Buy",
                              raise_exc=RuntimeError("etapa falhou"))
        return make

    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "openai"},
        store=HistoryStore(tmp_path), graph_factory=boom_factory())
    # Run de dono (server-key) → resumível: erra, mas o descritor FICA.
    rid = runner.start("AAPL", "2020-01-02", overrides={"allow_server_key": True})
    snap = _wait(runner, rid)
    assert snap["status"] == "error"
    assert any(d["run_id"] == rid for d in runner.active.list_pending())

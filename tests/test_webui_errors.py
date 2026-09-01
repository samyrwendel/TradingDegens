"""Tratamento de erro de provider na UI — mensagem humana em pt-BR, sem stack.

Erro cru de LLM (429 sem crédito, 401 chave inválida, rate limit, timeout) vira
uma frase acionável no lugar do stack trace, nos 3 endpoints (analyze/compare/ask).
A chave nunca vaza; o técnico cru fica só no log do servidor.
"""

import json
import threading
import time
import urllib.request

import pytest

import tradingagents.webui.runner as runner_module
from tests.test_webui_runner import FINAL_STATE, _FakeGraph
from tradingagents.webui.errors import (
    classify_provider_error,
    humanize_provider_error,
    provider_label,
)
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.server import make_server
from tradingagents.webui.store import HistoryStore


# ------------------------------------------------------------------ helpers ----
@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setattr(runner_module, "fetch_price_chart", lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_actionable_plan", lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_derivatives_report", lambda t, d: "")


def _base(tmp_path):
    return {"results_dir": str(tmp_path), "llm_provider": "openai",
            "deep_think_llm": "gpt-5.5", "quick_think_llm": "gpt-5.4-mini", "backend_url": None}


def _boom_factory(exc):
    def make(config, selected, callbacks):
        return _FakeGraph(callbacks, FINAL_STATE, "Buy", raise_exc=exc)
    return make


def _wait(runner, run_id, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = runner.status(run_id)
        if snap and snap["status"] != "running":
            return snap
        time.sleep(0.02)
    raise AssertionError("run did not finish in time")


# The real 429 the brief quotes (OpenAI no-credits).
NO_CREDIT = (
    "OpenAIRateLimitError: Error code: 429 - {'error': {'message': "
    "'You have no credits remaining.', 'type': 'insufficient_quota', "
    "'code': 'credit_balance_exhausted'}}"
)
INVALID_KEY = "OpenAIAuthenticationError: Error code: 401 - Incorrect API key provided"
RATE_LIMIT = "RateLimitError: Error code: 429 - rate limit reached, too many requests"
TIMEOUT = "APITimeoutError: Request timed out."


# ------------------------------------------------------- classify (pura) --------
@pytest.mark.parametrize("text,code", [
    (NO_CREDIT, "no_credit"),
    (INVALID_KEY, "invalid_key"),
    ("API key for provider 'openai' is not set.", "invalid_key"),
    (RATE_LIMIT, "rate_limit"),
    (TIMEOUT, "unavailable"),
    ("something totally unrelated broke", None),
    ("", None),
])
def test_classify(text, code):
    assert classify_provider_error(text) == code


def test_no_credit_beats_rate_limit_when_both_present():
    # a mensagem de quota também casa 429/'rate limit' — quota tem que ganhar
    assert classify_provider_error(NO_CREDIT) == "no_credit"


def test_humanize_mentions_provider_and_action():
    out = humanize_provider_error(NO_CREDIT, "openai")
    assert out["code"] == "no_credit"
    assert "OpenAI" in out["message"]
    assert "crédito" in out["message"].lower()
    out2 = humanize_provider_error(INVALID_KEY, "anthropic")
    assert out2["code"] == "invalid_key"
    assert "Anthropic" in out2["message"]


def test_humanize_unknown_returns_none():
    assert humanize_provider_error("random glitch", "openai") is None


def test_provider_label():
    assert provider_label("openai") == "OpenAI"
    assert provider_label("openrouter") == "OpenRouter"
    assert provider_label("weirdprov") == "Weirdprov"
    assert provider_label("") == "o provider"


# ------------------------------------------------- runner: analyze error --------
def test_run_no_credit_shows_human_message(tmp_path):
    runner = AnalysisRunner(base_config=_base(tmp_path), store=HistoryStore(tmp_path),
                            graph_factory=_boom_factory(RuntimeError(NO_CREDIT)))
    snap = _wait(runner, runner.start("AAPL", "2026-08-22"))
    assert snap["status"] == "error"
    assert snap["error_code"] == "no_credit"
    assert "crédito" in snap["error"].lower()
    # sem stack trace na UI
    assert "Traceback" not in json.dumps(snap, default=str)
    assert (snap.get("result") or {}).get("trace") is None


def test_run_invalid_key_shows_human_message(tmp_path):
    runner = AnalysisRunner(base_config=_base(tmp_path), store=HistoryStore(tmp_path),
                            graph_factory=_boom_factory(RuntimeError(INVALID_KEY)))
    snap = _wait(runner, runner.start("AAPL", "2026-08-22"))
    assert snap["error_code"] == "invalid_key"
    assert "inválida" in snap["error"].lower() or "ausente" in snap["error"].lower()


def test_run_error_code_uses_effective_provider(tmp_path):
    """Com provider trocado por BYOK, a mensagem cita o provider do usuário."""
    runner = AnalysisRunner(base_config=_base(tmp_path), store=HistoryStore(tmp_path),
                            graph_factory=_boom_factory(RuntimeError(NO_CREDIT)))
    snap = _wait(runner, runner.start("AAPL", "2026-08-22",
                                      overrides={"provider": "anthropic", "api_key": "sk-x"}))
    assert "Anthropic" in snap["error"]


def test_unknown_error_keeps_generic_message(tmp_path):
    runner = AnalysisRunner(base_config=_base(tmp_path), store=HistoryStore(tmp_path),
                            graph_factory=_boom_factory(RuntimeError("boom")))
    snap = _wait(runner, runner.start("AAPL", "2026-08-22"))
    assert snap["status"] == "error"
    assert "boom" in snap["error"]  # fallback preserva o técnico curto


def test_error_persisted_with_code(tmp_path):
    store = HistoryStore(tmp_path)
    runner = AnalysisRunner(base_config=_base(tmp_path), store=store,
                            graph_factory=_boom_factory(RuntimeError(NO_CREDIT)))
    rid = runner.start("AAPL", "2026-08-22")
    _wait(runner, rid)
    rec = store.get(rid)
    assert rec["error_code"] == "no_credit"
    assert "Traceback" not in json.dumps(rec, default=str)


# ------------------------------------------------- runner: ask error ------------
class _RaisingLLM:
    def __init__(self, exc):
        self._exc = exc

    def invoke(self, _messages):
        raise self._exc


def _ask_record():
    return {
        "run_id": "r-ask", "ticker": "AAPL", "date": "2026-08-22",
        "asset_type": "stock", "status": "done",
        "result": {"verdict": "Buy", "price_structure": {"zones": []},
                   "market_report": "algo", "verdict_timeframe": "1d"},
    }


def test_ask_no_credit_returns_human_error(tmp_path, monkeypatch):
    runner = AnalysisRunner(base_config=_base(tmp_path), store=HistoryStore(tmp_path))
    monkeypatch.setattr(runner, "_load_record", lambda rid: _ask_record())
    monkeypatch.setattr(runner, "_answer_llm",
                        lambda cbs, ov=None: _RaisingLLM(RuntimeError(NO_CREDIT)))
    out = runner.ask("r-ask", "onde está o suporte?")
    assert out["error_code"] == "no_credit"
    assert "crédito" in out["error"].lower()
    assert "answer" not in out
    assert "Traceback" not in json.dumps(out, default=str)


def test_ask_redacts_key_in_unknown_error(tmp_path, monkeypatch):
    secret = "sk-ASKLEAK-777"
    runner = AnalysisRunner(base_config=_base(tmp_path), store=HistoryStore(tmp_path))
    monkeypatch.setattr(runner, "_load_record", lambda rid: _ask_record())
    monkeypatch.setattr(runner, "_answer_llm",
                        lambda cbs, ov=None: _RaisingLLM(RuntimeError(f"odd {secret}")))
    out = runner.ask("r-ask", "e aí?", overrides={"api_key": secret})
    assert secret not in json.dumps(out, default=str)
    assert "***" in out["error"]


# ------------------------------------------------- HTTP end-to-end --------------
def _make_server(tmp_path, factory):
    runner = AnalysisRunner(base_config=_base(tmp_path), store=HistoryStore(tmp_path),
                            graph_factory=factory)
    httpd = make_server("127.0.0.1", 0, runner=runner)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{port}"


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=5) as resp:
        return resp.status, json.loads(resp.read())


def _post(base, path, payload, headers=None):
    hdr = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode(), headers=hdr)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read())


def test_http_analyze_401_shows_clean_message(tmp_path):
    httpd, base = _make_server(tmp_path, _boom_factory(RuntimeError(INVALID_KEY)))
    try:
        _, body = _post(base, "/api/analyze", {"ticker": "AAPL", "date": "2026-08-22"},
                        headers={"X-LLM-Key": "sk-CANARY-42"})
        rid = body["run_id"]
        for _ in range(200):
            _, snap = _get(base, "/api/status/" + rid)
            if snap["status"] != "running":
                break
            time.sleep(0.02)
        assert snap["status"] == "error"
        assert snap["error_code"] == "invalid_key"
        assert "Traceback" not in json.dumps(snap, default=str)
        assert "sk-CANARY-42" not in json.dumps(snap, default=str)
    finally:
        httpd.shutdown()

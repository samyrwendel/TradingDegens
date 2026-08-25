"""BYOK — cada usuário traz a própria chave de API (multi-provider).

Cobre a SEGURANÇA do fluxo da chave (requisito duro do brief):
  - a chave do usuário chega por header/corpo e vira a config efetiva DAQUELA run
    (prioridade sobre a env do servidor);
  - sem chave do usuário, cai na env do servidor (fallback);
  - a chave NUNCA é persistida (run record, index.jsonl, snapshot, history) nem
    ecoada num erro (redigida);
  - o provider/modelo do usuário substituem os do servidor por-run;
  - /api/test-key valida a chave sem rodar análise.

Usa o mesmo motor falso (``_factory``) dos outros testes de webui — nenhuma
chamada de LLM real. Onde uma run precisa capturar a config recebida, o factory
grava-a numa lista.
"""

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

import tradingagents.webui.runner as runner_module
from tests.test_webui_runner import FINAL_STATE, _factory, _FakeGraph
from tradingagents.webui.runner import (
    AnalysisRunner,
    apply_llm_overrides,
)
from tradingagents.webui.server import make_server
from tradingagents.webui.store import HistoryStore


# ------------------------------------------------------------------ helpers ----
@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """Sem rede: o worker sempre enriquece com chart/plan/derivativos."""
    monkeypatch.setattr(runner_module, "fetch_price_chart", lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_actionable_plan", lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_derivatives_report", lambda t, d: "")


def _capturing_factory(captured):
    """Factory que grava a config efetiva recebida por cada run (pra inspeção)."""
    def make(config, selected, callbacks):
        captured.append(dict(config))
        return _FakeGraph(callbacks, FINAL_STATE, "Buy")
    return make


def _base_config(tmp_path):
    return {
        "results_dir": str(tmp_path),
        "llm_provider": "openai",
        "deep_think_llm": "gpt-5.5",
        "quick_think_llm": "gpt-5.4-mini",
        "backend_url": None,
    }


def _wait(runner, run_id, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = runner.status(run_id)
        if snap and snap["status"] != "running":
            return snap
        time.sleep(0.02)
    raise AssertionError("run did not finish in time")


# ------------------------------------------------ apply_llm_overrides (pura) ----
def test_overrides_none_returns_server_config():
    base = {"llm_provider": "openai", "deep_think_llm": "gpt-5.5",
            "quick_think_llm": "gpt-5.4-mini", "backend_url": None}
    cfg = apply_llm_overrides(base, None)
    assert cfg["llm_provider"] == "openai"
    assert "llm_api_key" not in cfg  # sem chave do usuário -> fallback env


def test_overrides_user_key_takes_priority():
    base = {"llm_provider": "openai", "deep_think_llm": "gpt-5.5",
            "quick_think_llm": "gpt-5.4-mini", "backend_url": None}
    cfg = apply_llm_overrides(base, {"api_key": "sk-USER"})
    assert cfg["llm_api_key"] == "sk-USER"
    # não muta a base do servidor
    assert "llm_api_key" not in base


def test_provider_switch_pulls_catalog_default_models():
    base = {"llm_provider": "openai", "deep_think_llm": "gpt-5.5",
            "quick_think_llm": "gpt-5.4-mini", "backend_url": None}
    cfg = apply_llm_overrides(base, {"provider": "anthropic", "api_key": "sk-ant"})
    assert cfg["llm_provider"] == "anthropic"
    # não herda o modelo OpenAI numa chave Anthropic
    assert cfg["deep_think_llm"].startswith("claude")
    assert cfg["quick_think_llm"].startswith("claude")


def test_explicit_model_and_base_url_win():
    base = {"llm_provider": "openai", "deep_think_llm": "gpt-5.5",
            "quick_think_llm": "gpt-5.4-mini", "backend_url": None}
    cfg = apply_llm_overrides(base, {
        "provider": "ollama", "base_url": "http://localhost:11434/v1",
        "deep_model": "llama3.1:70b", "quick_model": "llama3.1:8b",
    })
    assert cfg["llm_provider"] == "ollama"
    assert cfg["backend_url"] == "http://localhost:11434/v1"
    assert cfg["deep_think_llm"] == "llama3.1:70b"
    assert cfg["quick_think_llm"] == "llama3.1:8b"


# ------------------------------------------------ runner injeta por-run ---------
def test_run_uses_user_key_in_effective_config(tmp_path):
    captured = []
    runner = AnalysisRunner(base_config=_base_config(tmp_path),
                            store=HistoryStore(tmp_path),
                            graph_factory=_capturing_factory(captured))
    run_id = runner.start("AAPL", "2026-08-22", overrides={"api_key": "sk-USERKEY"})
    _wait(runner, run_id)
    assert captured, "factory nunca recebeu a config"
    assert captured[0]["llm_api_key"] == "sk-USERKEY"


def test_run_switches_provider_and_model_per_run(tmp_path):
    captured = []
    runner = AnalysisRunner(base_config=_base_config(tmp_path),
                            store=HistoryStore(tmp_path),
                            graph_factory=_capturing_factory(captured))
    run_id = runner.start("AAPL", "2026-08-22",
                          overrides={"provider": "anthropic", "api_key": "sk-ant"})
    _wait(runner, run_id)
    assert captured[0]["llm_provider"] == "anthropic"
    assert captured[0]["deep_think_llm"].startswith("claude")


def test_run_without_key_falls_back_to_env(tmp_path):
    captured = []
    runner = AnalysisRunner(base_config=_base_config(tmp_path),
                            store=HistoryStore(tmp_path),
                            graph_factory=_capturing_factory(captured))
    run_id = runner.start("AAPL", "2026-08-22")  # sem overrides
    _wait(runner, run_id)
    # nenhuma chave injetada -> o grafo cai na env do servidor
    assert "llm_api_key" not in captured[0]


def test_two_runs_dont_mix_keys(tmp_path):
    captured = []
    runner = AnalysisRunner(base_config=_base_config(tmp_path),
                            store=HistoryStore(tmp_path),
                            graph_factory=_capturing_factory(captured))
    a = runner.start("AAPL", "2026-08-22", overrides={"api_key": "sk-AAA"})
    b = runner.start("MSFT", "2026-08-22", overrides={"api_key": "sk-BBB"})
    _wait(runner, a)
    _wait(runner, b)
    keys = {c.get("llm_api_key") for c in captured}
    assert keys == {"sk-AAA", "sk-BBB"}  # cada run manteve a sua


# ------------------------------------------------ SEGURANÇA: nunca persiste -----
def test_user_key_never_persisted_to_disk(tmp_path):
    """A chave não pode aparecer em NENHUM arquivo: run record, index.jsonl."""
    store = HistoryStore(tmp_path)
    runner = AnalysisRunner(base_config=_base_config(tmp_path),
                            store=store, graph_factory=_factory())
    secret = "sk-SUPERSECRET-DO-NOT-LEAK"
    run_id = runner.start("AAPL", "2026-08-22", overrides={"api_key": secret})
    _wait(runner, run_id)
    # grep no diretório inteiro do histórico
    hits = []
    for path in tmp_path.rglob("*"):
        if path.is_file():
            try:
                if secret in path.read_text(encoding="utf-8", errors="ignore"):
                    hits.append(str(path))
            except OSError:
                pass
    assert hits == [], f"chave vazou em: {hits}"
    # e nem no record carregado nem no snapshot
    assert secret not in json.dumps(store.get(run_id), default=str)
    assert secret not in json.dumps(runner.status(run_id), default=str)
    assert secret not in json.dumps(runner.history(), default=str)


def test_user_key_redacted_from_error(tmp_path):
    """Se o motor estourar com a chave num erro NÃO reconhecido, ela sai redigida
    (o fallback genérico ``Tipo: msg`` passa pelo _redact_secret)."""
    secret = "sk-LEAKY-KEY-123"

    def boom_factory():
        def make(config, selected, callbacks):
            # erro sem padrão conhecido, mas que ecoa a chave -> tem que ser redigido
            return _FakeGraph(callbacks, FINAL_STATE, "Buy",
                              raise_exc=RuntimeError(f"weird failure with {secret}"))
        return make

    runner = AnalysisRunner(base_config=_base_config(tmp_path),
                            store=HistoryStore(tmp_path),
                            graph_factory=boom_factory())
    run_id = runner.start("AAPL", "2026-08-22", overrides={"api_key": secret})
    snap = _wait(runner, run_id)
    assert snap["status"] == "error"
    assert secret not in json.dumps(snap, default=str)
    assert "***" in snap["error"]


def test_pop_llm_api_key_keeps_key_out_of_global_config():
    """O grafo tira a chave do config ANTES do set_config global (sem estado
    compartilhado entre runs)."""
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    cfg = {"llm_provider": "openai", "llm_api_key": "sk-X"}
    key = TradingAgentsGraph._pop_llm_api_key(cfg)
    assert key == "sk-X"
    assert "llm_api_key" not in cfg  # removida do dict que iria pro set_config
    # dict sem chave / valor vazio -> None, nada a propagar
    assert TradingAgentsGraph._pop_llm_api_key({"llm_api_key": ""}) is None
    assert TradingAgentsGraph._pop_llm_api_key({}) is None


def test_get_provider_kwargs_forwards_user_key():
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    g = object.__new__(TradingAgentsGraph)
    g.config = {"llm_provider": "openai"}
    g._llm_api_key = "sk-USER"
    kwargs = g._get_provider_kwargs()
    assert kwargs["api_key"] == "sk-USER"


# ------------------------------------------------ test_key (validação barata) ---
class _FakeLLM:
    def __init__(self, raise_exc=None):
        self._raise = raise_exc

    def invoke(self, _msg):
        if self._raise:
            raise self._raise
        class _R:
            content = "pong"
        return _R()


class _FakeClient:
    def __init__(self, llm):
        self._llm = llm

    def get_llm(self):
        return self._llm


def test_test_key_ok(tmp_path, monkeypatch):
    runner = AnalysisRunner(base_config=_base_config(tmp_path),
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    seen = {}

    def fake_create(provider, model, base_url=None, **kwargs):
        seen.update(provider=provider, model=model, kwargs=kwargs)
        return _FakeClient(_FakeLLM())

    monkeypatch.setattr("tradingagents.llm_clients.create_llm_client", fake_create)
    out = runner.test_key({"provider": "anthropic", "api_key": "sk-ant"})
    assert out["ok"] is True
    assert out["provider"] == "anthropic"
    assert out["using_user_key"] is True
    assert seen["kwargs"]["api_key"] == "sk-ant"


def test_test_key_error_is_redacted(tmp_path, monkeypatch):
    runner = AnalysisRunner(base_config=_base_config(tmp_path),
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    secret = "sk-BADKEY-XYZ"

    def fake_create(provider, model, base_url=None, **kwargs):
        # erro NÃO reconhecido que ecoa a chave -> exercita o fallback redigido
        return _FakeClient(_FakeLLM(raise_exc=RuntimeError(f"weird glitch {secret}")))

    monkeypatch.setattr("tradingagents.llm_clients.create_llm_client", fake_create)
    out = runner.test_key({"provider": "openai", "api_key": secret})
    assert out["ok"] is False
    assert secret not in json.dumps(out, default=str)
    assert "***" in out["error"]


# ---------------------------------------- test_model (pinga rápido + pesado) ----
def test_test_model_pings_quick_and_deep(tmp_path, monkeypatch):
    """Pinga os DOIS modelos (rápido/pesado) com a chave do usuário e devolve a
    latência real de cada — sem rodar análise nem tocar estado global."""
    runner = AnalysisRunner(base_config=_base_config(tmp_path),
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    seen = []

    def fake_create(provider, model, base_url=None, **kwargs):
        seen.append((model, kwargs))
        return _FakeClient(_FakeLLM())

    monkeypatch.setattr("tradingagents.llm_clients.create_llm_client", fake_create)
    out = runner.test_model({"provider": "openai", "api_key": "sk-x",
                             "quick_model": "gpt-quick", "deep_model": "gpt-deep"})
    assert out["ok"] is True
    assert out["using_user_key"] is True
    roles = {m["role"]: m for m in out["models"]}
    assert set(roles) == {"quick", "deep"}
    assert roles["quick"]["model"] == "gpt-quick"
    assert roles["deep"]["model"] == "gpt-deep"
    for m in out["models"]:
        assert m["ok"] is True
        assert isinstance(m["latency_ms"], int) and m["latency_ms"] >= 0
        assert m["sample"] == "pong"   # _FakeLLM devolve content "pong"
    # os DOIS foram pingados, cada um com a chave do usuário (kwarg, nunca global)
    assert [s[0] for s in seen] == ["gpt-quick", "gpt-deep"]
    assert all(s[1].get("api_key") == "sk-x" for s in seen)


def test_test_model_error_is_human_and_redacted(tmp_path, monkeypatch):
    """Modelo ruim → mensagem humana, sem stack e SEM a chave (redigida). O rápido
    ainda responde ✅, o pesado falha ❌ — cada item com seu próprio veredito."""
    runner = AnalysisRunner(base_config=_base_config(tmp_path),
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    secret = "sk-SECRET-123"

    def fake_create(provider, model, base_url=None, **kwargs):
        if model == "bad-deep":
            # erro que ecoa a chave e NÃO casa o mapa (404) → fallback redigido
            return _FakeClient(_FakeLLM(raise_exc=RuntimeError(f"404 model not found {secret}")))
        return _FakeClient(_FakeLLM())

    monkeypatch.setattr("tradingagents.llm_clients.create_llm_client", fake_create)
    out = runner.test_model({"provider": "openrouter", "api_key": secret,
                             "quick_model": "good-quick", "deep_model": "bad-deep"})
    assert out["ok"] is False
    roles = {m["role"]: m for m in out["models"]}
    assert roles["quick"]["ok"] is True
    assert roles["deep"]["ok"] is False
    # a chave não vaza em NENHUM lugar do retorno; o erro cru foi redigido
    assert secret not in json.dumps(out, default=str)
    assert "***" in roles["deep"]["error"]


def test_test_model_invalid_key_is_humanized(tmp_path, monkeypatch):
    """401 vira a frase acionável (mapa da 041), com error_code estável."""
    runner = AnalysisRunner(base_config=_base_config(tmp_path),
                            store=HistoryStore(tmp_path), graph_factory=_factory())

    def fake_create(provider, model, base_url=None, **kwargs):
        return _FakeClient(_FakeLLM(raise_exc=RuntimeError("401 invalid api key")))

    monkeypatch.setattr("tradingagents.llm_clients.create_llm_client", fake_create)
    out = runner.test_model({"provider": "openai", "api_key": "sk-bad",
                             "quick_model": "q", "deep_model": "d"})
    assert out["ok"] is False
    quick = next(m for m in out["models"] if m["role"] == "quick")
    assert quick["error_code"] == "invalid_key"
    assert "Configurações" in quick["error"]


def test_test_model_refused_without_key_and_owner(tmp_path):
    """Público explícito sem chave própria não pinga nada (need_key, models vazio) —
    a chave do servidor é só do dono, jamais gasta num teste público."""
    runner = AnalysisRunner(base_config=_base_config(tmp_path),
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    out = runner.test_model({"allow_server_key": False})
    assert out["ok"] is False
    assert out["error_code"] == "need_key"
    assert out["models"] == []


def test_test_model_no_model_named(tmp_path, monkeypatch):
    """Provider custom-only sem modelo nomeado → item de erro claro, não estoura."""
    base = dict(_base_config(tmp_path))
    base["quick_think_llm"] = None
    base["deep_think_llm"] = None
    runner = AnalysisRunner(base_config=base, store=HistoryStore(tmp_path),
                            graph_factory=_factory())

    def fake_create(provider, model, base_url=None, **kwargs):  # não deve ser chamado
        raise AssertionError("não devia criar client sem modelo")

    monkeypatch.setattr("tradingagents.llm_clients.create_llm_client", fake_create)
    out = runner.test_model({"api_key": "sk-x"})
    assert out["ok"] is False
    assert all(m["ok"] is False and m["error_code"] == "no_model" for m in out["models"])


# ------------------------------------------------ config_info advertises BYOK ---
def test_config_info_exposes_providers_without_leaking_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-server-secret")
    runner = AnalysisRunner(base_config=_base_config(tmp_path),
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    info = runner.config_info()
    assert "llm" in info
    llm = info["llm"]
    assert llm["default_provider"] == "openai"
    ids = {p["id"] for p in llm["providers"]}
    assert {"openai", "anthropic", "openrouter", "ollama"}.issubset(ids)
    # presença de env é um booleano, jamais o valor
    assert "sk-server-secret" not in json.dumps(info, default=str)
    openai_p = next(p for p in llm["providers"] if p["id"] == "openai")
    assert openai_p["server_key"] is True


# ------------------------------------------------ HTTP: header X-LLM-Key --------
def _make_server(tmp_path, factory, base_config=None):
    runner = AnalysisRunner(base_config=base_config or {"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=factory)
    httpd = make_server("127.0.0.1", 0, runner=runner)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{port}"


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=5) as resp:
        return resp.status, json.loads(resp.read())


def test_http_analyze_reads_key_from_header(tmp_path):
    captured = []
    httpd, base = _make_server(tmp_path, _capturing_factory(captured),
                               base_config=_base_config(tmp_path))
    try:
        req = urllib.request.Request(
            base + "/api/analyze",
            data=json.dumps({"ticker": "AAPL", "date": "2026-08-22",
                             "llm_provider": "anthropic"}).encode(),
            headers={"Content-Type": "application/json", "X-LLM-Key": "sk-HEADERKEY"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            run_id = json.loads(resp.read())["run_id"]
        for _ in range(200):
            _, snap = _get(base, "/api/status/" + run_id)
            if snap["status"] != "running":
                break
            time.sleep(0.02)
        assert snap["status"] == "done"
        assert captured[0]["llm_api_key"] == "sk-HEADERKEY"
        assert captured[0]["llm_provider"] == "anthropic"
        # a chave não aparece no snapshot devolvido
        assert "sk-HEADERKEY" not in json.dumps(snap, default=str)
    finally:
        httpd.shutdown()


def test_http_analyze_without_header_is_refused_for_public(tmp_path):
    """Sem chave própria e sem login do dono, a run é RECUSADA (403) — nunca cai na
    env do servidor (o fallback automático foi removido na task 042). O acesso à
    chave do servidor é coberto em test_webui_auth.py (dono logado)."""
    captured = []
    httpd, base = _make_server(tmp_path, _capturing_factory(captured),
                               base_config=_base_config(tmp_path))
    try:
        req = urllib.request.Request(
            base + "/api/analyze",
            data=json.dumps({"ticker": "AAPL", "date": "2026-08-22"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("esperava 403")
        except urllib.error.HTTPError as e:
            assert e.code == 403
            assert json.loads(e.read())["error_code"] == "need_key"
        assert captured == []  # nenhuma run criada; nunca tocou a env
    finally:
        httpd.shutdown()


def test_http_test_key_endpoint(tmp_path, monkeypatch):
    httpd, base = _make_server(tmp_path, _factory(), base_config=_base_config(tmp_path))

    def fake_create(provider, model, base_url=None, **kwargs):
        return _FakeClient(_FakeLLM())

    monkeypatch.setattr("tradingagents.llm_clients.create_llm_client", fake_create)
    try:
        req = urllib.request.Request(
            base + "/api/test-key",
            data=json.dumps({"llm_provider": "openai"}).encode(),
            headers={"Content-Type": "application/json", "X-LLM-Key": "sk-abc"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            out = json.loads(resp.read())
        assert out["ok"] is True and out["using_user_key"] is True
    finally:
        httpd.shutdown()


def test_http_test_model_endpoint(tmp_path, monkeypatch):
    """/api/test-model: chave só no header, pinga os dois modelos, chave nunca no
    corpo da resposta."""
    httpd, base = _make_server(tmp_path, _factory(), base_config=_base_config(tmp_path))

    def fake_create(provider, model, base_url=None, **kwargs):
        return _FakeClient(_FakeLLM())

    monkeypatch.setattr("tradingagents.llm_clients.create_llm_client", fake_create)
    try:
        req = urllib.request.Request(
            base + "/api/test-model",
            data=json.dumps({"llm_provider": "openai",
                             "quick_think_llm": "q", "deep_think_llm": "d"}).encode(),
            headers={"Content-Type": "application/json", "X-LLM-Key": "sk-abc"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            out = json.loads(resp.read())
        assert out["ok"] is True and out["using_user_key"] is True
        assert len(out["models"]) == 2
        assert {m["role"] for m in out["models"]} == {"quick", "deep"}
        assert "sk-abc" not in json.dumps(out, default=str)
    finally:
        httpd.shutdown()

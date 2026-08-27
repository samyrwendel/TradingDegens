"""Fallback transparente AUTOMÁTICO entre provedores LLM (task 027-fallback).

Exercita DE VERDADE (não infere) os quatro pontos do aceite:
  1. topo falha por estado do provedor (429/402/…) → NÃO para: cai pro próximo da
     cadeia, CONCLUI, e a troca fica registrada (marcador de desvio);
  2. ordem padrão começa claude-cli ($0), openai como fallback; o provedor do seletor
     avançado vira o topo da cadeia;
  3. cadeia inteira falha → erro honesto (sem loop); erro NÃO-de-provedor (bug) não
     dispara fallback;
  4. plumbing owner-gated / BYOK (cadeia limitada, sem vazar provedor).
"""
import pytest

from tradingagents.graph.trading_graph import (
    _DEFAULT_FALLBACK_ORDER,
    resolve_fallback_chain,
)
from tradingagents.llm_clients.fallback import (
    FallbackRunnable,
    FallbackTracker,
    provider_error_code,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- fakes
class _Msg:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    """Membro de cadeia: ``.invoke`` devolve ou levanta; ``bind_tools`` /
    ``with_structured_output`` marcam o objeto pra provar o fan-out do binding."""

    def __init__(self, *, returns=None, raises=None, tag="x"):
        self.returns = returns
        self.raises = raises
        self.tag = tag
        self.calls = 0
        self.bound = False
        self.structured = False

    def invoke(self, input, config=None, **kwargs):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return _Msg(self.returns)

    def bind_tools(self, tools, **kwargs):
        self.bound = True
        return self

    def with_structured_output(self, schema, **kwargs):
        self.structured = True
        return self


def _member(provider, model, llm):
    return {"provider": provider, "model": model, "llm": llm}


# ------------------------------------------------- classificação do erro de provedor
def test_provider_error_code_recognises_state_errors():
    assert provider_error_code(RuntimeError("Error code: 429 rate limit")) == "rate_limit"
    assert provider_error_code(RuntimeError("no credits remaining")) == "no_credit"
    assert provider_error_code(RuntimeError("401 unauthorized invalid api key")) == "invalid_key"
    assert provider_error_code(RuntimeError("503 service unavailable")) == "unavailable"


def test_provider_error_code_none_for_bug():
    # Um bug de código (KeyError/ValueError comum) NÃO é estado de provedor.
    assert provider_error_code(KeyError("market_report")) is None
    assert provider_error_code(ValueError("índice fora do intervalo")) is None


# ------------------------------------------------------------- FallbackRunnable core
def test_falls_over_on_provider_error_and_records():
    tracker = FallbackTracker()
    prim = _FakeLLM(raises=RuntimeError("Error code: 429 Too Many Requests"))
    sec = _FakeLLM(returns="ok-secundário")
    fr = FallbackRunnable(
        [_member("claude-cli", "claude-sonnet-5", prim),
         _member("openai", "gpt-5.4-mini", sec)],
        tracker=tracker, level="quick",
    )
    out = fr.invoke("prompt", config={"metadata": {"langgraph_node": "Market Analyst"}})
    assert out.content == "ok-secundário"          # NÃO parou: concluiu no fallback
    assert prim.calls == 1 and sec.calls == 1
    hops = tracker.snapshot()
    assert len(hops) == 1
    h = hops[0]
    assert h["from_provider"] == "claude-cli" and h["to_provider"] == "openai"
    assert h["code"] == "rate_limit" and h["node"] == "Market Analyst"
    assert h["reason"]                              # motivo humano preenchido


def test_bug_error_propagates_without_fallback():
    tracker = FallbackTracker()
    prim = _FakeLLM(raises=KeyError("boom"))        # bug, não é estado de provedor
    sec = _FakeLLM(returns="não-deveria-rodar")
    fr = FallbackRunnable(
        [_member("claude-cli", "m1", prim), _member("openai", "m2", sec)],
        tracker=tracker,
    )
    with pytest.raises(KeyError):
        fr.invoke("prompt")
    assert sec.calls == 0                           # fallback NÃO disparou
    assert tracker.snapshot() == []                 # nada registrado


def test_whole_chain_fails_raises_last_honest_error_bounded():
    tracker = FallbackTracker()
    prim = _FakeLLM(raises=RuntimeError("429 rate limit"))
    sec = _FakeLLM(raises=RuntimeError("402 no credits remaining"))
    fr = FallbackRunnable(
        [_member("claude-cli", "m1", prim), _member("openai", "m2", sec)],
        tracker=tracker,
    )
    with pytest.raises(RuntimeError, match="no credits"):   # erro honesto do último
        fr.invoke("prompt")
    # Bounded: cada membro tentado UMA vez, sem loop.
    assert prim.calls == 1 and sec.calls == 1
    # A troca até o último foi registrada (transparência), mas não há salto extra.
    assert len(tracker.snapshot()) == 1


def test_cancellation_propagates_without_fallback():
    from tradingagents.webui.progress import RunCancelled

    tracker = FallbackTracker()
    prim = _FakeLLM(raises=RunCancelled())
    sec = _FakeLLM(returns="não-deveria-rodar")
    fr = FallbackRunnable(
        [_member("claude-cli", "m1", prim), _member("openai", "m2", sec)],
        tracker=tracker,
    )
    with pytest.raises(RunCancelled):
        fr.invoke("prompt")
    assert sec.calls == 0 and tracker.snapshot() == []


def test_bind_tools_fans_out_and_still_falls_over():
    tracker = FallbackTracker()
    prim = _FakeLLM(raises=RuntimeError("429 rate limit"))
    sec = _FakeLLM(returns="ok")
    fr = FallbackRunnable(
        [_member("claude-cli", "m1", prim), _member("openai", "m2", sec)],
        tracker=tracker,
    ).bind_tools(["tool"])
    assert prim.bound and sec.bound                 # binding aplicado a TODA a cadeia
    out = fr.invoke("prompt", config={"metadata": {"langgraph_node": "Trader"}})
    assert out.content == "ok"
    assert len(tracker.snapshot()) == 1


def test_with_structured_output_fans_out():
    prim = _FakeLLM(returns="ok")
    sec = _FakeLLM(returns="ok2")
    fr = FallbackRunnable(
        [_member("claude-cli", "m1", prim), _member("openai", "m2", sec)],
    ).with_structured_output(object)
    assert prim.structured and sec.structured
    # topo saudável → nem toca no fallback
    assert fr.invoke("p").content == "ok" and sec.calls == 0


def test_single_member_chain_never_falls_over():
    tracker = FallbackTracker()
    only = _FakeLLM(returns="só-eu")
    fr = FallbackRunnable([_member("openai", "m", only)], tracker=tracker)
    assert fr.invoke("p").content == "só-eu"
    assert tracker.snapshot() == []


# ------------------------------------------------------- resolve_fallback_chain (2)
_BASE_CLAUDE = {"llm_provider": "claude-cli", "deep_think_llm": "claude-sonnet-5",
                "quick_think_llm": "claude-haiku-4-5", "backend_url": None}


def test_default_order_starts_with_claude():
    assert _DEFAULT_FALLBACK_ORDER[0] == "claude-cli"


def test_owner_default_chain_is_claude_then_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-server")
    ch = resolve_fallback_chain(_BASE_CLAUDE, None, allow_server_key=True)
    assert [c["provider"] for c in ch["deep"]] == ["claude-cli", "openai"]
    assert [c["provider"] for c in ch["quick"]] == ["claude-cli", "openai"]
    # o link de fallback openai puxa o modelo padrão do catálogo daquele nível.
    assert ch["deep"][1]["model"] and ch["quick"][1]["model"]
    assert ch["deep"][1]["api_key"] is None          # server-key, não BYOK


def test_advanced_selector_provider_becomes_top(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-server")
    cfg = {"llm_provider": "anthropic", "deep_think_provider": "anthropic",
           "quick_think_provider": "openai", "deep_think_llm": "claude-fable-5",
           "quick_think_llm": "gpt-5.4-mini", "backend_url": None}
    ch = resolve_fallback_chain(cfg, None, allow_server_key=True)
    assert ch["deep"][0]["provider"] == "anthropic"          # seletor vira o topo
    assert [c["provider"] for c in ch["deep"]] == ["anthropic", "claude-cli", "openai"]


def test_non_owner_chain_is_single_link():
    ch = resolve_fallback_chain(_BASE_CLAUDE, None, allow_server_key=False)
    assert [c["provider"] for c in ch["deep"]] == ["claude-cli"]   # sem fallback público


def test_byok_chain_stays_on_user_provider(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-server")
    cfg = {"llm_provider": "openai", "deep_think_llm": "gpt-5.5",
           "quick_think_llm": "gpt-5.4-mini", "backend_url": None}
    ch = resolve_fallback_chain(cfg, "sk-user-byok", allow_server_key=True)
    # a chave do usuário dirige o nível → cadeia limitada ao provedor dele, sem vazar.
    assert [c["provider"] for c in ch["deep"]] == ["openai"]
    assert ch["deep"][0]["api_key"] == "sk-user-byok"


def test_max_hops_caps_chain(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-server")
    cfg = dict(_BASE_CLAUDE, fallback_max_hops=0)
    ch = resolve_fallback_chain(cfg, None, allow_server_key=True)
    assert [c["provider"] for c in ch["deep"]] == ["claude-cli"]   # 0 saltos → só topo


def test_openai_link_dropped_without_server_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    ch = resolve_fallback_chain(_BASE_CLAUDE, None, allow_server_key=True)
    # sem chave de servidor do openai, o link de fallback certeiro em 401 some.
    assert [c["provider"] for c in ch["deep"]] == ["claude-cli"]

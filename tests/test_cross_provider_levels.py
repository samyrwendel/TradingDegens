"""Cross-provider RÁPIDO/PESADO — cada nível (quick/deep) pode rodar um provedor+
modelo diferente (task 027 parte A, engine).

Sem rede: exercita a resolução pura de níveis (``resolve_level_specs``) e os kwargs
por-nível (``_get_provider_kwargs``). O modo simples (só ``llm_provider``) tem que
ficar byte-a-byte igual; o cross-provider mistura e NUNCA vaza a chave BYOK de um
provedor no client do outro.
"""
import pytest

from tradingagents.graph.trading_graph import (
    TradingAgentsGraph,
    resolve_level_specs,
)


def _bare(config):
    g = TradingAgentsGraph.__new__(TradingAgentsGraph)
    g.config = config
    return g


# --------------------------------------------------------- resolve_level_specs --
@pytest.mark.unit
def test_simple_mode_both_levels_share_provider_and_byok():
    cfg = {"llm_provider": "openai", "deep_think_llm": "gpt-5.5",
           "quick_think_llm": "gpt-5.4-mini", "backend_url": "http://x"}
    specs = resolve_level_specs(cfg, byok_key="sk-user")
    assert specs["deep"] == {"provider": "openai", "model": "gpt-5.5",
                             "base_url": "http://x", "api_key": "sk-user"}
    assert specs["quick"] == {"provider": "openai", "model": "gpt-5.4-mini",
                              "base_url": "http://x", "api_key": "sk-user"}


@pytest.mark.unit
def test_cross_provider_each_level_its_own_provider():
    cfg = {
        "llm_provider": "claude-cli",  # base = pesado
        "deep_think_provider": "claude-cli", "deep_think_llm": "claude-opus-4-8",
        "quick_think_provider": "openai", "quick_think_llm": "gpt-5.4-mini",
    }
    specs = resolve_level_specs(cfg, byok_key=None)
    assert specs["deep"]["provider"] == "claude-cli"
    assert specs["deep"]["model"] == "claude-opus-4-8"
    assert specs["quick"]["provider"] == "openai"
    assert specs["quick"]["model"] == "gpt-5.4-mini"


@pytest.mark.unit
def test_byok_never_leaks_across_providers():
    # A chave BYOK é do provedor-base (anthropic). O nível quick usa openai →
    # NÃO recebe a chave anthropic (senão o client openai autentica errado).
    cfg = {
        "llm_provider": "anthropic",
        "deep_think_provider": "anthropic", "deep_think_llm": "claude-opus-4-8",
        "quick_think_provider": "openai", "quick_think_llm": "gpt-5.4-mini",
    }
    specs = resolve_level_specs(cfg, byok_key="sk-ant")
    assert specs["deep"]["api_key"] == "sk-ant"   # mesmo provedor da chave
    assert specs["quick"]["api_key"] is None       # provedor diferente → sem chave


@pytest.mark.unit
def test_per_level_base_url_falls_back_to_backend_url():
    cfg = {"llm_provider": "openai_compatible", "deep_think_llm": "m1",
           "quick_think_llm": "m2", "backend_url": "http://base",
           "quick_backend_url": "http://quick-only"}
    specs = resolve_level_specs(cfg)
    assert specs["deep"]["base_url"] == "http://base"       # herda o backend_url
    assert specs["quick"]["base_url"] == "http://quick-only"  # override por-nível


# ------------------------------------------------------- _get_provider_kwargs ---
@pytest.mark.unit
def test_provider_kwargs_effort_matches_the_level_provider():
    # Config carrega knobs dos dois provedores; cada nível pega só o seu.
    g = _bare({"llm_provider": "claude-cli", "openai_reasoning_effort": "high",
               "anthropic_effort": "medium"})
    quick = g._get_provider_kwargs("openai", None)
    deep = g._get_provider_kwargs("claude-cli", None)
    assert quick.get("reasoning_effort") == "high"
    assert "effort" not in quick
    assert deep.get("effort") == "medium"       # claude-cli reusa o knob anthropic
    assert "reasoning_effort" not in deep


@pytest.mark.unit
def test_provider_kwargs_forwards_level_key_only():
    g = _bare({"llm_provider": "anthropic"})
    assert g._get_provider_kwargs("openai", "sk-x").get("api_key") == "sk-x"
    assert "api_key" not in g._get_provider_kwargs("openai", None)


@pytest.mark.unit
def test_default_provider_is_base_backcompat():
    # Chamada sem provider (código legado) usa o llm_provider — comportamento antigo.
    g = _bare({"llm_provider": "openai", "openai_reasoning_effort": "low"})
    assert g._get_provider_kwargs().get("reasoning_effort") == "low"

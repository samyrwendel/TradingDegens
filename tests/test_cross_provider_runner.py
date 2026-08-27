"""Runner: modo avançado (provedor+modelo por-nível) + validação de credencial
por-nível ANTES de rodar (task 027 parte A, plumbing).
"""
import pytest

from tradingagents.webui.runner import (
    apply_llm_overrides,
    levels_credential_error,
)

_BASE = {"llm_provider": "openai", "deep_think_llm": "gpt-5.5",
         "quick_think_llm": "gpt-5.4-mini", "backend_url": ""}


# ---------------------------------------------------- apply_llm_overrides -------
@pytest.mark.unit
def test_advanced_sets_per_level_providers_and_models():
    ov = {"advanced": True, "deep_provider": "claude-cli", "quick_provider": "openai",
          "deep_model": "claude-opus-4-8", "quick_model": "gpt-5.4-mini",
          "allow_server_key": True}
    cfg = apply_llm_overrides(_BASE, ov)
    assert cfg["deep_think_provider"] == "claude-cli"
    assert cfg["quick_think_provider"] == "openai"
    assert cfg["llm_provider"] == "claude-cli"  # base = pesado
    assert cfg["deep_think_llm"] == "claude-opus-4-8"
    assert cfg["quick_think_llm"] == "gpt-5.4-mini"


@pytest.mark.unit
def test_advanced_without_model_pulls_catalog_default():
    ov = {"advanced": True, "deep_provider": "claude-cli", "quick_provider": "openai",
          "allow_server_key": True}
    cfg = apply_llm_overrides(_BASE, ov)
    # Sem modelo explícito, cada nível puxa o padrão do catálogo do seu provedor.
    assert cfg["deep_think_llm"] and cfg["deep_think_llm"] != "gpt-5.5"
    assert cfg["quick_think_llm"]


@pytest.mark.unit
def test_simple_mode_untouched_without_advanced_flag():
    ov = {"provider": "openai", "allow_server_key": True}
    cfg = apply_llm_overrides(_BASE, ov)
    assert "deep_think_provider" not in cfg and "quick_think_provider" not in cfg
    assert cfg["llm_provider"] == "openai"


# ------------------------------------------------ levels_credential_error -------
@pytest.mark.unit
def test_cross_provider_owner_with_server_key_ok(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-server")
    cfg = apply_llm_overrides(_BASE, {
        "advanced": True, "deep_provider": "claude-cli", "quick_provider": "openai",
        "deep_model": "claude-opus-4-8", "quick_model": "gpt-5.4-mini",
        "allow_server_key": True})
    code, msg = levels_credential_error(cfg, {"allow_server_key": True})
    assert code is None and msg is None


@pytest.mark.unit
def test_claude_cli_level_without_owner_is_owner_only():
    cfg = apply_llm_overrides(_BASE, {
        "advanced": True, "deep_provider": "claude-cli", "quick_provider": "openai",
        "deep_model": "claude-opus-4-8", "quick_model": "gpt-5.4-mini"})
    code, msg = levels_credential_error(cfg, {"allow_server_key": False})
    assert code == "owner_only"
    assert "Pesado" in msg  # nomeia o nível bloqueado


@pytest.mark.unit
def test_level_provider_without_key_is_need_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Rápido=openai sem BYOK e sem env do servidor → erro nomeando o nível.
    cfg = apply_llm_overrides(_BASE, {
        "advanced": True, "deep_provider": "claude-cli", "quick_provider": "openai",
        "deep_model": "claude-opus-4-8", "quick_model": "gpt-5.4-mini",
        "allow_server_key": True})
    code, msg = levels_credential_error(cfg, {"allow_server_key": True})
    assert code == "need_key"
    assert "Rápido" in msg and "openai" in msg


@pytest.mark.unit
def test_simple_openai_owner_ok(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-server")
    cfg = apply_llm_overrides(_BASE, {"provider": "openai", "allow_server_key": True})
    code, _ = levels_credential_error(cfg, {"allow_server_key": True})
    assert code is None


@pytest.mark.unit
def test_keyless_provider_passes(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = apply_llm_overrides(_BASE, {
        "advanced": True, "deep_provider": "ollama", "quick_provider": "ollama",
        "deep_model": "llama3", "quick_model": "llama3", "allow_server_key": True})
    code, _ = levels_credential_error(cfg, {"allow_server_key": True})
    assert code is None  # ollama não exige chave

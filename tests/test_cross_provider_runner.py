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


# ------------------------------- normalização de FORMATO do id (task 016) -------
# O id de modelo não é portável entre provedores: OpenRouter usa "vendor/modelo",
# a API Anthropic (e a assinatura claude-cli, que fala a mesma API) só entende o id
# PURO. A config efetiva é o último ponto antes de montar o client — é aqui que um id
# no formato do provedor ANTERIOR tem que morrer, senão vira 404 no meio da run.
@pytest.mark.unit
def test_modelo_formato_openrouter_no_claude_cli_e_normalizado():
    # o bug reportado: provedor trocado pra assinatura, modelo ficou no formato antigo
    cfg = apply_llm_overrides(_BASE, {
        "provider": "claude-cli", "deep_model": "anthropic/claude-opus-5",
        "quick_model": "anthropic/claude-haiku-4.5", "allow_server_key": True})
    assert cfg["deep_think_llm"] == "claude-opus-5"
    assert cfg["quick_think_llm"] == "claude-haiku-4-5"


@pytest.mark.unit
def test_normalizacao_vale_por_nivel_no_avancado():
    # cada nível normaliza pelo SEU provedor: o Rápido segue OpenAI, o Pesado vira
    # id puro da Anthropic — nada de aplicar o formato do provedor-base nos dois.
    cfg = apply_llm_overrides(_BASE, {
        "advanced": True, "deep_provider": "claude-cli", "quick_provider": "openai",
        "deep_model": "anthropic/claude-opus-5", "quick_model": "openai/gpt-5.4-mini",
        "allow_server_key": True})
    assert cfg["deep_think_llm"] == "claude-opus-5"
    assert cfg["quick_think_llm"] == "gpt-5.4-mini"


@pytest.mark.unit
def test_id_fora_do_catalogo_nao_e_trocado_pelo_default():
    # A config efetiva normaliza só o FORMATO: um id que o catálogo não conhece pode
    # ser um fine-tune/deploy do próprio usuário. Quem reseta sobra de OUTRO provedor
    # é a troca de provedor na UI (que sabe que o id é resto do provedor anterior).
    cfg = apply_llm_overrides(_BASE, {
        "advanced": True, "deep_provider": "openai", "quick_provider": "openai",
        "deep_model": "ft:gpt-5.4-meu-modelo", "quick_model": "modelo-interno-v3",
        "allow_server_key": True})
    assert cfg["deep_think_llm"] == "ft:gpt-5.4-meu-modelo"
    assert cfg["quick_think_llm"] == "modelo-interno-v3"


@pytest.mark.unit
def test_modelo_ja_correto_nao_e_alterado():
    cfg = apply_llm_overrides(_BASE, {
        "advanced": True, "deep_provider": "claude-cli", "quick_provider": "openai",
        "deep_model": "claude-opus-4-8", "quick_model": "gpt-5.4-mini",
        "allow_server_key": True})
    assert cfg["deep_think_llm"] == "claude-opus-4-8"
    assert cfg["quick_think_llm"] == "gpt-5.4-mini"


@pytest.mark.unit
def test_config_do_servidor_sem_overrides_fica_intacta():
    # apply_llm_overrides(base, None) é chamado só pra LER a config — não pode mexer
    # no modelo do servidor (que já está no formato do provedor dele).
    assert apply_llm_overrides(_BASE, None) == dict(_BASE)

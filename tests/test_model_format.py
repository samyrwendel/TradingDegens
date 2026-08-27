"""Formato do id de modelo POR PROVEDOR — normalização (task 20260827-016).

Bug do Samyr: com OpenRouter salvo, trocar o provedor pra "Claude — assinatura
(claude-cli)" deixava os MODELOS no formato OpenRouter ("anthropic/claude-opus-5").
A API Anthropic (que a assinatura fala via proxy) não entende o prefixo ``anthropic/``
→ 404 ``AnthropicModelNotFoundError: model: anthropic/claude-opus-5`` no meio da run.

Aqui provamos a conversão pura (sem rede): o id vira o formato do provedor, o
provedor de id livre (Ollama/self-host) não é tocado, e o ``strict`` separa os dois
momentos — na TROCA de provedor um id de outra família cai no default do catálogo;
na leitura de um id DIGITADO só o formato é corrigido.
"""

import pytest

from tradingagents.llm_clients.model_format import (
    FORMAT_BARE,
    FORMAT_FREE,
    FORMAT_VENDOR_SLASH,
    id_format_meta,
    normalize_model_id,
    provider_format,
)


# ------------------------------------------------------------ o bug reportado --
@pytest.mark.unit
def test_openrouter_id_no_claude_cli_perde_o_prefixo():
    # o 404 exato do brief: PESADO = anthropic/claude-opus-5 na assinatura
    assert normalize_model_id("claude-cli", "anthropic/claude-opus-5", "deep") == "claude-opus-5"


@pytest.mark.unit
def test_grafia_openrouter_com_ponto_vira_id_anthropic_com_traco():
    # OpenRouter escreve a versão com PONTO; a API nativa usa TRAÇO. Tirar só a
    # barra deixaria "claude-haiku-4.5" — um 404 disfarçado.
    assert normalize_model_id("claude-cli", "anthropic/claude-haiku-4.5", "quick") \
        == "claude-haiku-4-5"
    assert normalize_model_id("anthropic", "anthropic/claude-haiku-4.5", "quick") \
        == "claude-haiku-4-5"


@pytest.mark.unit
def test_aliases_do_provedor_valem_igual():
    for prov in ("claude_cli", "claude-subscription"):
        assert normalize_model_id(prov, "anthropic/claude-opus-5", "deep") == "claude-opus-5"


# ---------------------------------------------- id de OUTRA família → default --
@pytest.mark.unit
def test_modelo_de_outro_provedor_reseta_pro_default_do_catalogo():
    # gpt num provedor Claude não é normalizável: vira o default daquele nível.
    quick = normalize_model_id("claude-cli", "gpt-5.5", "quick")
    deep = normalize_model_id("claude-cli", "openai/gpt-5.5", "deep")
    assert quick.startswith("claude")
    assert deep.startswith("claude")
    # e o default é por NÍVEL (rápido != pesado no catálogo da assinatura)
    assert quick != deep


@pytest.mark.unit
def test_claude_em_provedor_openai_reseta_pra_gpt():
    assert normalize_model_id("openai", "anthropic/claude-opus-5", "deep").startswith("gpt")


# ------------------------------------------------------- id já certo: intacto --
@pytest.mark.unit
def test_id_ja_no_formato_do_provedor_passa_intacto():
    assert normalize_model_id("claude-cli", "claude-sonnet-5", "deep") == "claude-sonnet-5"
    assert normalize_model_id("openai", "gpt-5.4-mini", "quick") == "gpt-5.4-mini"
    assert normalize_model_id("google", "gemini-3.5-flash", "quick") == "gemini-3.5-flash"


@pytest.mark.unit
def test_id_novo_fora_do_catalogo_mas_da_familia_e_preservado():
    # O catálogo curado envelhece (modelo novo sai antes de entrar na lista):
    # resetar um id VÁLIDO seria pior que o bug. Família certa = passa.
    assert normalize_model_id("claude-cli", "claude-opus-9", "deep") == "claude-opus-9"
    assert normalize_model_id("anthropic", "claude-haiku-4-5-20251001", "quick") \
        == "claude-haiku-4-5-20251001"


@pytest.mark.unit
def test_normalizacao_e_idempotente():
    once = normalize_model_id("claude-cli", "anthropic/claude-opus-5", "deep")
    assert normalize_model_id("claude-cli", once, "deep") == once


@pytest.mark.unit
def test_modelo_vazio_continua_vazio():
    # vazio = "padrão do provedor"; quem resolve o default é o apply_llm_overrides.
    assert normalize_model_id("claude-cli", "", "deep") == ""
    assert normalize_model_id("claude-cli", None, "deep") == ""


# ----------------------------------------------------- outros estilos de id ----
@pytest.mark.unit
def test_provedor_de_id_livre_nao_e_tocado():
    # Ollama/self-host: o id é do deploy do usuário, nada a normalizar.
    assert normalize_model_id("ollama", "qwen3:latest", "quick") == "qwen3:latest"
    assert normalize_model_id("openai_compatible", "vendor/whatever", "deep") == "vendor/whatever"
    assert normalize_model_id("bedrock", "us.anthropic.claude-opus-4-8-v1:0", "deep") \
        == "us.anthropic.claude-opus-4-8-v1:0"


@pytest.mark.unit
def test_openrouter_exige_vendor_slash_e_ganha_o_namespace():
    # rota inversa: voltar pro OpenRouter com um id puro também dá 404 — põe o vendor.
    assert normalize_model_id("openrouter", "claude-opus-5", "deep") == "anthropic/claude-opus-5"
    assert normalize_model_id("openrouter", "gpt-5.5", "deep") == "openai/gpt-5.5"
    # id que já tem namespace fica como está
    assert normalize_model_id("openrouter", "z-ai/glm-5.2", "deep") == "z-ai/glm-5.2"


@pytest.mark.unit
def test_provider_format_classifica_os_provedores():
    assert provider_format("claude-cli") == FORMAT_BARE
    assert provider_format("anthropic") == FORMAT_BARE
    assert provider_format("openrouter") == FORMAT_VENDOR_SLASH
    assert provider_format("ollama") == FORMAT_FREE
    assert provider_format("provedor-que-nao-existe") == FORMAT_FREE


# -------------------------------------------------- meta que o front consome ---
@pytest.mark.unit
def test_id_format_meta_alimenta_a_mesma_regra_no_front():
    meta = id_format_meta("claude-cli")
    assert meta["style"] == FORMAT_BARE
    assert meta["families"] == ["claude"]
    assert meta["vendor_ns"] == "anthropic"
    assert id_format_meta("ollama")["style"] == FORMAT_FREE


# ------------------------------------------- strict: os dois momentos do fix ---
# strict=True é a TROCA de provedor na UI (o id é resto do provedor anterior);
# strict=False é a rede de proteção sobre um id que o usuário DIGITOU.
@pytest.mark.unit
def test_nao_estrito_corrige_formato_mas_preserva_id_proprio():
    # fine-tune / deploy do usuário: fora do catálogo, fora da família — é escolha dele
    assert normalize_model_id("openai", "ft:gpt-5.4-meu", "deep", strict=False) \
        == "ft:gpt-5.4-meu"
    assert normalize_model_id("openai", "modelo-interno-v3", "quick", strict=False) \
        == "modelo-interno-v3"
    # mas o FORMATO continua sendo corrigido — é o bug reportado
    assert normalize_model_id("claude-cli", "anthropic/claude-opus-5", "deep", strict=False) \
        == "claude-opus-5"


@pytest.mark.unit
def test_estrito_reseta_sobra_de_outro_provedor():
    assert normalize_model_id("claude-cli", "gpt-5.5", "deep", strict=True).startswith("claude")
    # ...e o não-estrito, no MESMO id, não mexe (a diferença é só o momento)
    assert normalize_model_id("claude-cli", "gpt-5.5", "deep", strict=False) == "gpt-5.5"


@pytest.mark.unit
def test_nao_estrito_sem_casar_catalogo_ainda_tira_o_namespace():
    # id novo demais pro catálogo, mas o prefixo do OpenRouter tem que sair de qualquer jeito
    assert normalize_model_id("claude-cli", "anthropic/claude-opus-9", "deep", strict=False) \
        == "claude-opus-9"

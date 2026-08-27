
from .base_client import BaseLLMClient


def create_llm_client(
    provider: str,
    model: str,
    base_url: str | None = None,
    **kwargs,
) -> BaseLLMClient:
    """Create an LLM client for the specified provider.

    Provider modules are imported lazily so that simply importing this
    factory (e.g. during test collection) does not pull in heavy LLM SDKs
    or fail when their API keys are absent.

    Args:
        provider: LLM provider name
        model: Model name/identifier
        base_url: Optional base URL for API endpoint
        **kwargs: Additional provider-specific arguments

    Returns:
        Configured BaseLLMClient instance

    Raises:
        ValueError: If provider is not supported
    """
    provider_lower = provider.lower()

    # Assinatura Claude via CLI OAuth (task 20260826-030): custo por token = $0.
    # Fala a API Anthropic normal, mas roteia por um PROXY LOCAL que injeta o Bearer
    # da assinatura (o token OAuth do CLI vive só no proxy, nunca aqui). Por isso a
    # ``base_url`` é FORÇADA pro proxy (ignora qualquer base_url do cliente) e a
    # ``api_key`` é dummy — a auth real é do proxy. Owner-gated no runner (a
    # assinatura é do dono; público nunca cai aqui). Reusa o AnthropicClient, então
    # streaming/custo/effort/temperature valem igual.
    if provider_lower in ("claude-cli", "claude_cli", "claude-subscription"):
        from .anthropic_client import AnthropicClient
        from .claude_cli_proxy import proxy_base_url
        kwargs.pop("api_key", None)
        return AnthropicClient(
            model, base_url=proxy_base_url(), api_key="claude-cli-oauth", **kwargs
        )

    # Native (non-OpenAI) APIs are matched first so their string check doesn't
    # import the OpenAI client. Everything else is OpenAI-compatible and routes
    # through the provider registry (single source of truth).
    if provider_lower == "anthropic":
        from .anthropic_client import AnthropicClient
        return AnthropicClient(model, base_url, **kwargs)

    if provider_lower == "google":
        from .google_client import GoogleClient
        return GoogleClient(model, base_url, **kwargs)

    if provider_lower == "azure":
        from .azure_client import AzureOpenAIClient
        return AzureOpenAIClient(model, base_url, **kwargs)

    if provider_lower == "bedrock":
        from .bedrock_client import BedrockClient
        return BedrockClient(model, base_url, **kwargs)

    from .openai_client import OpenAIClient, is_openai_compatible
    if is_openai_compatible(provider_lower):
        return OpenAIClient(model, base_url, provider=provider_lower, **kwargs)

    raise ValueError(f"Unsupported LLM provider: {provider}")

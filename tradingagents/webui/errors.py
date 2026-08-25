"""Traduz erro cru de provider LLM → mensagem clara, acionável, em pt-BR.

O motor pode estourar com o erro técnico do SDK (OpenAIRateLimitError 429
'no credits', 401 chave inválida, rate limit, timeout de rede). Jogar isso cru na
UI (com stack trace) é ruído e ainda arrisca vazar a chave. Este módulo mapeia o
erro pra uma frase curta que diz O QUE fazer — sem stack, sem chave (o texto que
chega aqui já vem redigido pelo caller; ver runner._redact_secret).

Uso:
    info = humanize_provider_error(raw_text, provider="openai")
    # -> {"code": "no_credit", "message": "Sua chave OpenAI está sem crédito…"}

``code`` é estável (no_credit | invalid_key | rate_limit | unavailable | error) pra
UI decidir o call-to-action (ex.: abrir ⚙️ Configurações). ``None`` de
:func:`classify_provider_error` significa 'não reconhecido' — o caller usa o
fallback genérico (mensagem curta + tipo do erro, já redigida)."""

from __future__ import annotations

# Recusa quando a requisição não é do dono logado E não trouxe chave própria — o
# público precisa de BYOK; só o dono usa a chave do servidor. Nunca cai na env.
NEED_KEY_CODE = "need_key"
NEED_KEY_MESSAGE = (
    "Informe sua chave de API nas Configurações (⚙️) para rodar. "
    "Só o dono logado usa a chave do servidor."
)

# provider id -> rótulo amigável pra frase (o resto capitaliza o id cru).
_PROVIDER_LABELS = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "openrouter": "OpenRouter",
    "ollama": "Ollama",
    "google": "Google",
    "deepseek": "DeepSeek",
    "xai": "xAI",
    "azure": "Azure OpenAI",
    "bedrock": "AWS Bedrock",
    "openai_compatible": "o endpoint",
}


def provider_label(provider: str | None) -> str:
    """Rótulo amigável do provider pra frase (fallback: capitaliza o id)."""
    pid = (provider or "").strip().lower()
    if not pid:
        return "o provider"
    return _PROVIDER_LABELS.get(pid) or pid.capitalize()


def classify_provider_error(text: str) -> str | None:
    """Classifica um erro cru em um ``code`` estável, ou ``None`` se não reconhecido.

    Ordem importa: sem-crédito (429 quota) é checado ANTES do rate-limit genérico,
    porque a mensagem de quota também casa 429/'rate limit'."""
    low = (text or "").lower()
    if not low:
        return None
    # 429 sem crédito / quota esgotada (o caso do brief: 'no credits remaining').
    if any(s in low for s in (
        "insufficient_quota", "no credits", "credit_balance", "credit balance",
        "exceeded your current quota", "billing", "out of credits", "payment required",
    )):
        return "no_credit"
    # 401 / chave inválida ou ausente (inclui o ValueError próprio 'is not set').
    if any(s in low for s in (
        "invalid api key", "incorrect api key", "invalid_api_key", "authenticationerror",
        "authentication_error", "unauthorized", "401", "is not set", "no api key",
        "missing api key", "api key not", "permission_denied", "invalid x-api-key",
    )):
        return "invalid_key"
    # 429 / limite de requisições (sem ser quota).
    if any(s in low for s in ("rate limit", "ratelimit", "too many requests", "429")):
        return "rate_limit"
    # timeout / rede / provider fora do ar.
    if any(s in low for s in (
        "timeout", "timed out", "connection", "connect error", "network",
        "temporarily unavailable", "service unavailable", "overloaded",
        "502", "503", "504", "bad gateway", "econnrefused",
    )):
        return "unavailable"
    return None


def humanize_provider_error(text: str, provider: str | None = None) -> dict | None:
    """Erro cru (JÁ redigido) → ``{"code", "message"}`` em pt-BR, ou ``None``.

    ``None`` quando o erro não casa nenhum padrão conhecido — aí o caller mantém o
    fallback genérico. O texto de entrada nunca deve conter a chave (redija antes)."""
    code = classify_provider_error(text)
    if code is None:
        return None
    lbl = provider_label(provider)
    if code == "no_credit":
        msg = (f"Sua chave {lbl} está sem crédito. Adicione crédito na conta do "
               f"provider ou troque de provider nas Configurações (⚙️).")
    elif code == "invalid_key":
        msg = (f"Chave inválida ou ausente para {lbl}. Confira a chave em "
               f"Configurações (⚙️).")
    elif code == "rate_limit":
        msg = "Limite de requisições atingido — tente de novo em instantes."
    elif code == "unavailable":
        msg = f"{lbl} está indisponível no momento — tente de novo."
    else:  # pragma: no cover - classify só devolve os códigos acima
        return None
    return {"code": code, "message": msg}

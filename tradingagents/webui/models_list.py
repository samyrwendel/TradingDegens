"""Lista os modelos que uma chave/provider dá acesso (proxy do backend).

O front (BYOK) quer popular os dropdowns de modelo rápido/pesado com os modelos
REAIS da chave, sem expor a chave a CORS no navegador. Este módulo faz a chamada
server-side ao endpoint de modelos do provider e devolve só os ids:

    OpenAI/OpenRouter/compatíveis: GET <base>/models  (Authorization: Bearer)
    Anthropic:                     GET <base>/models  (x-api-key + anthropic-version)
    Ollama (local):                GET <root>/api/tags

Segurança: a chave chega por header/corpo (nunca querystring), é usada só pra esta
chamada e NUNCA é gravada/logada — só os NOMES dos modelos voltam. Erros de rede/
auth sobem como exceção pro caller redigir a chave e humanizar (ver errors.py)."""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable

# Endpoints default por provider OpenAI-compatível (mesma fonte do provider
# registry do openai_client). base_url explícito do usuário tem prioridade.
_OPENAI_COMPAT_BASE = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "deepseek": "https://api.deepseek.com",
    "xai": "https://api.x.ai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "groq": "https://api.groq.com/openai/v1",
    "qwen": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
}

# Ordena "modelos de chat reconhecíveis" primeiro — o usuário acha o que quer sem
# rolar 300 ids. Só ordenação; nada é escondido.
_CHAT_HINTS = ("gpt", "claude", "gemini", "llama", "grok", "deepseek", "mistral",
               "qwen", "moonshot", "kimi", "command", "mixtral", "phi", "yi")


def _rank(model_id: str) -> tuple[int, str]:
    low = model_id.lower()
    return (0 if any(h in low for h in _CHAT_HINTS) else 1, low)


def _get_json(url: str, headers: dict, timeout: float,
              urlopen: Callable) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw)


def fetch_provider_models(provider: str, api_key: str | None,
                          base_url: str | None = None, *, timeout: float = 8.0,
                          limit: int = 400, urlopen: Callable | None = None) -> list[str]:
    """Ids dos modelos do provider. Levanta em falha de rede/auth/parse.

    ``urlopen`` é injetável (default ``urllib.request.urlopen``) pra teste. A chave
    é usada só aqui, em memória — o caller não deve logá-la."""
    provider = (provider or "").strip().lower()
    key = (api_key or "").strip()
    base = (base_url or "").strip().rstrip("/")
    opener = urlopen or urllib.request.urlopen

    if provider == "ollama":
        # /api/tags fica na RAIZ (não no /v1 OpenAI-compatível) — tira o sufixo /v1.
        root = base or "http://localhost:11434"
        if root.endswith("/v1"):
            root = root[:-3].rstrip("/")
        data = _get_json(root + "/api/tags", {}, timeout, opener)
        models = [m.get("name") for m in (data.get("models") or []) if m.get("name")]
    elif provider == "anthropic":
        url = (base or "https://api.anthropic.com/v1") + "/models"
        headers = {"anthropic-version": "2023-06-01"}
        if key:
            headers["x-api-key"] = key
        data = _get_json(url, headers, timeout, opener)
        models = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
    else:
        # OpenAI-compatível (openai/openrouter/deepseek/xai/… ou custom via base_url).
        endpoint = base or _OPENAI_COMPAT_BASE.get(provider)
        if not endpoint:
            raise ValueError(
                f"provider '{provider}' precisa de um base_url para listar modelos"
            )
        headers = {}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        data = _get_json(endpoint.rstrip("/") + "/models", headers, timeout, opener)
        models = [m.get("id") for m in (data.get("data") or []) if m.get("id")]

    # únicos, chat-primeiro, cortados no teto (datalist não precisa de milhares).
    seen: dict[str, None] = {}
    for m in models:
        if m and m not in seen:
            seen[m] = None
    return sorted(seen.keys(), key=_rank)[:limit]

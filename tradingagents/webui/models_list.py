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
    # Gemini expõe uma camada OpenAI-compatível (chat/completions + /models) com
    # auth por header ``Authorization: Bearer <GOOGLE_API_KEY>`` — a MESMA regra
    # dos outros compat. Lista os modelos Gemini com a chave do usuário sem SDK
    # extra e sem NUNCA pôr a chave na URL (?key= não é o caminho aqui). A análise
    # roda pelo GoogleClient nativo (factory); isto é só a listagem do BYOK.
    "google": "https://generativelanguage.googleapis.com/v1beta/openai",
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


def _price_per_million(raw) -> float | None:
    """Preço USD por 1M tokens a partir do valor por-token do provider (string ou
    número). OpenRouter manda ``pricing.prompt``/``completion`` em USD/token; aqui
    vira USD/1M pra caber no rótulo. Valor ausente/inválido/"0" vira None."""
    if raw in (None, "", "0"):
        return None
    try:
        per_token = float(raw)
    except (TypeError, ValueError):
        return None
    if per_token <= 0:
        return None
    return per_token * 1_000_000


def _raw_models(provider: str, api_key: str | None, base_url: str | None,
                timeout: float, opener: Callable) -> list[dict]:
    """Lista crua de dicts do provider (id + name/pricing quando existirem)."""
    provider = (provider or "").strip().lower()
    key = (api_key or "").strip()
    base = (base_url or "").strip().rstrip("/")

    if provider == "ollama":
        # /api/tags fica na RAIZ (não no /v1 OpenAI-compatível) — tira o sufixo /v1.
        root = base or "http://localhost:11434"
        if root.endswith("/v1"):
            root = root[:-3].rstrip("/")
        data = _get_json(root + "/api/tags", {}, timeout, opener)
        # Ollama usa ``name`` como id; sem catálogo de preço.
        return [{"id": m.get("name")} for m in (data.get("models") or []) if m.get("name")]
    if provider == "anthropic":
        url = (base or "https://api.anthropic.com/v1") + "/models"
        headers = {"anthropic-version": "2023-06-01"}
        if key:
            headers["x-api-key"] = key
        data = _get_json(url, headers, timeout, opener)
        return list(data.get("data") or [])
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
    models = list(data.get("data") or [])
    if provider == "google":
        # O /models OpenAI-compat do Gemini devolve ids com prefixo ``models/``
        # (ex.: models/gemini-2.5-flash). O client nativo (langchain_google_genai)
        # aceita ambos, mas tirar o prefixo deixa o dropdown consistente com o
        # catálogo (gemini-2.5-flash) e com o que o usuário reconhece.
        for m in models:
            mid = m.get("id") if isinstance(m, dict) else None
            if isinstance(mid, str) and mid.startswith("models/"):
                m["id"] = mid[len("models/"):]
    return models


def fetch_provider_model_infos(provider: str, api_key: str | None,
                               base_url: str | None = None, *, timeout: float = 8.0,
                               limit: int = 2000,
                               urlopen: Callable | None = None) -> list[dict]:
    """Modelos do provider como dicts ``{id, name, price_in, price_out}``.

    ``name`` é o rótulo amigável (OpenRouter ``name`` / Anthropic ``display_name``;
    cai no id quando não vem). ``price_in``/``price_out`` são USD por 1M tokens (ou
    None). Serve o combobox pesquisável do BYOK (casa id E nome, mostra preço).
    Levanta em falha de rede/auth/parse. A chave é usada só aqui, em memória."""
    opener = urlopen or urllib.request.urlopen
    raw = _raw_models(provider, api_key, base_url, timeout, opener)

    seen: dict[str, dict] = {}
    for m in raw:
        mid = m.get("id") if isinstance(m, dict) else None
        if not mid or mid in seen:
            continue
        name = (m.get("name") or m.get("display_name") or "").strip() if isinstance(m, dict) else ""
        pricing = m.get("pricing") if isinstance(m, dict) else None
        price_in = price_out = None
        if isinstance(pricing, dict):
            price_in = _price_per_million(pricing.get("prompt"))
            price_out = _price_per_million(pricing.get("completion"))
        seen[mid] = {"id": mid, "name": name or mid,
                     "price_in": price_in, "price_out": price_out}
    # Ordena chat-primeiro (só UX). O ``limit`` é uma trava de segurança contra um
    # provider patológico com dezenas de milhares — NÃO uma paginação: o front é
    # combobox pesquisável (filtra a lista TODA, mostra 60), então capar embaixo
    # escondia famílias inteiras. Bug real: OpenRouter tem 418 modelos e o _rank
    # empurrava a família z-ai/ (não está nos _CHAT_HINTS) pro fim, então o corte
    # em 400 sumia com z-ai/glm-5.2. Teto alto (2000) = catálogo completo passa.
    infos = sorted(seen.values(), key=lambda i: _rank(i["id"]))
    return infos[:limit]


def fetch_provider_models(provider: str, api_key: str | None,
                          base_url: str | None = None, *, timeout: float = 8.0,
                          limit: int = 2000, urlopen: Callable | None = None) -> list[str]:
    """Ids dos modelos do provider (compat: só os ids, chat-primeiro). Levanta em
    falha de rede/auth/parse. ``urlopen`` é injetável pra teste."""
    infos = fetch_provider_model_infos(provider, api_key, base_url,
                                       timeout=timeout, limit=limit, urlopen=urlopen)
    return [i["id"] for i in infos]

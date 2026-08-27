"""Formato do ID de modelo POR PROVEDOR — normalização (task 20260827-016).

O id de um modelo NÃO é portável entre provedores. O OpenRouter usa
``vendor/modelo`` (``anthropic/claude-opus-5``); a API Anthropic — e a assinatura
``claude-cli``, que fala a MESMA API por um proxy local — só entende o id PURO
(``claude-opus-5``). Trocar de provedor deixando o id no formato do outro dá 404:

    AnthropicModelNotFoundError: model: anthropic/claude-opus-5

Bug real (run CRWD): o usuário tinha OpenRouter salvo, trocou o provedor pra
``claude-cli`` e os campos de modelo continuaram no formato OpenRouter.

Estilos de id por provedor:

* ``bare``          (openai/anthropic/claude-cli/google/xai/…) — id puro, sem
  ``vendor/``. O prefixo é REMOVIDO e a grafia casa no catálogo do provedor.
* ``vendor_slash``  (openrouter) — exige ``vendor/modelo``. Um id puro reconhecível
  ganha o namespace da sua família (``claude-opus-5`` → ``anthropic/claude-opus-5``).
* ``free``          (ollama/azure/bedrock/self-host) — o id é do deploy do usuário;
  nada é tocado.

Dois momentos, um só ``strict`` separando (ver ``normalize_model_id``): na TROCA de
provedor (UI) o id é resto do provedor anterior e um id de outra FAMÍLIA cai no
default do catálogo; na config efetiva do servidor a normalização é só de FORMATO —
ali o id já passou pela UI e pode ser um fine-tune/deploy próprio que o catálogo não
conhece. A família é checada por PREFIXO, não por lista fechada: o catálogo curado
envelhece (um Claude novo sai antes de entrar na lista) e resetar um id válido só
porque ele ainda não está no catálogo seria pior que o bug.

O front aplica a MESMA regra, alimentado por ``id_format_meta`` na meta de
``/api/config`` — uma fonte só.
"""

from __future__ import annotations

# Estilos de id.
FORMAT_BARE = "bare"
FORMAT_VENDOR_SLASH = "vendor_slash"
FORMAT_FREE = "free"

# Apelidos de provedor aceitos pela factory → chave canônica deste módulo.
_ALIASES = {
    "claude_cli": "claude-cli",
    "claude-subscription": "claude-cli",
}

# Provedores de id PURO (API nativa do dono do modelo).
_BARE_PROVIDERS = (
    "openai", "anthropic", "claude-cli", "google", "xai", "deepseek",
    "qwen", "qwen-cn", "glm", "glm-cn", "minimax", "minimax-cn",
)

# Provedores de id ``vendor/modelo``.
_VENDOR_SLASH_PROVIDERS = ("openrouter",)

# Prefixos que identificam a FAMÍLIA de modelos que cada provedor nativo serve.
# Case-insensitive. Provedor fora do mapa = sem checagem de família (só o formato).
_FAMILIES: dict[str, tuple[str, ...]] = {
    "openai": ("gpt", "o1", "o3", "o4", "chatgpt"),
    "anthropic": ("claude",),
    "claude-cli": ("claude",),
    "google": ("gemini",),
    "xai": ("grok",),
    "deepseek": ("deepseek",),
    "qwen": ("qwen",),
    "qwen-cn": ("qwen",),
    "glm": ("glm",),
    "glm-cn": ("glm",),
    "minimax": ("minimax",),
    "minimax-cn": ("minimax",),
}

# Namespace do provedor no OpenRouter (pra rota inversa: id puro → vendor/modelo).
_VENDOR_NS: dict[str, str] = {
    "openai": "openai",
    "anthropic": "anthropic",
    "claude-cli": "anthropic",
    "google": "google",
    "xai": "x-ai",
    "deepseek": "deepseek",
    "qwen": "qwen",
    "qwen-cn": "qwen",
    "glm": "z-ai",
    "glm-cn": "z-ai",
    "minimax": "minimax",
    "minimax-cn": "minimax",
}


def canonical_provider(provider: str) -> str:
    """Chave canônica do provedor (resolve os apelidos da factory)."""
    key = (provider or "").strip().lower()
    return _ALIASES.get(key, key)


def provider_format(provider: str) -> str:
    """Estilo de id do provedor: ``bare``, ``vendor_slash`` ou ``free``."""
    key = canonical_provider(provider)
    if key in _BARE_PROVIDERS:
        return FORMAT_BARE
    if key in _VENDOR_SLASH_PROVIDERS:
        return FORMAT_VENDOR_SLASH
    return FORMAT_FREE


def model_families(provider: str) -> tuple[str, ...]:
    """Prefixos de modelo que o provedor serve (``()`` = qualquer um)."""
    return _FAMILIES.get(canonical_provider(provider), ())


def vendor_namespace(provider: str) -> str | None:
    """Namespace do provedor no formato ``vendor/modelo`` (ou None)."""
    return _VENDOR_NS.get(canonical_provider(provider))


def id_format_meta(provider: str) -> dict:
    """Regras de formato do provedor pro front aplicar a MESMA normalização."""
    return {
        "style": provider_format(provider),
        "families": list(model_families(provider)),
        "vendor_ns": vendor_namespace(provider),
    }


def _bare_id(model: str) -> str:
    """Tira o namespace ``vendor/`` — provedor de id puro nunca tem barra."""
    return model.rsplit("/", 1)[-1]


def _matches_family(provider: str, model: str) -> bool:
    fams = model_families(provider)
    if not fams:
        return True                       # sem família declarada: aceita qualquer id
    low = model.lower()
    return any(low.startswith(f) for f in fams)


def _ns_for_bare_id(model: str) -> str | None:
    """Namespace OpenRouter de um id puro, pela família (ou None se não der)."""
    for prov, fams in _FAMILIES.items():
        ns = _VENDOR_NS.get(prov)
        if not ns:
            continue
        low = model.lower()
        if any(low.startswith(f) for f in fams):
            return ns
    return None


def _loose(model: str) -> str:
    """Chave frouxa pra casar grafias: o OpenRouter escreve a versão com PONTO
    (``claude-haiku-4.5``) onde a API nativa usa TRAÇO (``claude-haiku-4-5``)."""
    return model.strip().lower().replace(".", "-")


def catalog_ids(provider: str) -> list[str]:
    """Ids conhecidos do provedor no catálogo curado (rápido ∪ pesado)."""
    try:
        from .model_catalog import MODEL_OPTIONS
    except Exception:  # noqa: BLE001 - catálogo é opcional aqui
        return []
    opts = MODEL_OPTIONS.get(canonical_provider(provider))
    if not isinstance(opts, dict):
        return []
    ids: list[str] = []
    for mode_options in opts.values():
        for _label, value in mode_options or []:
            if value and value != "custom" and value not in ids:
                ids.append(value)
    return ids


def _match_catalog(provider: str, model: str) -> str | None:
    """Id do catálogo do provedor equivalente a ``model`` (grafia frouxa), ou None."""
    key = _loose(model)
    for cid in catalog_ids(provider):
        if _loose(cid) == key:
            return cid
    return None


def _catalog_default(provider: str, mode: str) -> str | None:
    """Primeiro modelo do catálogo curado do provedor pro nível (ou None)."""
    try:
        from .model_catalog import MODEL_OPTIONS
    except Exception:  # noqa: BLE001 - catálogo é opcional aqui
        return None
    opts = MODEL_OPTIONS.get(canonical_provider(provider))
    if not isinstance(opts, dict):
        return None
    options = opts.get("deep" if mode == "deep" else "quick") or []
    for _label, value in options:
        if value and value != "custom":
            return value
    return None


def normalize_model_id(provider: str, model: str, mode: str = "deep", *,
                       strict: bool = True) -> str:
    """Id do modelo no formato do ``provider`` (mode = ``deep``/``quick``).

    Devolve o id inalterado quando já está certo (ou quando o provedor aceita
    qualquer string). Prefixo ``vendor/`` sobrando é sempre REMOVIDO pros provedores
    de id puro. Nunca levanta: sem catálogo, devolve o melhor palpite, não vazio.

    ``strict`` decide o que fazer com um id que sobra de OUTRO provedor: com
    ``True`` (a troca de provedor na UI, onde o id é comprovadamente resto do
    provedor anterior) ele cai no default do catálogo; com ``False`` (rede de
    proteção sobre um id que o usuário DIGITOU) só o formato é corrigido — um
    fine-tune/deploy próprio fora do catálogo é escolha legítima, não um bug.
    """
    model = (model or "").strip()
    if not model:
        return model
    style = provider_format(provider)
    if style == FORMAT_FREE:
        return model
    if style == FORMAT_VENDOR_SLASH:
        if "/" in model:
            return model
        ns = _ns_for_bare_id(model)
        return f"{ns}/{model}" if ns else model
    # id puro: sem namespace e da família certa
    bare = _bare_id(model)
    if not bare:
        return _catalog_default(provider, mode) or model
    if bare != model:
        # Veio com namespace ⇒ é um id de OUTRO formato (OpenRouter), não um id
        # nativo por coincidência: a grafia diverge (``claude-haiku-4.5`` ×
        # ``claude-haiku-4-5``). Casa no catálogo (grafia frouxa) ou reseta pro
        # default do provedor — tirar só a barra deixaria um 404 disfarçado.
        hit = _match_catalog(provider, bare)
        if hit:
            return hit
        return (_catalog_default(provider, mode) or bare) if strict else bare
    # Id já puro: confia na FAMÍLIA, não na lista. O catálogo curado envelhece (um
    # Claude novo sai antes de entrar nele) e resetar um id válido seria pior que o
    # bug. Só o que é claramente de outro provedor cai no default.
    if _matches_family(provider, bare) or not strict:
        return bare
    return _catalog_default(provider, mode) or bare

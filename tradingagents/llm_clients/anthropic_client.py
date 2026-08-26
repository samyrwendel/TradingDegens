import re
from typing import Any

from langchain_anthropic import ChatAnthropic

from .base_client import BaseLLMClient, normalize_content
from .validators import validate_model

_PASSTHROUGH_KWARGS = (
    "timeout", "max_retries", "api_key", "max_tokens", "temperature",
    "callbacks", "http_client", "http_async_client", "effort",
)

# Anthropic's extended-thinking ``effort`` parameter is accepted by Opus 4.5+,
# Sonnet 4.6+, and the Claude 5 family (Sonnet 5, Fable 5). Sonnet 4.5 and any
# Haiku version 400 with ``"This model does not support the effort parameter"``
# (#831). Versions may be dotted (``opus-4-8``) or single-number (``sonnet-5``,
# ``fable-5``); the per-family minimum below is forward-compatible.
_EFFORT_EXACT = {
    "claude-mythos-preview",  # non-standard preview name; effort-capable
    "claude-mythos-5",        # Fable 5 twin (Project Glasswing); effort-capable
}
_EFFORT_MODEL = re.compile(r"^claude-(opus|sonnet|fable)-(\d+)(?:-(\d+))?$")
_EFFORT_MIN_VERSION = {"opus": (4, 5), "sonnet": (4, 6), "fable": (5, 0)}


def _supports_effort(model: str) -> bool:
    """Whether Anthropic accepts the ``effort`` parameter for this model."""
    model_lc = model.lower()
    if model_lc in _EFFORT_EXACT:
        return True
    match = _EFFORT_MODEL.match(model_lc)
    if not match:
        return False
    family = match.group(1)
    major = int(match.group(2))
    minor = int(match.group(3)) if match.group(3) else 0
    return (major, minor) >= _EFFORT_MIN_VERSION[family]


# ``temperature`` was deprecated for the Claude 5 family (Opus 5, Sonnet 5,
# Fable 5) and later: sending it returns 400 ``"temperature is deprecated for
# this model"``. Earlier families (opus/sonnet/haiku 4.x) still accept it. This
# mirrors the ``effort`` machine above — a per-family minimum version keeps it
# forward-compatible so future ``claude-{family}-X`` releases inherit the drop.
# Models on 4.x that reject temperature only under extended thinking are caught
# by the invoke-level retry (``_is_temperature_deprecated_error``), not here.
_NO_TEMPERATURE_EXACT = {
    "claude-mythos-preview",  # Claude 5 preview name; temperature deprecated
    "claude-mythos-5",        # Fable 5 twin (Project Glasswing); deprecated
}
_TEMPERATURE_MODEL = re.compile(r"^claude-(opus|sonnet|fable|haiku)-(\d+)(?:-(\d+))?$")
_TEMPERATURE_DEPRECATED_MIN_VERSION = {
    "opus": (5, 0), "sonnet": (5, 0), "fable": (5, 0), "haiku": (5, 0),
}


def _supports_temperature(model: str) -> bool:
    """Whether Anthropic still accepts the ``temperature`` parameter for this model.

    Returns ``False`` for the Claude 5 family and later (which deprecated it).
    Unknown / unmatched model names default to ``True`` — the conservative choice
    here is to keep forwarding temperature and let the invoke-level retry drop it
    if the API actually rejects it, rather than silently omitting a valid knob.
    """
    model_lc = model.lower()
    if model_lc in _NO_TEMPERATURE_EXACT:
        return False
    match = _TEMPERATURE_MODEL.match(model_lc)
    if not match:
        return True
    family = match.group(1)
    major = int(match.group(2))
    minor = int(match.group(3)) if match.group(3) else 0
    return (major, minor) < _TEMPERATURE_DEPRECATED_MIN_VERSION[family]


def _is_temperature_deprecated_error(exc: BaseException) -> bool:
    """True when an Anthropic 400 says ``temperature`` is deprecated/unsupported.

    Model-agnostic safety belt: matches on the message text, so any current or
    future model/route that rejects ``temperature`` is caught, not just the ones
    the name-based gate knows about.
    """
    msg = str(exc).lower()
    return "temperature" in msg and (
        "deprecated" in msg or "not supported" in msg or "unsupported" in msg
    )


class NormalizedChatAnthropic(ChatAnthropic):
    """ChatAnthropic with normalized content output.

    Claude models with extended thinking or tool use return content as a
    list of typed blocks. This normalizes to string for consistent
    downstream handling.

    Also carries a one-shot safety belt: if a call fails with a 400 saying
    ``temperature`` is deprecated/unsupported, drop the parameter and retry
    once. This backstops the name-based gate in :meth:`AnthropicClient.get_llm`
    for models the gate doesn't recognize (e.g. BYOK/base_url routes).
    """

    def invoke(self, input, config=None, **kwargs):
        try:
            return normalize_content(super().invoke(input, config, **kwargs))
        except Exception as exc:
            if self.temperature is None or not _is_temperature_deprecated_error(exc):
                raise
            # Model rejected ``temperature`` — drop it (persists for later calls
            # on this instance too) and retry the request once.
            self.temperature = None
            return normalize_content(super().invoke(input, config, **kwargs))


class AnthropicClient(BaseLLMClient):
    """Client for Anthropic Claude models."""

    def __init__(self, model: str, base_url: str | None = None, **kwargs):
        super().__init__(model, base_url, **kwargs)

    def get_llm(self) -> Any:
        """Return configured ChatAnthropic instance."""
        self.warn_if_unknown_model()
        llm_kwargs = {"model": self.model}

        if self.base_url:
            llm_kwargs["base_url"] = self.base_url

        for key in _PASSTHROUGH_KWARGS:
            if key not in self.kwargs:
                continue
            if key == "effort" and not _supports_effort(self.model):
                continue
            if key == "temperature" and not _supports_temperature(self.model):
                continue
            llm_kwargs[key] = self.kwargs[key]

        return NormalizedChatAnthropic(**llm_kwargs)

    def validate_model(self) -> bool:
        """Validate model for Anthropic."""
        return validate_model("anthropic", self.model)

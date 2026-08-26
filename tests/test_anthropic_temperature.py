"""Tests for Anthropic temperature-parameter handling.

The Claude 5 family (Opus 5, Sonnet 5, Fable 5) and later deprecated the
``temperature`` sampling parameter and reject it with a 400
``"temperature is deprecated for this model"``. Two layers guard against it:

1. A name-based gate (mirroring the ``effort`` machine) that omits temperature
   for the models known to reject it.
2. A model-agnostic invoke-level retry that drops temperature and retries once
   whenever the API says it is deprecated/unsupported — covering models the gate
   does not recognise (e.g. BYOK / base_url routes).
"""

import pytest

from tradingagents.llm_clients import anthropic_client as mod


def _capture_kwargs(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        mod, "NormalizedChatAnthropic",
        lambda **kwargs: captured.setdefault("kwargs", kwargs),
    )
    return captured


@pytest.mark.unit
class TestTemperatureGate:
    @pytest.mark.parametrize(
        "model",
        [
            "claude-opus-5", "claude-sonnet-5", "claude-fable-5",
            "claude-mythos-5", "claude-mythos-preview",
            "claude-opus-5-0", "claude-sonnet-5-1",
        ],
    )
    def test_claude_5_family_drops_temperature(self, monkeypatch, model):
        captured = _capture_kwargs(monkeypatch)
        mod.AnthropicClient(model=model, temperature=0.0, api_key="x").get_llm()
        assert "temperature" not in captured["kwargs"]

    @pytest.mark.parametrize(
        "model",
        [
            "claude-opus-4-8", "claude-sonnet-4-6",
            "claude-haiku-4-5", "claude-opus-4-5",
        ],
    )
    def test_pre_5_models_keep_temperature(self, monkeypatch, model):
        captured = _capture_kwargs(monkeypatch)
        mod.AnthropicClient(model=model, temperature=0.2, api_key="x").get_llm()
        assert captured["kwargs"]["temperature"] == 0.2

    def test_unknown_model_keeps_temperature(self, monkeypatch):
        """Default is permissive — unknown names keep temperature; the invoke
        retry drops it if the API actually rejects it."""
        captured = _capture_kwargs(monkeypatch)
        mod.AnthropicClient(
            model="claude-experimental-x", temperature=0.1, api_key="x"
        ).get_llm()
        assert captured["kwargs"]["temperature"] == 0.1

    def test_other_kwargs_forwarded_when_temperature_dropped(self, monkeypatch):
        """Skipping temperature must not break other passthrough kwargs."""
        captured = _capture_kwargs(monkeypatch)
        mod.AnthropicClient(
            model="claude-sonnet-5",
            temperature=0.0,
            api_key="placeholder",
            max_tokens=1024,
            timeout=30,
        ).get_llm()
        assert "temperature" not in captured["kwargs"]
        assert captured["kwargs"]["api_key"] == "placeholder"
        assert captured["kwargs"]["max_tokens"] == 1024
        assert captured["kwargs"]["timeout"] == 30


@pytest.mark.unit
class TestTemperatureDeprecatedDetection:
    @pytest.mark.parametrize(
        "message",
        [
            "Error code: 400 - temperature is deprecated for this model.",
            "temperature is not supported for this model",
            "The 'temperature' parameter is unsupported here",
        ],
    )
    def test_matches_deprecated_messages(self, message):
        assert mod._is_temperature_deprecated_error(RuntimeError(message))

    @pytest.mark.parametrize(
        "message",
        [
            "Error code: 500 - overloaded_error",
            "max_tokens is required",
            "rate limit exceeded",
        ],
    )
    def test_ignores_unrelated_errors(self, message):
        assert not mod._is_temperature_deprecated_error(RuntimeError(message))


class _FakeResp:
    def __init__(self, content):
        self.content = content


@pytest.mark.unit
class TestTemperatureRetry:
    def test_retry_drops_temperature_and_succeeds(self, monkeypatch):
        """A model the gate did not catch: the 400 triggers a one-shot retry
        without temperature, and the run completes."""
        calls = {"n": 0}

        def fake_invoke(self, input, config=None, **kwargs):
            calls["n"] += 1
            if self.temperature is not None:
                raise RuntimeError(
                    "Error code: 400 - {'type': 'invalid_request_error', "
                    "'message': 'temperature is deprecated for this model.'}"
                )
            return _FakeResp("ok")

        monkeypatch.setattr(mod.ChatAnthropic, "invoke", fake_invoke)
        llm = mod.NormalizedChatAnthropic(
            model="claude-opus-4-8", api_key="x", temperature=0.0
        )
        result = llm.invoke("hi")
        assert result.content == "ok"
        assert llm.temperature is None  # dropped for subsequent calls too
        assert calls["n"] == 2  # original + one retry

    def test_non_temperature_error_propagates_without_retry(self, monkeypatch):
        calls = {"n": 0}

        def fake_invoke(self, input, config=None, **kwargs):
            calls["n"] += 1
            raise RuntimeError("Error code: 500 - overloaded")

        monkeypatch.setattr(mod.ChatAnthropic, "invoke", fake_invoke)
        llm = mod.NormalizedChatAnthropic(
            model="claude-opus-4-8", api_key="x", temperature=0.0
        )
        with pytest.raises(RuntimeError, match="overloaded"):
            llm.invoke("hi")
        assert calls["n"] == 1
        assert llm.temperature == 0.0  # untouched

    def test_no_retry_when_temperature_already_absent(self, monkeypatch):
        """If temperature was never set, a deprecated-error can't be about it —
        don't retry, just propagate."""
        calls = {"n": 0}

        def fake_invoke(self, input, config=None, **kwargs):
            calls["n"] += 1
            raise RuntimeError("temperature is deprecated for this model")

        monkeypatch.setattr(mod.ChatAnthropic, "invoke", fake_invoke)
        llm = mod.NormalizedChatAnthropic(model="claude-sonnet-5", api_key="x")
        with pytest.raises(RuntimeError):
            llm.invoke("hi")
        assert calls["n"] == 1

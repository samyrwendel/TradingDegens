"""Streaming ligado nos clients LLM (task 011) — completa o raciocínio ao vivo (008).

O diagnóstico: o ThinkingCallbackHandler tem ``on_llm_new_token``, mas os clients
eram criados SEM ``streaming=True``, então o token nunca chegava e o card só aparecia
quando o agente terminava (``on_llm_end``). Aqui provamos:

  1. cada client agora nasce com ``streaming=True`` (e ``stream_usage=True`` onde o
     provider suporta, pra o custo continuar sendo medido);
  2. com isso, ``.invoke()`` — o que o pipeline chama — toma o caminho de streaming
     (``_should_stream`` True), então ``on_llm_new_token`` dispara token-a-token;
  3. o fallback: ``streaming=False`` volta ao reveal-no-fim (provider/rota que não
     stremar), sem quebrar;
  4. ponta-a-ponta: um chat model que stremar de verdade faz o card CRESCER via o
     ThinkingCallbackHandler.
"""

import warnings

import pytest

from tradingagents.llm_clients.factory import create_llm_client

pytestmark = pytest.mark.unit


def _llm(provider, model, **kw):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")   # modelos de teste fora da lista conhecida
        return create_llm_client(provider, model, api_key="sk-test", **kw).get_llm()


@pytest.mark.parametrize("provider,model", [
    ("openai", "gpt-5.4-mini"),
    ("openrouter", "openai/gpt-4o-mini"),
    ("deepseek", "deepseek-chat"),
    ("anthropic", "claude-opus-4-8"),
    ("google", "gemini-3-pro"),
])
def test_clients_stream_by_default(provider, model):
    llm = _llm(provider, model)
    assert llm.streaming is True
    # .invoke() (o que o pipeline chama) toma o caminho de streaming → on_llm_new_token
    assert llm._should_stream(async_api=False) is True


@pytest.mark.parametrize("provider,model", [
    ("openai", "gpt-5.4-mini"),
    ("openrouter", "openai/gpt-4o-mini"),
    ("anthropic", "claude-opus-4-8"),
])
def test_stream_usage_on_for_cost_tracking(provider, model):
    # OpenAI-family e Anthropic mandam a usage DENTRO do stream, então o
    # UsageMetadataCallbackHandler continua medindo o custo mesmo streamando.
    assert _llm(provider, model).stream_usage is True


@pytest.mark.parametrize("provider,model", [
    ("openai", "gpt-5.4-mini"),
    ("anthropic", "claude-opus-4-8"),
    ("google", "gemini-3-pro"),
])
def test_streaming_false_is_honored_fallback(provider, model):
    # Fallback explícito: provider/rota sem streaming volta ao reveal-no-fim.
    llm = _llm(provider, model, streaming=False)
    assert llm.streaming is False
    assert llm._should_stream(async_api=False) is False


def test_azure_streams(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT_NAME", "dep")
    monkeypatch.setenv("OPENAI_API_VERSION", "2025-03-01-preview")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com/")
    llm = create_llm_client("azure", "gpt-4o", api_key="x").get_llm()
    assert llm.streaming is True and llm.stream_usage is True


def test_streaming_tokens_feed_the_thinking_card():
    # Ponta-a-ponta com um chat model que stremar DE VERDADE (langchain), provando
    # que os tokens crescem o card via o ThinkingCallbackHandler correlacionado por nó.
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from tradingagents.webui.progress import ThinkingCallbackHandler, ThinkingTracker

    tracker = ThinkingTracker()
    handler = ThinkingCallbackHandler(tracker)
    model = GenericFakeChatModel(
        messages=iter([AIMessage(content="leitura técnica do mercado token a token")]))
    seen_lengths = []
    for chunk in model.stream(
        "oi", config={"callbacks": [handler], "metadata": {"langgraph_node": "Market Analyst"}}
    ):
        snap = tracker.snapshot()
        if snap:
            seen_lengths.append(snap[0]["len"])
    snap = tracker.snapshot()
    assert snap and snap[0]["id"] == "Market Analyst"
    assert "leitura técnica" in snap[0]["text"]
    # cresceu progressivamente (mais de um tamanho distinto ao longo do stream)
    assert len(set(seen_lengths)) > 1, seen_lengths

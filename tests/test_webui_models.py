"""Listagem de modelos por chave/provider (POST /api/models) pra popular os
dropdowns de BYOK. A chave é usada só pra listar, nunca gravada/logada/querystring;
falha vira mensagem humana (041) + fallback texto livre no front.
"""

import io
import json
import threading
import urllib.error
import urllib.request

import pytest

from tradingagents.webui.models_list import (
    fetch_provider_model_infos,
    fetch_provider_models,
)
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.server import make_server
from tradingagents.webui.store import HistoryStore


# ------------------------------------------------------------ fetch (unidade) --
class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


def _fake_urlopen(payload, capture=None):
    def _open(req, timeout=None):
        if capture is not None:
            capture["url"] = req.full_url
            capture["headers"] = dict(req.headers)
        return _FakeResp(json.dumps(payload).encode())
    return _open


def test_fetch_openai_compatible_parses_data_ids():
    cap = {}
    op = _fake_urlopen({"data": [{"id": "gpt-5.5"}, {"id": "gpt-5.4-mini"}]}, cap)
    out = fetch_provider_models("openai", "sk-K", urlopen=op)
    assert set(out) == {"gpt-5.5", "gpt-5.4-mini"}
    assert cap["url"] == "https://api.openai.com/v1/models"
    # a chave vai no header Authorization, nunca na URL
    assert cap["headers"].get("Authorization") == "Bearer sk-K"
    assert "sk-K" not in cap["url"]


def test_fetch_openrouter_uses_its_base():
    cap = {}
    op = _fake_urlopen({"data": [{"id": "openai/gpt-5.5"}]}, cap)
    fetch_provider_models("openrouter", "sk-or", urlopen=op)
    assert cap["url"] == "https://openrouter.ai/api/v1/models"


def test_fetch_anthropic_uses_x_api_key_header():
    cap = {}
    op = _fake_urlopen({"data": [{"id": "claude-sonnet-5"}]}, cap)
    out = fetch_provider_models("anthropic", "sk-ant", urlopen=op)
    assert out == ["claude-sonnet-5"]
    assert cap["url"] == "https://api.anthropic.com/v1/models"
    # header case-insensitive: urllib capitaliza -> "X-api-key"
    hdr = {k.lower(): v for k, v in cap["headers"].items()}
    assert hdr.get("x-api-key") == "sk-ant"
    assert hdr.get("anthropic-version") == "2023-06-01"


def test_fetch_ollama_hits_api_tags_on_root():
    cap = {}
    op = _fake_urlopen({"models": [{"name": "llama3.1:8b"}, {"name": "qwen2:7b"}]}, cap)
    out = fetch_provider_models("ollama", None, "http://localhost:11434/v1", urlopen=op)
    assert set(out) == {"llama3.1:8b", "qwen2:7b"}
    assert cap["url"] == "http://localhost:11434/api/tags"  # /v1 removido


def test_fetch_chat_models_ranked_first():
    op = _fake_urlopen({"data": [{"id": "whisper-1"}, {"id": "gpt-5.5"}, {"id": "text-embedding-3"}]})
    out = fetch_provider_models("openai", "k", urlopen=op)
    assert out[0] == "gpt-5.5"  # chat-hint vem primeiro


def test_fetch_custom_provider_requires_base_url():
    with pytest.raises(ValueError):
        fetch_provider_models("openai_compatible", "k", None,
                              urlopen=_fake_urlopen({"data": []}))


# --------------------------------------------------- infos (id + nome + preço) --
def test_fetch_infos_parses_name_and_price_per_million():
    # OpenRouter manda name + pricing por-token (USD); vira USD/1M no combobox.
    payload = {"data": [{
        "id": "z-ai/glm-5.2", "name": "Z.AI: GLM 5.2",
        "pricing": {"prompt": "0.0000006", "completion": "0.0000022"},
    }]}
    out = fetch_provider_model_infos("openrouter", "sk-or",
                                     urlopen=_fake_urlopen(payload))
    assert out == [{"id": "z-ai/glm-5.2", "name": "Z.AI: GLM 5.2",
                    "price_in": 0.6, "price_out": 2.2}]


def test_fetch_infos_name_falls_back_to_id_and_price_optional():
    # Provider sem name/pricing (ex.: OpenAI /models): name = id, preços None.
    out = fetch_provider_model_infos("openai", "k",
                                     urlopen=_fake_urlopen({"data": [{"id": "gpt-5.5"}]}))
    assert out == [{"id": "gpt-5.5", "name": "gpt-5.5",
                    "price_in": None, "price_out": None}]


def test_fetch_infos_ranks_chat_first():
    op = _fake_urlopen({"data": [{"id": "text-embedding-3"}, {"id": "gpt-5.5"}]})
    out = fetch_provider_model_infos("openai", "k", urlopen=op)
    assert out[0]["id"] == "gpt-5.5"


def test_fetch_infos_does_not_cut_family_beyond_400(subtests):
    """Regressão: com 418 modelos (o caso real do OpenRouter), a família z-ai/ era
    empurrada pro fim por _rank (não está nos _CHAT_HINTS) e o corte antigo em 400 a
    sumia inteira. Agora o catálogo COMPLETO passa — z-ai/glm-5.2 sobrevive."""
    data = [
        {"id": "openai/gpt-5.5", "name": "OpenAI: GPT-5.5"},          # rank 0 (chat)
        {"id": "anthropic/claude-sonnet-5", "name": "Claude Sonnet 5"},  # rank 0
        {"id": "z-ai/glm-5.2", "name": "Z.AI: GLM 5.2",              # rank 1, sorta por ÚLTIMO
         "pricing": {"prompt": "0.0000006", "completion": "0.0000022"}},
    ]
    # 415 fillers sem chat-hint que sortam ANTES de z-ai (a… < z…) → 418 no total.
    for i in range(415):
        data.append({"id": f"acme/filler-{i:03d}", "name": f"Filler {i}"})
    out = fetch_provider_model_infos("openrouter", "sk-or",
                                     urlopen=_fake_urlopen({"data": data}))
    ids = [i["id"] for i in out]
    with subtests.test("nada é cortado — os 418 modelos vêm"):
        assert len(out) == 418
    with subtests.test("z-ai/glm-5.2 sobrevive ao ranking + cap"):
        assert "z-ai/glm-5.2" in ids
    with subtests.test("preço da família z-ai foi parseado (USD/1M)"):
        z = next(i for i in out if i["id"] == "z-ai/glm-5.2")
        assert z["price_in"] == 0.6 and z["price_out"] == 2.2


# ------------------------------------------------------------- HTTP endpoint ---
def _make_server(tmp_path):
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path),
                                         "llm_provider": "openai",
                                         "deep_think_llm": "gpt-5.5",
                                         "quick_think_llm": "gpt-5.4-mini"},
                            store=HistoryStore(tmp_path))
    httpd = make_server("127.0.0.1", 0, runner=runner)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{port}"


def _post(base, path, payload, headers=None):
    hdr = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode(), headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_http_models_lists_with_header_key(tmp_path, monkeypatch):
    seen = {}

    def fake_fetch(provider, api_key, base_url=None, **kw):
        seen.update(provider=provider, api_key=api_key, base_url=base_url)
        return [{"id": "gpt-5.5", "name": "gpt-5.5", "price_in": None, "price_out": None},
                {"id": "gpt-5.4-mini", "name": "gpt-5.4-mini", "price_in": None, "price_out": None}]

    # o server importa o símbolo no seu namespace: patcha lá
    monkeypatch.setattr("tradingagents.webui.server.fetch_provider_model_infos", fake_fetch)
    httpd, base = _make_server(tmp_path)
    try:
        code, body = _post(base, "/api/models", {"llm_provider": "openai"},
                           headers={"X-LLM-Key": "sk-SECRET-XYZ"})
        assert code == 200 and body["ok"] is True
        ids = [m["id"] for m in body["models"]]
        assert "gpt-5.5" in ids and body["count"] == 2
        # a chave chegou ao fetcher pelo HEADER (não pela querystring/URL)
        assert seen["api_key"] == "sk-SECRET-XYZ"
    finally:
        httpd.shutdown()


def test_http_models_key_not_in_querystring(tmp_path, monkeypatch):
    # a URL da requisição não pode conter a chave em hipótese alguma
    monkeypatch.setattr("tradingagents.webui.server.fetch_provider_model_infos",
                        lambda *a, **k: [{"id": "gpt-5.5", "name": "gpt-5.5",
                                          "price_in": None, "price_out": None}])
    httpd, base = _make_server(tmp_path)
    try:
        req = urllib.request.Request(
            base + "/api/models", data=json.dumps({"llm_provider": "openai"}).encode(),
            headers={"Content-Type": "application/json", "X-LLM-Key": "sk-Q"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
        assert "sk-Q" not in req.full_url
    finally:
        httpd.shutdown()


def test_http_models_error_is_humanized_and_redacted(tmp_path, monkeypatch):
    secret = "sk-BADKEY-999"

    def boom(provider, api_key, base_url=None, **kw):
        raise urllib.error.HTTPError("https://api.openai.com/v1/models", 401,
                                     f"Unauthorized: invalid api key {secret}", {}, None)

    monkeypatch.setattr("tradingagents.webui.server.fetch_provider_model_infos", boom)
    httpd, base = _make_server(tmp_path)
    try:
        code, body = _post(base, "/api/models", {"llm_provider": "openai"},
                           headers={"X-LLM-Key": secret})
        assert code == 200          # não trava: devolve ok:false + fallback
        assert body["ok"] is False
        assert body["models"] == []
        assert body["error_code"] == "invalid_key"
        assert secret not in json.dumps(body, default=str)  # chave redigida
    finally:
        httpd.shutdown()

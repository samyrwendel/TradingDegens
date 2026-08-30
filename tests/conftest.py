"""Shared pytest fixtures that prevent CI hangs when API keys are absent."""

import os
from unittest.mock import MagicMock, patch

import pytest


def pytest_configure(config):
    for marker in ("unit", "integration", "smoke"):
        config.addinivalue_line("markers", f"{marker}: {marker}-level tests")


_API_KEY_ENV_VARS = (
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "XAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "DASHSCOPE_CN_API_KEY",
    "ZHIPU_API_KEY",
    "ZHIPU_CN_API_KEY",
    "MINIMAX_API_KEY",
    "MINIMAX_CN_API_KEY",
    "OPENROUTER_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "ALPHA_VANTAGE_API_KEY",
)


@pytest.fixture(autouse=True)
def _dummy_api_keys(monkeypatch):
    for env_var in _API_KEY_ENV_VARS:
        # `or` not a .get default: an env var present but empty (e.g. a key left
        # blank in a .env copied from .env.example) must still get the placeholder.
        monkeypatch.setenv(env_var, os.environ.get(env_var) or "placeholder")


@pytest.fixture(autouse=True)
def _isolate_config():
    """Reset the global dataflows config before and after each test.

    ``set_config`` merges (it never clears keys absent from the override), so a
    test that sets e.g. ``tool_vendors`` would otherwise leak into later tests
    and make routing behavior order-dependent. Replace the global outright so
    every test starts from a clean DEFAULT_CONFIG.
    """
    import copy

    import tradingagents.dataflows.config as config_module
    import tradingagents.default_config as default_config

    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    yield
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)


@pytest.fixture(autouse=True)
def _disable_datacache(monkeypatch):
    """Keep the in-repo data-governance cache inert during unit tests.

    The cache monkeypatches the social fetchers and the vendor funnel and keys
    social fetches by calendar day, so leaving it active lets one test's fetch
    satisfy another's same-ticker call (cross-test pollution — a cached NVDA
    result would skip the mocked ``_fetch_subreddit`` entirely). Tests that
    exercise the cache itself re-enable it via their own fixture, which runs
    after this autouse one and wins.
    """
    import tradingagents.datacache.cache as _cache

    monkeypatch.setattr(_cache, "DISABLED", True)


@pytest.fixture(autouse=True)
def _e2e_testa_o_working_tree(monkeypatch):
    """A suíte mede o WORKING TREE, não o front publicado.

    Desde a DA-080 o servidor lê os estáticos de um diretório PUBLICADO (a revisão
    commitada) — que é exatamente o que se quer em produção e exatamente o que NÃO se
    quer aqui: um teste de CSS/JS tem de medir o arquivo que está prestes a ser
    commitado, senão ele valida a versão anterior e passa por acidente.

    Os testes da própria publicação (``test_webui_static_publish``) apontam o
    ``_STATIC_DIR`` pra onde precisam nas suas fixtures, que rodam DEPOIS desta.
    """
    from tradingagents.webui import server as _sv, static_publish as _sp

    monkeypatch.setattr(_sv, "_STATIC_DIR", _sp.REPO_STATIC, raising=False)


@pytest.fixture(autouse=True)
def _limpa_cache_de_serie_preparada():
    """Zera o cache de :func:`price_structure._prep` entre testes.

    Ele é um cache de PROCESSO com TTL de 60s, keyado por (símbolo, data, frame) —
    e a suíte troca a FONTE por baixo (``_load_frame`` monkeypatchado por séries
    sintéticas). Dois testes com o mesmo símbolo/data e dados diferentes cairiam na
    mesma chave, e o segundo leria a série do primeiro: cross-test pollution da
    mesma família da que o ``_disable_datacache`` acima já evita.
    """
    from tradingagents.dataflows import price_structure as _ps

    _ps.clear_prep_cache()
    yield
    _ps.clear_prep_cache()


@pytest.fixture()
def mock_llm_client():
    client = MagicMock()
    client.get_llm.return_value = MagicMock()
    with patch(
        "tradingagents.llm_clients.factory.create_llm_client",
        return_value=client,
    ):
        yield client

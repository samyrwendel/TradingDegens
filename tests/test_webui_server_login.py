"""Detecção READ-ONLY do login do CLI da box, por provedor (task 020, parte C).

Prova que a detecção reflete "conectada (login do servidor)" lendo os arquivos dos
CLIs — SEM escrever/apagar nada e SEM nunca devolver o conteúdo do token. Caminhos
sobrepostos por env pra o teste ser hermético (não toca a box real).
"""

import json

import pytest

from tradingagents.webui import server_login as sl


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    # por padrão, tudo aponta pra caminhos inexistentes → nada detectado
    monkeypatch.setenv("TRADINGDEGENS_CODEX_AUTH_FILE", str(tmp_path / "no-codex.json"))
    monkeypatch.setenv("TRADINGDEGENS_CLAUDE_CREDS_FILE", str(tmp_path / "no-claude.json"))
    monkeypatch.setenv("TRADINGDEGENS_GEMINI_DIR", str(tmp_path / "no-gemini"))


def test_all_absent_is_not_connected():
    for key in ("openai", "anthropic", "google"):
        assert sl.detect(key) == {"connected": False, "detected_at": None}


def test_unknown_provider_is_not_connected():
    assert sl.detect("groky")["connected"] is False


# ------------------------------------------------------------------- openai ----
def test_openai_detected_from_codex_auth(monkeypatch, tmp_path):
    f = tmp_path / "codex.json"
    f.write_text(json.dumps({"openai": {"type": "oauth", "access": "AT-SECRET",
                                        "refresh": "RT"}}))
    monkeypatch.setenv("TRADINGDEGENS_CODEX_AUTH_FILE", str(f))
    out = sl.detect("openai")
    assert out["connected"] is True and out["detected_at"]
    assert "AT-SECRET" not in json.dumps(out)          # NUNCA vaza o token


def test_openai_absent_key_not_connected(monkeypatch, tmp_path):
    f = tmp_path / "codex.json"
    f.write_text(json.dumps({"anthropic": {"access": "x"}}))   # sem chave openai
    monkeypatch.setenv("TRADINGDEGENS_CODEX_AUTH_FILE", str(f))
    assert sl.detect("openai")["connected"] is False


# --------------------------------------------------------------- anthropic -----
def test_anthropic_detected_from_credentials(monkeypatch, tmp_path):
    f = tmp_path / "creds.json"
    f.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-SECRET",
                                               "subscriptionType": "max"}}))
    monkeypatch.setenv("TRADINGDEGENS_CLAUDE_CREDS_FILE", str(f))
    out = sl.detect("anthropic")
    assert out["connected"] is True
    assert "sk-ant-SECRET" not in json.dumps(out)


def test_anthropic_without_token_not_connected(monkeypatch, tmp_path):
    f = tmp_path / "creds.json"
    f.write_text(json.dumps({"claudeAiOauth": {"scopes": []}}))   # sem accessToken
    monkeypatch.setenv("TRADINGDEGENS_CLAUDE_CREDS_FILE", str(f))
    assert sl.detect("anthropic")["connected"] is False


# ------------------------------------------------------------------ google -----
def test_google_detected_from_oauth_creds(monkeypatch, tmp_path):
    d = tmp_path / "gem"
    d.mkdir()
    (d / "oauth_creds.json").write_text(json.dumps({"access_token": "ya29-SECRET",
                                                    "refresh_token": "1//RT"}))
    monkeypatch.setenv("TRADINGDEGENS_GEMINI_DIR", str(d))
    out = sl.detect("google")
    assert out["connected"] is True
    assert "ya29-SECRET" not in json.dumps(out)


def test_google_detected_from_active_account(monkeypatch, tmp_path):
    d = tmp_path / "gem"
    d.mkdir()
    (d / "google_accounts.json").write_text(json.dumps({"active": "me@gmail.com",
                                                        "old": []}))
    monkeypatch.setenv("TRADINGDEGENS_GEMINI_DIR", str(d))
    assert sl.detect("google")["connected"] is True


def test_google_null_active_not_connected(monkeypatch, tmp_path):
    d = tmp_path / "gem"
    d.mkdir()
    (d / "google_accounts.json").write_text(json.dumps({"active": None, "old": []}))
    monkeypatch.setenv("TRADINGDEGENS_GEMINI_DIR", str(d))
    assert sl.detect("google")["connected"] is False


# --------------------------------------------------------- read-only guard -----
def test_detection_never_writes_or_mutates(monkeypatch, tmp_path):
    """Guardrail: a detecção não cria/altera/apaga NADA nos caminhos que lê."""
    f = tmp_path / "creds.json"
    f.write_text(json.dumps({"claudeAiOauth": {"accessToken": "AT"}}))
    monkeypatch.setenv("TRADINGDEGENS_CLAUDE_CREDS_FILE", str(f))
    before = f.read_bytes(), f.stat().st_mtime
    for _ in range(3):
        sl.detect("anthropic")
    d = tmp_path / "gem-absent"          # dir inexistente: detecção NÃO deve criá-lo
    monkeypatch.setenv("TRADINGDEGENS_GEMINI_DIR", str(d))
    sl.detect("google")
    assert (f.read_bytes(), f.stat().st_mtime) == before   # inalterado
    assert not d.exists()                                   # nada criado

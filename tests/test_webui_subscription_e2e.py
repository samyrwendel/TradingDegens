"""E2E (Playwright) da assinatura multi-provedor só-dono (task 020, refina a 017/019).

Prova:
- público NÃO vê a seção "Conectar assinatura";
- o DONO vê TRÊS linhas (ChatGPT/Claude/Gemini), cada uma com botão OAuth no estilo
  do app quando desconectada;
- ao conectar, a linha COLAPSA pra "✅ Label conectada · Desconectar" (esconde
  botão/texto/Avançado); desconectar volta o botão;
- cada botão abre a URL de autorização CERTA do seu provedor;
- a DETECÇÃO do login do CLI da box reflete "conectada (login do servidor)"; o
  Desconectar remove só o registro do app — as creds do CLI da box seguem intactas.

Screenshots de cada estado em ``/tmp/devbot-td-sub``. Pulado sem Playwright/Chromium.
"""

import json
import os
import threading

import pytest

import tradingagents.webui.runner as runner_module
from tradingagents.webui.auth import OwnerAuth
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.server import make_server
from tradingagents.webui.store import HistoryStore
from tradingagents.webui.subscription import SubscriptionStore

pytestmark = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None

_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
         "--disable-software-rasterizer", "--disable-gpu-compositing",
         "--disable-features=VizDisplayCompositor"]

_SHOTS = "/tmp/devbot-td-sub"
_PROVIDERS = ["openai", "anthropic", "google"]


def _shot(page, name):
    try:
        os.makedirs(_SHOTS, exist_ok=True)
        page.screenshot(path=os.path.join(_SHOTS, name))
    except Exception:  # noqa: BLE001 — screenshot é evidência, não deve quebrar o teste
        pass


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_module, "fetch_price_chart", lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_actionable_plan", lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_derivatives_report", lambda t, d: "")
    monkeypatch.setenv("TRADINGDEGENS_OWNER_TOKEN", "senha-dono")
    # Detecção do login do CLI aponta pra caminhos INEXISTENTES por padrão → estado
    # desconectado limpo. Testes que querem "login do servidor" reescrevem a env.
    monkeypatch.setenv("TRADINGDEGENS_CODEX_AUTH_FILE", str(tmp_path / "no-codex.json"))
    monkeypatch.setenv("TRADINGDEGENS_CLAUDE_CREDS_FILE", str(tmp_path / "no-claude.json"))
    monkeypatch.setenv("TRADINGDEGENS_GEMINI_DIR", str(tmp_path / "no-gemini"))
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "openai",
                     "deep_think_llm": "gpt-5.5", "quick_think_llm": "gpt-5.4-mini"},
        store=HistoryStore(tmp_path))
    sub = SubscriptionStore(tmp_path / "sub.json")
    httpd = make_server("127.0.0.1", 0, runner=runner, auth=OwnerAuth(), subscription=sub)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}", sub, tmp_path
    finally:
        httpd.shutdown()


def _login_owner(page, base):
    page.goto(base, wait_until="networkidle")
    page.click("#configBtn")
    page.evaluate("""async () => {
      await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'},
        credentials:'same-origin', body: JSON.stringify({password:'senha-dono'})});
      await applyConfig(); renderConfigPanel();
    }""")
    page.wait_for_selector("#subscriptionBox:not(.hidden)")


def _row(provider):
    return f".sub-row[data-provider='{provider}']"


# --------------------------------------------------------- público / rows ------
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_public_does_not_see_subscription(live):
    base, _sub, _tmp = live
    with sync_playwright() as p:
        browser = p.chromium.launch(args=_ARGS)
        page = browser.new_page(viewport={"width": 1100, "height": 800})
        page.goto(base, wait_until="networkidle")
        page.click("#configBtn")
        assert page.query_selector("#subscriptionBox").is_visible() is False
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_three_rows_disconnected_show_connect_buttons(live):
    base, _sub, _tmp = live
    with sync_playwright() as p:
        browser = p.chromium.launch(args=_ARGS)
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        _login_owner(page, base)
        # três linhas, cada uma no estado "conectar" (botão visível, colapsado oculto)
        for prov in _PROVIDERS:
            page.wait_for_selector(f"{_row(prov)} .sub-row-connect:not(.hidden)")
            assert page.query_selector(f"{_row(prov)} .sub-oauth-btn").is_visible() is True
            assert page.query_selector(f"{_row(prov)} .sub-row-connected").is_visible() is False
            assert "btn-primary" in (page.get_attribute(f"{_row(prov)} .sub-oauth-btn", "class") or "")
        # os rótulos certos por provedor
        assert "Conectar com ChatGPT" in page.inner_text(f"{_row('openai')} .sub-oauth-btn")
        assert "Conectar com Claude" in page.inner_text(f"{_row('anthropic')} .sub-oauth-btn")
        assert "Conectar com Google" in page.inner_text(f"{_row('google')} .sub-oauth-btn")
        _shot(page, "01-desconectado-tres-linhas.png")
        browser.close()


# ------------------------------------------------- conectar → colapsa ----------
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_connect_collapses_each_row_and_disconnect_restores(live):
    base, sub, _tmp = live
    with sync_playwright() as p:
        browser = p.chromium.launch(args=_ARGS)
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        _login_owner(page, base)
        labels = {"openai": "ChatGPT", "anthropic": "Claude", "google": "Gemini"}
        for prov, label in labels.items():
            secret = f"sk-{prov.upper()}-SECRET-777"
            # colar token no fallback avançado desta linha
            page.click(f"{_row(prov)} .sub-advanced summary")
            page.wait_for_selector(f"{_row(prov)} .sub-token", state="visible")
            page.fill(f"{_row(prov)} .sub-token", secret)
            page.click(f"{_row(prov)} .sub-connect-btn")
            # COLAPSA: linha "conectada" aparece, "conectar" some
            page.wait_for_selector(f"{_row(prov)} .sub-row-connected:not(.hidden)")
            assert page.query_selector(f"{_row(prov)} .sub-row-connect").is_visible() is False
            lbl = page.inner_text(f"{_row(prov)} .sub-connected-label")
            assert "conectada" in lbl and label in lbl
            assert "Desconectar" in page.inner_text(f"{_row(prov)} .sub-disc-btn")
            # token gravado server-side no arquivo do provedor; nunca sobra no DOM
            assert sub.token(provider=prov) == secret
            assert secret not in page.content()
        _shot(page, "02-conectado-colapsado-tres.png")

        # desconectar o openai volta pro botão (registro do app removido)
        page.click(f"{_row('openai')} .sub-disc-btn")
        page.wait_for_selector(f"{_row('openai')} .sub-row-connect:not(.hidden)")
        assert page.query_selector(f"{_row('openai')} .sub-row-connected").is_visible() is False
        assert sub.token(provider="openai") is None
        _shot(page, "03-openai-desconectado.png")
        browser.close()


# --------------------------------------------- OAuth abre a URL certa ----------
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_each_oauth_button_opens_correct_authorize_url(live):
    base, _sub, _tmp = live
    expect = {
        "openai": ("auth.openai.com/oauth/authorize", "client_id=app_EMoamEEZ73f0CkXaXp7hrann"),
        "anthropic": ("claude.ai/oauth/authorize", "client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e"),
        "google": ("accounts.google.com/o/oauth2/v2/auth",
                   "client_id=681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com"),
    }
    with sync_playwright() as p:
        browser = p.chromium.launch(args=_ARGS)
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        _login_owner(page, base)
        for prov, (host, cid) in expect.items():
            page.evaluate("window.__opened=null; window.open=(u)=>{window.__opened=u; return null;};")
            page.click(f"{_row(prov)} .sub-oauth-btn")
            page.wait_for_function("() => window.__opened && window.__opened.length > 0")
            opened = page.evaluate("window.__opened")
            assert host in opened, (prov, opened)
            assert cid in opened, (prov, opened)
            assert "code_challenge_method=S256" in opened
            assert "response_type=code" in opened
        browser.close()


# ------------------------------- detecção do login do servidor + guardrail -----
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_server_login_detected_and_disconnect_preserves_cli_creds(live, monkeypatch, tmp_path):
    base, sub, _tmp = live
    # simula o CLI do codex logado na box: arquivo de creds REAL do CLI (stand-in)
    cli_creds = tmp_path / "cli-codex.json"
    cli_body = json.dumps({"openai": {"type": "oauth", "access": "AT-DA-BOX",
                                      "refresh": "RT-DA-BOX", "accountId": "acc"}})
    cli_creds.write_text(cli_body)
    monkeypatch.setenv("TRADINGDEGENS_CODEX_AUTH_FILE", str(cli_creds))

    with sync_playwright() as p:
        browser = p.chromium.launch(args=_ARGS)
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        _login_owner(page, base)
        # openai aparece CONECTADA por detecção do login do servidor, sem round-trip
        page.wait_for_selector(f"{_row('openai')} .sub-row-connected:not(.hidden)")
        lbl = page.inner_text(f"{_row('openai')} .sub-connected-label")
        assert "login do servidor" in lbl
        assert "Reconectar" in page.inner_text(f"{_row('openai')} .sub-disc-btn")
        _shot(page, "04-openai-login-do-servidor.png")

        # Guardrail (via fetch — a linha está colapsada, sem UI de colar): conecta o
        # registro do app (0600) e depois DESCONECTA. Desconectar remove SÓ o registro
        # do app; as creds do CLI da box NÃO podem ser tocadas.
        page.evaluate("""async () => {
          await fetch('/api/subscription/connect', {method:'POST', credentials:'same-origin',
            headers:{'Content-Type':'application/json','X-Subscription-Token':'sk-APP-REGISTRO-1'},
            body: JSON.stringify({provider:'openai'})});
        }""")
        assert sub.token(provider="openai") == "sk-APP-REGISTRO-1"   # registro do app existe
        page.evaluate("""async () => {
          await fetch('/api/subscription/disconnect', {method:'POST', credentials:'same-origin',
            headers:{'Content-Type':'application/json'}, body: JSON.stringify({provider:'openai'})});
        }""")
        browser.close()

    # registro do app removido; creds do CLI da box BYTE-A-BYTE intactas
    assert sub.token(provider="openai") is None
    assert cli_creds.read_text() == cli_body


# --------- sessão de dono perdida no restart degrada com dignidade (BUG task 023) ---
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_owner_session_lost_on_restart_degrades_gracefully(tmp_path):
    """O print do bug: dono logado clica "Conectar com Google" e leva "acesso restrito
    ao dono". A causa NÃO é falta de credentials no fetch (o cookie vai) — é que o
    servidor REINICIOU (deploy da 022) e as sessões, em memória por design, zeraram; a
    página, aberta desde antes, ainda se achava logada. O oauth/start aceita o dono com
    sessão válida (ver test_each_oauth_button_opens_correct_authorize_url); o que faltava
    era a UI reagir ao owner_only: cair pro login em vez de mostrar o enigma. Aqui
    simulamos o restart limpando as sessões do servidor com o MESMO cookie no browser."""
    # A senha do dono é lida por OwnerAuth() no __init__ → setar a env ANTES de construí-lo.
    os.environ["TRADINGDEGENS_OWNER_TOKEN"] = "senha-dono"
    # detecção do CLI aponta pra caminhos inexistentes → nada "conectado por servidor"
    os.environ["TRADINGDEGENS_CODEX_AUTH_FILE"] = str(tmp_path / "no-codex.json")
    os.environ["TRADINGDEGENS_CLAUDE_CREDS_FILE"] = str(tmp_path / "no-claude.json")
    os.environ["TRADINGDEGENS_GEMINI_DIR"] = str(tmp_path / "no-gemini")
    auth = OwnerAuth()
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "openai",
                     "deep_think_llm": "gpt-5.5", "quick_think_llm": "gpt-5.4-mini"},
        store=HistoryStore(tmp_path))
    sub = SubscriptionStore(tmp_path / "sub.json")
    httpd = make_server("127.0.0.1", 0, runner=runner, auth=auth, subscription=sub)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=_ARGS)
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            _login_owner(page, base)
            page.wait_for_selector(f"{_row('google')} .sub-oauth-btn")

            # RESTART: o servidor esquece as sessões (o cookie do browser continua o mesmo)
            auth._sessions.clear()

            # o dono clica "Conectar com Google" → oauth/start responde 403 owner_only
            page.evaluate("window.__opened=null; window.open=(u)=>{window.__opened=u; return null;};")
            page.click(f"{_row('google')} .sub-oauth-btn")

            # NÃO abre OAuth com sessão morta; a UI cai pro login e explica (nada de enigma)
            page.wait_for_selector("#ownerLoggedOut:not(.hidden)")
            page.wait_for_selector("#subscriptionBox", state="hidden")
            status = page.inner_text("#ownerStatus")
            assert "expirou" in status.lower(), status
            assert page.evaluate("window.__opened") is None
            _shot(page, "06-sessao-expirada-cai-pro-login.png")

            # RECUPERAÇÃO: reloga (sessão nova, válida) e o MESMO botão passa — prova que
            # oauth/start aceita o dono; o problema era só a sessão morta, não o caminho.
            page.evaluate("""async () => {
              await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'},
                credentials:'same-origin', body: JSON.stringify({password:'senha-dono'})});
              await applyConfig(); renderConfigPanel();
            }""")
            page.wait_for_selector("#subscriptionBox:not(.hidden)")
            page.evaluate("window.__opened=null; window.open=(u)=>{window.__opened=u; return null;};")
            page.click(f"{_row('google')} .sub-oauth-btn")
            page.wait_for_function("() => window.__opened && window.__opened.length > 0")
            assert "accounts.google.com/o/oauth2/v2/auth" in page.evaluate("window.__opened")
            browser.close()
    finally:
        httpd.shutdown()
        for k in ("TRADINGDEGENS_OWNER_TOKEN", "TRADINGDEGENS_CODEX_AUTH_FILE",
                  "TRADINGDEGENS_CLAUDE_CREDS_FILE", "TRADINGDEGENS_GEMINI_DIR"):
            os.environ.pop(k, None)

"""Dependency-free HTTP server for the TradingDegens web UI.

Built on the stdlib ``http.server`` (no FastAPI/uvicorn) so it runs under the
existing venv with nothing to install — the right shape for a systemd service
that must come up on boot. Binds ``0.0.0.0`` by default so it answers on the
host's Tailscale IP, not only localhost (the whole point of the deliverable).

Routes:
    GET  /                     -> index.html
    GET  /static/<file>        -> bundled static asset
    GET  /api/health           -> {"ok": true, ...}
    POST /api/analyze          -> {ticker, date[, compare]} -> {run_id, run_token}
    POST /api/run/<id>/cancel  -> PARAR/PAUSAR: dono OU X-Run-Token daquela run
    POST /api/run/<id>/resume  -> RETOMAR do checkpoint: só o dono (chave do servidor)
    POST /api/compare          -> {a, b} -> meta-judge snapshot over two runs
    POST /api/ask              -> {run_id, question} -> grounded Q&A over a run
    POST /api/test-key         -> {} + X-LLM-Key header -> {ok, provider, model}
    POST /api/test-model       -> {provider,base_url,quick,deep} + X-LLM-Key ->
                                  {ok, models:[{role,latency_ms,sample|error}]}
    POST /api/models           -> {provider,base_url} + X-LLM-Key -> {models:[...]}
    POST /api/login            -> {password} -> cookie de sessão HttpOnly (dono)
    POST /api/logout           -> encerra a sessão do dono

BYOK (traga sua chave): /analyze, /compare, /ask e /test-key aceitam a chave do
usuário no header ``X-LLM-Key`` (nunca em querystring) e provider/modelo/base_url
no corpo do POST. A chave é usada só em memória pra aquela run e nunca é
persistida/logada.

Gating da chave do servidor (task 042): o público (sem sessão do dono) DEVE trazer
a própria chave — sem ela a run é recusada (403 need_key), nunca cai na env. Só o
DONO logado (senha em ``TRADINGDEGENS_OWNER_TOKEN``, verificada server-side em
:mod:`tradingagents.webui.auth`) usa a chave env do servidor sem colar nada. A
chave do servidor nunca é enviada ao cliente.
    GET  /api/status/<run_id>  -> live run snapshot (progress, cost, result)
    GET  /api/run/<run_id>     -> alias of status (from history if needed)
    GET  /api/runs?status=running -> live in-process runs (em andamento)
    GET  /api/history          -> recent run summaries (running runs merged in front)
    GET  /api/chart            -> ?ticker=&date=&tf= -> recomputed chart + plan
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import signal
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from tradingagents.webui import (
    agenda,
    oauth_codex,
    oauth_providers,
    server_login,
    static_publish,
    timeutil,
)
from tradingagents.webui.auth import OwnerAuth
from tradingagents.webui.errors import (
    NEED_KEY_CODE,
    NEED_KEY_MESSAGE,
    humanize_provider_error,
)
from tradingagents.webui.models_list import fetch_provider_model_infos
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.subscription import SubscriptionStore

# ALLOWLIST do corpo do /api/analyze que a isenção de gate do atalho 1-2-3 aceita.
# Fechado por default DE VERDADE: só isenta quando TODA chave com valor de peso no
# corpo está aqui. Flag nova (não listada) = sem isenção, o portão volta a valer —
# o contrário da denylist antiga, que deixava passar tudo que ninguém tivesse
# lembrado de listar.
#
# Dois grupos, ambos incapazes de subir a rota pra um pipeline com modelo:
#   - parâmetros da própria rota estrutural ($0 de LLM);
#   - configuração BYOK, que o front manda em TODA requisição (inclusive de quem
#     não tem chave, ex.: Ollama self-host) e que o 1-2-3 sequer consome.
# `compare` está FORA de propósito: é ele que troca a rota por Padrão x Erick x
# meta-juiz na chave do servidor.
# Métodos ESTRUTURAIS: leem a série e devolvem níveis, $0 de LLM — o portão de custo
# protegeria um custo que não existe. Lista, nunca um ``if`` por método: cada novo
# caminho de isenção é uma chance nova de reabrir o buraco da task 007.
_METODOS_SEM_LLM = frozenset({"setup123", "storm123"})

_CORPO_ISENTO_DE_GATE = frozenset({
    "ticker", "date", "method", "timeframe", "force_fresh",
    "llm_provider", "deep_think_llm", "quick_think_llm", "backend_url",
    "advanced", "deep_provider", "quick_provider",
    "deep_backend_url", "quick_backend_url",
})

# Token de CONTROLE da run (Parar/Pausar): capacidade que só quem INICIOU a run
# recebe, devolvida junto do run_id no /api/analyze. O run_id NÃO serve de prova de
# autoria — ele é público de propósito (/api/runs e /api/history listam qualquer um),
# então quem soubesse ler a lista podia parar a análise alheia. Derivado por HMAC de
# um segredo de processo: nada a guardar, nada a expirar, e impossível de forjar sem
# o segredo. Restart invalida todos, e tudo bem: run BYOK não sobrevive a restart
# (a chave vive no navegador) e a de chave-do-servidor é do dono, que para pela sessão.
_RUN_TOKEN_SECRET = secrets.token_bytes(32)


def _run_token(run_id: str) -> str:
    """Token de controle da run ``run_id`` (ver ``_RUN_TOKEN_SECRET``)."""
    return hmac.new(_RUN_TOKEN_SECRET, (run_id or "").encode(),
                    hashlib.sha256).hexdigest()[:32]


# De onde o front é SERVIDO — o diretório PUBLICADO, não o do repo.
#
# Ler o repo a cada requisição fazia o instante em que um agente salvava ``app.js``
# ser o instante em que o usuário via aquilo: sem commit, sem teste, sem deploy (e o
# cache-buster por mtime garantia que o navegador nem segurasse a versão anterior).
# Ver :mod:`tradingagents.webui.static_publish` — publicar passa a ser um passo
# explícito, e ele copia da REVISÃO COMMITADA.
#
# Resolvido no IMPORT, não a cada requisição: o caminho não muda em voo, e resolver
# por requisição reabriria a porta pra um estado intermediário aparecer na tela.
_STATIC_DIR = static_publish.static_dir()
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class _Handler(BaseHTTPRequestHandler):
    server_version = "TradingDegensWeb/1.0"
    runner: AnalysisRunner  # injected on the server instance
    auth: OwnerAuth         # injected on the server instance
    subscription: SubscriptionStore          # injected on the server instance
    oauth_flows: oauth_providers.PendingOAuth  # injected on the server instance

    # -- helpers --------------------------------------------------------------
    def _send_json(self, obj, status: int = 200, cookies: list[str] | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for cookie in cookies or []:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # HTML nunca é cacheado: sem isso o navegador servia a página antiga com
        # os ?v= velhos e o usuário via "ainda não funciona" depois de um deploy.
        # (os assets já têm cache-buster por mtime, então podem ser cacheados.)
        if "text/html" in content_type:
            self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, name: str) -> None:
        # Prevent path traversal: only a bare filename inside the static dir.
        safe = Path(name).name
        path = _STATIC_DIR / safe
        if not path.is_file():
            self._send_json({"error": "não encontrado"}, 404)
            return
        ctype = _CONTENT_TYPES.get(path.suffix, "application/octet-stream")
        data = path.read_bytes()
        # Cache-buster: o index referencia app.js/style.css por nome puro, então o
        # navegador segurava a versão antiga e a tela "não mudava" depois de um deploy.
        # Reescreve o link com a versão do ASSET PUBLICADO — o sha da revisão quando
        # há publicação, o mtime no modo ao vivo (ver ``versao_do_asset``, que explica
        # por que o mtime sozinho não serve depois que a publicação passou a vir de
        # ``git archive``).
        if safe == "index.html":
            text = data.decode("utf-8")
            for asset in ("app.js", "style.css"):
                if (_STATIC_DIR / asset).is_file():
                    versao = static_publish.versao_do_asset(_STATIC_DIR, asset)
                    text = text.replace(
                        f"/static/{asset}", f"/static/{asset}?v={versao}"
                    )
            data = text.encode("utf-8")
        self._send_bytes(data, ctype)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # -- BYOK -----------------------------------------------------------------
    def _llm_overrides(self, body: dict) -> dict:
        """Overrides de LLM da requisição (BYOK): a CHAVE vem no header
        ``X-LLM-Key`` (nunca em querystring — não vaza em log de acesso); provider,
        modelos e base_url vêm no corpo do POST. Só campos preenchidos entram.

        A chave é usada em memória pra montar o client daquela run e NUNCA é
        gravada/logada/persistida — o ``log_message`` do handler é no-op e o record
        do histórico é montado por campos nomeados, sem a config/chave."""
        ov: dict = {}
        key = (self.headers.get("X-LLM-Key") or "").strip()
        if key:
            ov["api_key"] = key
        prov = (body.get("llm_provider") or "").strip()
        if prov:
            ov["provider"] = prov.lower()
        for bkey, okey in (("deep_think_llm", "deep_model"),
                           ("quick_think_llm", "quick_model"),
                           ("backend_url", "base_url")):
            val = (body.get(bkey) or "").strip()
            if val:
                ov[okey] = val
        # Modo AVANÇADO (task 027): provedor por-nível (RÁPIDO/PESADO) cross-provider.
        # Os modelos reusam deep_think_llm/quick_think_llm acima; aqui vêm só os
        # provedores de cada nível e o flag que liga o caminho avançado no runner.
        if body.get("advanced"):
            ov["advanced"] = True
            for bkey, okey in (("deep_provider", "deep_provider"),
                               ("quick_provider", "quick_provider")):
                val = (body.get(bkey) or "").strip().lower()
                if val:
                    ov[okey] = val
            # Endpoint POR NÍVEL (task 017): com provedor por nível como caminho
            # primário, um self-host (Ollama/compatível) num nível não pode arrastar o
            # endpoint dele pro client do OUTRO nível — cada um manda a sua base_url.
            for bkey, okey in (("deep_backend_url", "deep_base_url"),
                               ("quick_backend_url", "quick_base_url")):
                val = (body.get(bkey) or "").strip()
                if val:
                    ov[okey] = val
        # Só o DONO logado destrava a chave do servidor: marca allow_server_key
        # SEMPRE (True pro dono, False pro público) — assim o runner recusa a
        # requisição pública sem chave própria e nunca cai na env do servidor.
        ov["allow_server_key"] = self._is_owner()
        return ov

    # -- listagem de modelos (BYOK) -------------------------------------------
    def _list_models(self, body: dict) -> dict:
        """Lista os modelos do provider pra popular os dropdowns. Usa a chave do
        header (BYOK); dono logado sem chave própria usa a env do servidor (só os
        NOMES voltam). Nunca grava/loga a chave; erro humanizado + redigido."""
        provider = ((body.get("llm_provider") or body.get("provider") or "")
                    .strip().lower())
        base_url = (body.get("backend_url") or body.get("base_url") or "").strip()
        key = (self.headers.get("X-LLM-Key") or "").strip()
        if not key and self._is_owner():
            # dono sem chave própria: usa a env do servidor só pra LISTAR (nunca
            # devolve a chave — só os nomes dos modelos).
            from tradingagents.llm_clients.api_key_env import get_api_key_env
            env_var = get_api_key_env(provider)
            key = os.environ.get(env_var) if env_var else None
        try:
            # Devolve id + nome + preço (USD/1M) pra o combobox pesquisável casar
            # tanto no id quanto no nome e mostrar o custo — sem jamais expor a chave.
            models = fetch_provider_model_infos(provider, key, base_url or None)
            return {"ok": True, "provider": provider, "models": models,
                    "count": len(models)}
        except Exception as exc:
            raw = self._redact_key(f"{type(exc).__name__}: {exc}")
            human = humanize_provider_error(raw, provider)
            return {"ok": False, "provider": provider, "models": [],
                    "error": human["message"] if human else raw,
                    "error_code": human["code"] if human else None}

    # -- login do dono ---------------------------------------------------------
    def _session_id(self) -> str | None:
        """Id de sessão do cookie ``td_session`` (ou None)."""
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = SimpleCookie(raw)
        except Exception:
            return None
        morsel = jar.get(self.auth.cookie_name)
        return morsel.value if morsel else None

    def _is_owner(self) -> bool:
        """Requisição autenticada como dono? (sessão válida server-side)."""
        return self.auth.is_valid(self._session_id())

    def _gate_or_403(self, body: dict) -> bool:
        """True se a requisição pode rodar LLM (dono logado OU trouxe chave própria).
        Senão, responde 403 com mensagem clara e retorna False (não cria run)."""
        has_key = bool((self.headers.get("X-LLM-Key") or "").strip())
        if has_key or self._is_owner():
            return True
        self._send_json({"error": NEED_KEY_MESSAGE, "error_code": NEED_KEY_CODE}, 403)
        return False

    @staticmethod
    def _rota_sem_llm(method: str, body: dict) -> bool:
        """A requisição vai mesmo subir a rota estrutural de $0 (sem LLM nenhum)?

        Só um método ESTRUTURAL puro (``setup123`` ou ``storm123`` — os dois leem a
        série e não chamam modelo nenhum), e só com um corpo INTEIRAMENTE conhecido:
        toda chave de valor com peso tem que estar em ``_CORPO_ISENTO_DE_GATE``.
        Chave que ninguém reconhece pode ser uma flag de rota nova (``compare`` foi
        exatamente isso) — e diante do desconhecido a resposta é o portão, não a
        isenção. Chave de valor vazio/falso é ignorada porque não liga rota nenhuma
        (o front manda ``compare: false`` no 1-2-3 legítimo).

        O método novo entra pela LISTA, não por um segundo ``if`` paralelo: era o
        segundo caminho de isenção que abriria de novo o buraco que a task 007
        fechou. A allowlist do corpo continua a mesma — ``storm123`` não traz chave
        nova nenhuma.
        """
        if method not in _METODOS_SEM_LLM:
            return False
        return all(k in _CORPO_ISENTO_DE_GATE for k, v in (body or {}).items() if v)

    def _owner_or_403(self) -> bool:
        """Portão só-dono (task 017): rotas da assinatura exigem sessão de dono
        válida ANTES de tudo. Público → 403 e retorna False (não executa nada)."""
        if self._is_owner():
            return True
        self._send_json({"error": "acesso restrito ao dono", "error_code": "owner_only"}, 403)
        return False

    def _controla_a_run_or_403(self, run_id: str) -> bool:
        """Portão do PARAR/PAUSAR: dono logado OU portador do token DAQUELA run.

        Não dá pra usar ``_gate_or_403`` aqui: ele aprova qualquer ``X-LLM-Key``
        não-vazia, sem validar, e um BYOK legítimo continua não sendo dono da run
        alheia — o portão de custo não é portão de autoria. Quem inicia a run recebe
        ``run_token`` na resposta e o devolve em ``X-Run-Token`` pra interromper a
        SUA análise; qualquer outro (com ou sem chave) leva 403 em vez de derrubar
        a análise dos outros."""
        if self._is_owner():
            return True
        tok = (self.headers.get("X-Run-Token") or "").strip()
        if tok and hmac.compare_digest(tok, _run_token(run_id)):
            return True
        self._send_json({"error": "esta execução não é sua",
                         "error_code": "not_run_owner"}, 403)
        return False

    def _redact_key(self, text: str) -> str:
        """Redige credenciais dos headers (``X-LLM-Key`` BYOK e ``X-Subscription-Token``
        da assinatura) de um texto de erro antes de devolvê-lo — o SDK/OS pode ecoar
        o valor numa exceção. Nunca deixa um segredo vazar pela resposta."""
        if not text:
            return text
        for hdr in ("X-LLM-Key", "X-Subscription-Token"):
            secret = (self.headers.get(hdr) or "").strip()
            if secret:
                text = text.replace(secret, "***")
        return text

    # -- OAuth da assinatura (task 019; multi-provedor 020) -------------------
    def _oauth_redirect_uri(self, provider: str = "openai") -> str:
        """redirect_uri do fluxo, por provedor: default = o loopback verbatim do CLI
        de cada assinatura (o único aceito pelo issuer). ``TRADINGDEGENS_OAUTH_REDIRECT``
        sobrepõe (mantido pro openai/compat) pra apontar o callback deste servidor
        quando ele consegue receber o redirect."""
        default = oauth_providers.get(provider).default_redirect
        if (provider or "openai") == "openai":
            return os.getenv("TRADINGDEGENS_OAUTH_REDIRECT", default)
        return default

    def _handle_oauth_callback(self) -> None:
        """Callback do OAuth: ?code&state → valida o state (uso único, carrega o
        provedor), troca o code por token (PKCE) e grava server-side (store 017; ponte
        pro codex-proxy só no openai). Protegido pelo nonce ``state`` (o verifier fica
        no servidor), não pelo cookie — o redirect vem cross-site do issuer. Nada de
        segredo volta ao cliente/log."""
        qs = parse_qs(urlparse(self.path).query)
        err = (qs.get("error", [""])[0] or "").strip()
        code = (qs.get("code", [""])[0] or "").strip()
        state = (qs.get("state", [""])[0] or "").strip()
        if err:
            # ``error`` vem do issuer (ex.: access_denied) — texto controlado, sem segredo.
            self._oauth_html(f"Autorização não concluída ({err}). Você pode tentar de novo.",
                             ok=False)
            return
        taken = self.oauth_flows.take(state)
        if not code or not taken:
            self._oauth_html("Link de conexão inválido ou expirado. Recomece pelo botão "
                             "“Conectar”.", ok=False)
            return
        verifier, provider = taken
        try:
            token_resp = oauth_providers.get(provider).exchange_code(
                code, verifier, redirect_uri=self._oauth_redirect_uri(provider))
        except Exception:  # noqa: BLE001 — nunca ecoa a exceção (pode conter o code)
            self._oauth_html("Não consegui trocar o código pelo token da assinatura.",
                             ok=False)
            return
        # Guarda no store 017 (arquivo 0600, por provedor) — o token NUNCA volta ao cliente.
        try:
            self.subscription.connect(token_resp["access_token"], provider=provider,
                                      connected_at=timeutil.stamp())
        except Exception:  # noqa: BLE001 — segue mesmo se o store falhar
            pass
        # Ponte pro codex-proxy é EXCLUSIVA do openai (grava auth.json do codex). Pros
        # demais NÃO tocamos as creds do CLI da box (guardrail) — a detecção cuida.
        if provider == "openai":
            self._bridge_codex_auth(token_resp)
        self._oauth_html("Assinatura conectada. Pode fechar esta aba e voltar ao "
                         "TradingDegens.", ok=True)

    def _bridge_codex_auth(self, token_resp: dict) -> None:
        """Ponte pro codex-proxy: grava os tokens frescos no arquivo que ele lê
        (``~/.local/share/opencode/auth.json`` → chave ``openai``), reestabelecendo o
        refresh quebrado. É o elo que faz o ``gpt-5.3-codex`` (via litellm) voltar a
        responder. Best-effort e atômico 0600; preserva o que já havia. Caminho
        sobreponível por ``TRADINGDEGENS_CODEX_AUTH_FILE`` (testes)."""
        path = os.getenv("TRADINGDEGENS_CODEX_AUTH_FILE",
                         os.path.expanduser("~/.local/share/opencode/auth.json"))
        try:
            p = Path(path)
            current: dict = {}
            if p.exists():
                try:
                    loaded = json.loads(p.read_text("utf-8"))
                    current = loaded if isinstance(loaded, dict) else {}
                except Exception:  # noqa: BLE001 — arquivo corrompido: recomeça limpo
                    current = {}
            prev = current.get("openai") if isinstance(current.get("openai"), dict) else None
            current["openai"] = oauth_codex.bridge_record(
                token_resp, now_ms=int(time.time() * 1000), previous=prev)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(p.suffix + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(current, fh, indent=2)
            os.replace(tmp, p)
            os.chmod(p, 0o600)
        except Exception:  # noqa: BLE001 — ponte é best-effort; o store 017 já guardou
            pass

    def _oauth_html(self, message: str, *, ok: bool) -> None:
        """Página de retorno do OAuth (aba nova). ``message`` é sempre gerada pelo
        servidor — nunca reflete input/segredo do usuário."""
        # Sem pictograma (DA-076): a COR e a palavra da mensagem já dizem se deu
        # certo. Esta é a aba de retorno do OAuth, mas é tela do produto igual.
        color = "#22c55e" if ok else "#f59e0b"
        safe = (message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        html = (
            "<!doctype html><html lang=\"pt-br\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>TradingDegens — assinatura</title><style>"
            "body{background:#0d0d0f;color:#e7e7ea;font-family:system-ui,-apple-system,"
            "sans-serif;display:grid;place-items:center;min-height:100vh;margin:0}"
            ".card{max-width:420px;padding:28px 32px;border:1px solid #26262b;"
            "border-radius:12px;background:#151519;text-align:center}"
            ".i{font-size:40px;line-height:1}.m{margin:14px 0 0;font-size:15px;"
            f"line-height:1.5}}.s{{color:{color};font-weight:600}}"
            ".sub{color:#8a8a92;font-size:13px}</style></head><body><div class=\"card\">"
            f"<p class=\"m s\">{safe}</p>"
            "<p class=\"m sub\">Esta janela é só o retorno do login.</p></div></body></html>"
        )
        self._send_bytes(html.encode("utf-8"), "text/html; charset=utf-8", 200)

    def _subscription_state(self) -> dict:
        """Estado da assinatura por provedor (task 020): funde o REGISTRO DO APP
        (store 0600 da 017) com a DETECÇÃO read-only do login do CLI da box
        (:mod:`server_login`). ``connected`` = app OU servidor; ``source`` diz qual.
        NUNCA traz token — só booleanos, o rótulo do provedor e o "quando"."""
        providers: dict[str, dict] = {}
        for key in oauth_providers.PROVIDER_ORDER:
            prov = oauth_providers.PROVIDERS[key]
            app = self.subscription.status(provider=key)
            det = server_login.detect(key)
            app_on = bool(app.get("connected"))
            det_on = bool(det.get("connected"))
            source = "app" if app_on else ("server" if det_on else None)
            providers[key] = {
                "provider": key,
                "label": prov.label,
                "cta": prov.cta,
                "connected": app_on or det_on,
                "source": source,
                "connected_at": app.get("connected_at"),
                "detected_at": det.get("detected_at"),
            }
        return providers

    # -- routing --------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                self._serve_static("index.html")
            elif path.startswith("/static/"):
                self._serve_static(path[len("/static/"):])
            elif path == "/api/health":
                # Expõe as runs ATIVAS pra um deploy ser gracioso: quem for reiniciar
                # o serviço vê que há análise em voo e drena (o handler de SIGTERM
                # espera esvaziar) — nunca mata cega no meio. Os ids deixam auditar.
                active = self.runner.active_run_ids()
                self._send_json({
                    "ok": True, "service": "tradingdegens-web",
                    "active_runs": len(active), "runs": active,
                })
            elif path == "/api/config":
                info = self.runner.config_info()
                # Estado do login do dono (server-side): a UI mostra "usando a chave
                # do servidor" só quando é o dono; nunca envia a chave em si.
                info["owner"] = self._is_owner()
                info["owner_login_enabled"] = self.auth.enabled()
                self._send_json(info)
            elif path.startswith("/api/status/") or path.startswith("/api/run/"):
                run_id = path.rsplit("/", 1)[-1]
                snap = self.runner.status(run_id)
                if snap is None:
                    self._send_json({"error": "execução desconhecida"}, 404)
                else:
                    self._send_json(snap)
            elif path == "/api/runs":
                # Only the running set is exposed here (the finished runs live in
                # /api/history); a status filter other than "running" is ignored.
                self._send_json({"runs": self.runner.active_runs()})
            elif path == "/api/history":
                self._send_json({"runs": self.runner.history()})
            elif path == "/api/search":
                # Autocomplete do campo ATIVO: termo (nome OU sigla) -> símbolos
                # candidatos com o nome. Keyless (Yahoo search via yfinance), fail-open.
                qs = parse_qs(urlparse(self.path).query)
                term = (qs.get("q", [""])[0] or "").strip()
                self._send_json({"query": term, "results": self.runner.search_symbols(term)})
            elif path == "/api/watchlist":
                # Watchlist MANUAL do scan de portfólio. LEITURA pública (dados de
                # mercado não são secretos — mesmo regime do /api/chart); a edição
                # (POST) é owner-only.
                self._send_json({"tickers": self.runner.watchlist_get()})
            elif path == "/api/scan":
                # SCAN ESTRUTURAL 1-2-3 da watchlist (1d+4h+1h): $0 de LLM — só o plano
                # determinístico cacheado. Público como /api/chart. Síncrono e
                # PARALELO (:data:`scanner._SCAN_WORKERS`): ~8s com a cotação fria,
                # ~6,5s quente, na watchlist real de 20 ativos (mediana de N=4,
                # medido 29/08 — a tabela inteira vive em ``scan_watchlist``).
                # O comentário anterior dizia "~13s/10 ativos, cacheado ~2s": a
                # watchlist tem 20 e a medida pela HTTP dava 25–75s.
                qs = parse_qs(urlparse(self.path).query)
                date = (qs.get("date", [""])[0] or "").strip() or timeutil.today()
                self._send_json(self.runner.scan_portfolio(date))
            elif path == "/api/execucao":
                # CARD DE EXECUÇÃO: o que fazer com os níveis do ativo aberto —
                # veredito de oportunidade, ordens na sequência de digitar, saída
                # fracionada, proteção (BE/trailing, opt-in) e o índice de
                # confiabilidade com o gate de N. Público como o /api/chart: são
                # níveis derivados de série, $0 de LLM.
                qs = parse_qs(urlparse(self.path).query)
                ticker = (qs.get("ticker", [""])[0] or "").strip()
                date = (qs.get("date", [""])[0] or "").strip()
                tf = (qs.get("tf", ["1d"])[0] or "1d").strip()
                method = (qs.get("method", ["padrao"])[0] or "padrao").strip().lower()
                if not ticker:
                    self._send_json({"error": "informe um ticker"}, 400)
                else:
                    try:
                        self._send_json(
                            self.runner.execution_card(ticker, date, tf, method))
                    except ValueError as exc:
                        self._send_json({"error": str(exc)}, 400)
            elif path == "/api/agenda/proxima":
                # QUANDO o próximo candle do frame fecha. A tela agenda a
                # revalidação automática por aqui em vez de recalcular o horário em
                # JavaScript: a regra é a do :mod:`agenda`, a MESMA que a passada
                # agendada do scan usa, e dois relógios divergiriam. $0, sem varrer.
                qs = parse_qs(urlparse(self.path).query)
                self._send_json(self.runner.agenda_proxima(
                    (qs.get("tf", ["1d"])[0] or "1d"),
                    (qs.get("ticker", [""])[0] or ""),
                    (qs.get("asset_type", [""])[0] or "")))
            elif path == "/api/erick/carteira":
                # SÓ-DONO, SEM ALTERNATIVA DE BYOK (DA-148). Todo o resto do produto
                # lê fonte pública; isto é conteúdo de assinatura paga de terceiro —
                # a tela de login do site diz "acesso exclusivo para alunos". Trazer
                # chave própria de LLM não compra assinatura de outra pessoa, então
                # o `_gate_or_403` (portão de CUSTO) não serve aqui: é portão de
                # AUTORIZAÇÃO, e o único que serve é o de dono.
                if not self._owner_or_403():
                    return
                dados = self.runner.erick_carteira()
                if dados is None:
                    # Instância sem a credencial: a feature não existe aqui. 404 e
                    # não 500 — não é erro, é ausência; e a mensagem não diz nada
                    # sobre o endereço nem sobre quem é o assinante.
                    self._send_json({"error": "carteira não configurada nesta instância",
                                     "error_code": "sem_carteira"}, 404)
                    return
                self._send_json(dados)
            elif path == "/api/agenda/scan":
                # QUANDO a próxima passada da agenda do scan acontece — e quando a
                # tela deve RELER o resultado dela. A faixa de frames do card
                # (DA-133) se agenda por aqui em vez de contar 60 minutos em
                # JavaScript: a cadência é a de :mod:`agenda`, a mesma que a passada
                # usa, e dois relógios divergiriam sem que ninguém soubesse qual
                # manda. $0, sem varrer — é aritmética de calendário.
                self._send_json(self.runner.agenda_do_scan())
            elif path == "/api/scan/salvo":
                # O ÚLTIMO scan COMPLETO já salvo em disco — leitura de arquivo, $0 e
                # instantânea. É o que o painel pinta na ABERTURA, com o carimbo de
                # quando foi tirado, enquanto a varredura nova roda por baixo; sem
                # ele a tela ficava vazia os 8–20s da varredura. Público como o
                # /api/scan (é o mesmo dado, só que de antes).
                self._send_json(self.runner.scan_ultimo())
            elif path == "/api/scan/verdicts":
                # TRACK RECORD do scan: cada gatilho flagrado é re-avaliado contra o
                # preço de hoje (bateu TP / bateu SL / andamento) + taxa de acerto.
                qs = parse_qs(urlparse(self.path).query)
                date = (qs.get("date", [""])[0] or "").strip() or timeutil.today()
                # BANCA do PnL de paper (DA-154) — configurável, 100 é só o chão. Um
                # valor ilegível/negativo não derruba a rota: cai no padrão dentro de
                # scan_verdicts, a mesma disciplina que o resto do endpoint já segue.
                banca_raw = (qs.get("banca", [""])[0] or "").strip()
                try:
                    banca = float(banca_raw) if banca_raw else None
                except ValueError:
                    banca = None
                self._send_json(self.runner.scan_track_record(date, banca=banca))
            elif path == "/api/subscription/status":
                # Status da assinatura do dono (task 017; multi-provedor 020): SÓ-DONO.
                # Público → 403. Devolve só metadados (conectada?/quando/fonte por
                # provedor), NUNCA o token. Mantém os campos "planos" do openai pra
                # compat com a 017/019; a UI nova lê ``providers``.
                if not self._owner_or_403():
                    return
                providers = self._subscription_state()
                oa = providers["openai"]
                info = {
                    "owner": True,
                    "connected": oa["connected"],
                    "kind": "openai" if oa["connected"] else None,
                    "connected_at": oa["connected_at"],
                    "source": oa["source"],
                    "providers": providers,
                }
                self._send_json(info)
            elif path == "/api/subscription/oauth/callback":
                # Retorno do OAuth (task 019): o issuer redireciona o navegador pra cá
                # com ?code&state. NÃO owner-gated (o redirect vem cross-site, sem
                # cookie) — o nonce ``state`` (uso único, verifier server-side) é a
                # proteção. Fecha o token de ponta a ponta quando o redirect chega aqui.
                self._handle_oauth_callback()
            elif path == "/api/prices":
                # Preço LIVE (3ª linha da watchlist): lote de tickers -> preço atual
                # + variação do dia. Leve (fast_info, sem pipeline), cacheado ~45s;
                # fonte caída por ticker -> null (a UI mostra "—"). Teto de sanidade.
                qs = parse_qs(urlparse(self.path).query)
                raw = (qs.get("tickers", [""])[0] or "").strip()
                tickers = [t for t in (x.strip() for x in raw.split(",")) if t][:50]
                precos = self.runner.live_prices(tickers)
                # A VIGILÂNCIA VIAJA JUNTO (DA-138): a cotação já foi buscada aqui,
                # e comparar contra níveis já calculados não custa chamada nenhuma.
                # Pedi-la num endpoint próprio dobraria o número de requisições da
                # tela pra não descobrir nada de novo.
                self._send_json({"prices": precos,
                                 "vigilancia": self.runner.vigilancia_de_nivel(
                                     tickers, precos)})
            elif path == "/api/names":
                # Resolução em lote símbolo -> nome pros chips do histórico/cabeçalho.
                qs = parse_qs(urlparse(self.path).query)
                raw = (qs.get("symbols", [""])[0] or "").strip()
                symbols = [s for s in (x.strip() for x in raw.split(",")) if s]
                self._send_json({"names": self.runner.resolve_names(symbols)})
            elif path == "/api/chart":
                qs = parse_qs(urlparse(self.path).query)
                ticker = (qs.get("ticker", [""])[0] or "").strip()
                date = (qs.get("date", [""])[0] or "").strip()
                tf = (qs.get("tf", ["1d"])[0] or "1d").strip()
                # método da análise aberta (task 031): mantém a estrutura por método
                # (Erick EMA 8/21 / Padrão MMS) ao trocar de timeframe.
                method = (qs.get("method", ["padrao"])[0] or "padrao").strip().lower()
                if not ticker:
                    self._send_json({"error": "informe um ticker"}, 400)
                else:
                    try:
                        self._send_json(self.runner.timeframe_view(ticker, date, tf, method))
                    except ValueError as exc:
                        self._send_json({"error": str(exc)}, 400)
            else:
                self._send_json({"error": "não encontrado"}, 404)
        except Exception as exc:  # never leak a stack to the socket as HTML
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/login":
                # Login do dono: senha (env TRADINGDEGENS_OWNER_TOKEN) verificada
                # server-side em tempo-constante; cria sessão e devolve cookie
                # HttpOnly. Nunca ecoa a senha nem a chave do servidor.
                body = self._read_json_body()
                if not self.auth.enabled():
                    self._send_json({"ok": False, "error": "login do dono não configurado"}, 400)
                    return
                if not self.auth.verify_password(body.get("password")):
                    self._send_json({"ok": False, "error": "senha incorreta"}, 401)
                    return
                sid = self.auth.create_session()
                # HttpOnly: JS não lê (anti-XSS). SameSite=Strict: não vaza cross-site.
                cookie = (f"{self.auth.cookie_name}={sid}; HttpOnly; SameSite=Strict; "
                          f"Path=/; Max-Age=2592000")
                self._send_json({"ok": True, "owner": True}, cookies=[cookie])
                return
            if path == "/api/logout":
                self.auth.destroy(self._session_id())
                cleared = (f"{self.auth.cookie_name}=; HttpOnly; SameSite=Strict; "
                           f"Path=/; Max-Age=0")
                self._send_json({"ok": True, "owner": False}, cookies=[cleared])
                return
            if path == "/api/watchlist":
                # EDIÇÃO da watchlist do scan: SÓ-DONO (público lê via GET; só quem
                # é dono cura a lista). Ações: add | remove | set.
                if not self._owner_or_403():
                    return
                body = self._read_json_body()
                action = (body.get("action") or "").strip().lower()
                ticker = (body.get("ticker") or "").strip()
                if action == "add" and ticker:
                    tickers = self.runner.watchlist_add(ticker)
                elif action == "remove" and ticker:
                    tickers = self.runner.watchlist_remove(ticker)
                elif action == "set" and isinstance(body.get("tickers"), list):
                    tickers = self.runner.watchlist_set(body.get("tickers"))
                else:
                    self._send_json({"error": "ação inválida (use add/remove/set)"}, 400)
                    return
                self._send_json({"ok": True, "tickers": tickers})
                return
            if path == "/api/subscription/oauth/start":
                # Conectar via LINK (task 019): SÓ-DONO. Gera PKCE, guarda o verifier
                # server-side (state→verifier, em memória) e devolve a URL de
                # autorização do ChatGPT/OpenAI — a MESMA do ``codex login``. O segredo
                # (verifier) NUNCA vai ao cliente; a URL é pública por natureza. O front
                # a abre em nova aba (ação principal = ABRIR O LINK, não colar token).
                if not self._owner_or_403():
                    return
                provider = (self._read_json_body().get("provider") or "openai").strip().lower()
                try:
                    prov = oauth_providers.get(provider)
                except ValueError:
                    self._send_json({"ok": False, "error": "provedor desconhecido"}, 400)
                    return
                verifier = oauth_providers.new_verifier()
                state = oauth_providers.new_state()
                redirect_uri = self._oauth_redirect_uri(prov.key)
                url = prov.build_authorize_url(
                    state=state, code_challenge=oauth_providers.challenge_for(verifier),
                    redirect_uri=redirect_uri)
                self.oauth_flows.create(state, verifier, prov.key)
                self._send_json({"ok": True, "owner": True, "provider": prov.key,
                                 "authorize_url": url, "state": state,
                                 "redirect_uri": redirect_uri})
                return
            if path == "/api/subscription/connect":
                # Conectar a assinatura do dono (task 017): SÓ-DONO — valida a sessão
                # ANTES de tocar no token. O token vem no HEADER X-Subscription-Token
                # (nunca querystring/corpo-logado); é gravado server-side e NUNCA volta
                # ao cliente. log_message é no-op → não vaza no journal.
                if not self._owner_or_403():
                    return
                token = (self.headers.get("X-Subscription-Token") or "").strip()
                if not token:
                    self._send_json({"ok": False, "error": "token da assinatura ausente "
                                     "(envie no header X-Subscription-Token)"}, 400)
                    return
                body = self._read_json_body()
                # ``provider`` roteia o arquivo 0600; ``kind`` fica como rótulo (compat).
                provider = (body.get("provider") or body.get("kind") or "openai").strip().lower()
                info = self.subscription.connect(token, provider=provider,
                                                 kind=body.get("kind"),
                                                 connected_at=timeutil.stamp())
                info["ok"] = True
                info["owner"] = True
                info["provider"] = provider
                self._send_json(info)   # status só (sem o token)
                return
            if path == "/api/subscription/disconnect":
                # Desconectar (task 017; multi-provedor 020): remove SÓ o registro do
                # APP do provedor. NUNCA toca nas creds reais do CLI da box (guardrail).
                if not self._owner_or_403():
                    return
                provider = (self._read_json_body().get("provider") or "openai").strip().lower()
                info = self.subscription.disconnect(provider=provider)
                info["ok"] = True
                info["owner"] = True
                info["provider"] = provider
                self._send_json(info)
                return
            if path == "/api/analyze":
                body = self._read_json_body()
                ticker = (body.get("ticker") or "").strip()
                date = (body.get("date") or "").strip()
                # "método Erick" sob demanda: default Padrão. Aceita method=="erick"
                # ou o atalho legível erick==true.
                method = (body.get("method") or "").strip().lower()
                if not method and body.get("erick"):
                    method = "erick"
                # Reference timeframe for the verdict (task 012); default daily. An
                # invalid frame for the asset is a ValueError -> 400 below.
                timeframe = (body.get("timeframe") or "1d").strip() or "1d"
                if not ticker:
                    self._send_json({"error": "informe um ticker"}, 400)
                    return
                # Gating: público sem chave própria e sem sessão do dono não roda —
                # nunca cai na chave do servidor (responde 403 claro, sem criar run).
                # EXCEÇÃO setup123 (o atalho 1-2-3): $0 de LLM, nenhum modelo roda —
                # o gate protegeria um custo que não existe. Público pode escanear
                # estrutura do mesmo jeito que pode ver o /api/chart.
                #
                # A isenção vale SÓ quando o atalho é mesmo a rota que vai subir. O
                # `compare` é decidido DEPOIS daqui e roda Padrão + Erick + meta-juiz
                # na chave do servidor: `{"method":"setup123","compare":true}` passava
                # pela isenção e queimava crédito do dono anonimamente. A regra passa a
                # ser a rota REAL, não só o rótulo do método.
                if not self._rota_sem_llm(method, body) and not self._gate_or_403(body):
                    return
                # BYOK: a chave/provider/modelo do usuário viajam por header+corpo e
                # valem só pra ESTA run (chave do usuário > env do servidor).
                overrides = self._llm_overrides(body)
                # "comparar Padrão × Erick" (Fase 3): roda as duas leituras + meta-juiz.
                if body.get("compare"):
                    run_id = self.runner.start_compare(
                        ticker, date, timeframe=timeframe, overrides=overrides
                    )
                    self._send_json({"run_id": run_id, "run_token": _run_token(run_id)})
                    return
                # Reúso automático de análise idêntica já feita (DA-058); o front
                # pode forçar do zero com force_fresh (ex.: "reanalisar").
                reuse = not bool(body.get("force_fresh"))
                run_id = self.runner.start(
                    ticker, date, method=method or "padrao", timeframe=timeframe,
                    overrides=overrides, reuse=reuse,
                )
                # ``run_token``: capacidade de PARAR/PAUSAR esta run (o run_id é
                # público e não prova autoria) — o front guarda e devolve no header.
                self._send_json({"run_id": run_id, "run_token": _run_token(run_id)})
            elif path.startswith("/api/run/") and path.endswith("/cancel"):
                # PARAR/PAUSAR a run em andamento (task 026). Portão de AUTORIA, não de
                # custo: dono logado OU o token daquela run (task 007). O "quem pode
                # iniciar pode interromper" de antes partia de premissa falsa — o
                # _gate_or_403 aceita qualquer X-LLM-Key sem validar, então qualquer
                # anônimo com um header lixo e um run_id de /api/runs derrubava análise
                # alheia. Corpo {"pause": true} = PAUSAR (retomável, mantém checkpoint
                # da 022); default PARAR. Cooperativo — active_runs cai a 0 em segundos.
                run_id = path[len("/api/run/"):-len("/cancel")]
                if not self._controla_a_run_or_403(run_id):
                    return
                body = self._read_json_body()
                res = self.runner.cancel(run_id, keep_resume=bool(body.get("pause")))
                if res is None:
                    self._send_json({"error": "execução desconhecida ou já encerrada"}, 404)
                else:
                    self._send_json({"ok": True, **res})
            elif path.startswith("/api/run/") and path.endswith("/resume"):
                # RETOMAR uma run PAUSADA (task 026): continua do checkpoint da 022
                # (reaproveita as etapas concluídas), não recomeça do zero. OWNER-ONLY
                # pela mesma razão do escalar/refresh-step (task 007): o resume ignora
                # os overrides da requisição e re-enfileira pelo DESCRITOR da run, que
                # carrega allow_server_key=True — ou seja, roda NA CREDENCIAL DO
                # SERVIDOR. E só run resumível (dono/servidor) tem descritor: run BYOK
                # nunca é retomável, então o público não perde nada aqui.
                if not self._owner_or_403():
                    return
                run_id = path[len("/api/run/"):-len("/resume")]
                res = self.runner.resume(run_id)
                if res is None:
                    self._send_json({"error": "nada pra retomar (run não resumível ou desconhecida)"}, 404)
                else:
                    self._send_json({"ok": True, **res})
            elif path.startswith("/api/run/") and path.endswith("/escalate"):
                # ESCALAR uma etapa que falhou com OUTRO LLM (task 027 parte B): re-roda
                # SÓ ela reaproveitando o checkpoint (022). Owner-only — a escalação roda
                # pela credencial do servidor; run BYOK não é retomável (indisponível
                # honesto no runner). Corpo: {level: quick|deep, provider, model}.
                if not self._owner_or_403():
                    return
                body = self._read_json_body()
                run_id = path[len("/api/run/"):-len("/escalate")]
                res = self.runner.escalate(
                    run_id,
                    (body.get("level") or "").strip().lower(),
                    provider=(body.get("provider") or "").strip().lower(),
                    model=(body.get("model") or "").strip(),
                )
                if res is None:
                    self._send_json({"error": "execução desconhecida ou sem checkpoint"}, 404)
                elif not res.get("ok"):
                    self._send_json({"error": res.get("error"), "error_code": res.get("code")}, 409)
                else:
                    self._send_json(res)
            elif path.startswith("/api/run/") and path.endswith("/refresh-step"):
                # ATUALIZAR uma etapa concluída com DADO FRESCO (task 002 / DA-062):
                # invalida o cache de preço do ativo e rebobina o checkpoint (022) pra
                # antes daquela etapa, que re-roda com número novo — as anteriores
                # seguem vindo prontas. Owner-only, como o escalar: roda pela credencial
                # do servidor e run BYOK não é retomável (indisponível honesto no
                # runner). Corpo: {"node": "<nó do pipeline>"}.
                if not self._owner_or_403():
                    return
                body = self._read_json_body()
                run_id = path[len("/api/run/"):-len("/refresh-step")]
                res = self.runner.refresh_step(run_id, (body.get("node") or "").strip())
                if res is None:
                    self._send_json({"error": "execução desconhecida ou sem checkpoint"}, 404)
                elif not res.get("ok"):
                    self._send_json({"error": res.get("error"),
                                     "error_code": res.get("code")}, 409)
                else:
                    self._send_json(res)
            elif path == "/api/compare":
                # Manual confront (task 018): meta-judge over two EXISTING runs of
                # the same ticker (no pipeline re-run). Returns a ready snapshot.
                body = self._read_json_body()
                if not self._gate_or_403(body):
                    return
                a = (body.get("a") or "").strip()
                b = (body.get("b") or "").strip()
                self._send_json(self.runner.confront(a, b, overrides=self._llm_overrides(body)))
            elif path == "/api/ask":
                # Q&A ancorado (task 027): pergunta em linguagem natural sobre uma
                # run JÁ computada; responde com os NÍVEIS reais dela (price_structure)
                # via modelo barato, sem re-rodar a análise nem buscar dado externo.
                body = self._read_json_body()
                run_id = (body.get("run_id") or "").strip()
                question = (body.get("question") or "").strip()
                if not run_id:
                    self._send_json({"error": "informe o run_id"}, 400)
                    return
                answer = self.runner.ask(run_id, question, overrides=self._llm_overrides(body))
                if answer is None:
                    self._send_json({"error": "execução desconhecida"}, 404)
                else:
                    self._send_json(answer)
            elif path == "/api/test-key":
                # BYOK: valida a chave/config efetiva com UMA chamada barata, sem
                # rodar análise. A chave vem no header X-LLM-Key; a resposta diz só
                # ok/erro (erro já redigido da chave), nunca ecoa a chave.
                body = self._read_json_body()
                self._send_json(self.runner.test_key(self._llm_overrides(body)))
            elif path == "/api/test-model":
                # BYOK: pinga o modelo RÁPIDO e o PESADO escolhidos com um prompt
                # trivial e devolve a latência de cada — confirma que o modelo
                # responde SEM rodar a análise de 12min. Chave no header X-LLM-Key
                # (nunca querystring/log); só ok/latency/sample/erro voltam, jamais
                # a chave. Erro → mensagem humana (reusa o mapa da 041).
                body = self._read_json_body()
                self._send_json(self.runner.test_model(self._llm_overrides(body)))
            elif path == "/api/models":
                # Proxy: lista os modelos que a chave/provider dá acesso, pra popular
                # os dropdowns do BYOK. Chave no header X-LLM-Key (nunca querystring/
                # log); dono logado sem chave própria usa a env do servidor (só os
                # NOMES voltam, nunca a chave). Falha → mensagem humana + fallback.
                self._send_json(self._list_models(self._read_json_body()))
            else:
                self._send_json({"error": "não encontrado"}, 404)
        except ValueError as exc:
            self._send_json({"error": self._redact_key(str(exc))}, 400)
        except Exception as exc:
            self._send_json({"error": self._redact_key(f"{type(exc).__name__}: {exc}")}, 500)

    def do_DELETE(self) -> None:  # noqa: N802
        # O histórico é PÚBLICO pra LEITURA de propósito (track record — avaliar
        # acertos/erros das análises). Mas APAGAR é só do DONO: sem este portão,
        # qualquer visitante poderia destruir o histórico inteiro de um ativo. Gate no
        # TOPO → toda rota DELETE é owner-only por padrão (a leitura pública em
        # /api/history e /api/runs segue intacta, são do_GET). Público → 403 owner_only.
        if not self._owner_or_403():
            return
        # Remover um ATIVO da lista lateral (watchlist): apaga do histórico todas
        # as análises salvas daquele ticker. A lista é por ativo, então o × remove
        # o ativo inteiro. Idempotente (removed=0 se já não existe).
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/history/"):
                ticker = unquote(path[len("/api/history/"):]).strip()
                if not ticker:
                    self._send_json({"error": "informe um ticker"}, 400)
                    return
                removed = self.runner.delete_ticker(ticker)
                self._send_json({"ok": True, "ticker": ticker.upper(), "removed": removed})
            else:
                self._send_json({"error": "não encontrado"}, 404)
        except Exception as exc:  # never leak a stack to the socket as HTML
            self._send_json({"error": self._redact_key(f"{type(exc).__name__}: {exc}")}, 500)

    def log_message(self, fmt, *args) -> None:  # keep the journal readable
        pass


def make_server(host: str, port: int, runner: AnalysisRunner | None = None,
                auth: OwnerAuth | None = None,
                subscription: SubscriptionStore | None = None) -> ThreadingHTTPServer:
    runner = runner or AnalysisRunner()
    # OwnerAuth lê a senha do dono da env (TRADINGDEGENS_OWNER_TOKEN) uma vez, na
    # subida; sem senha configurada, o login fica desabilitado e todos são público.
    auth = auth or OwnerAuth()
    # Credencial da assinatura do dono (task 017): arquivo 0600 no dir de dados do
    # runtime (env TRADINGDEGENS_SUBSCRIPTION_FILE sobrepõe). NUNCA no repo.
    if subscription is None:
        sub_path = os.getenv("TRADINGDEGENS_SUBSCRIPTION_FILE") or str(
            Path(getattr(runner.store, "base", ".")) / "subscription.json")
        subscription = SubscriptionStore(sub_path)
    # Fluxos OAuth em andamento (task 019): state→verifier em memória, uso único +
    # TTL. Compartilhado no handler (não por-requisição) pra o /start e o /callback
    # verem o mesmo mapa.
    oauth_flows = oauth_providers.PendingOAuth()
    handler = type("BoundHandler", (_Handler,),
                   {"runner": runner, "auth": auth, "subscription": subscription,
                    "oauth_flows": oauth_flows})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


# Valores que DESLIGAM uma chave de ambiente. Um lugar só: "0" ligando a agenda porque
# alguém escreveu "false" seria a pior forma de descobrir a diferença.
_DESLIGADO = ("0", "false", "nao", "não", "no", "off")


def _graceful_shutdown(httpd: ThreadingHTTPServer, runner: AnalysisRunner,
                       agendador=None) -> None:
    """Instala o handler de SIGTERM/SIGINT pra um deploy NÃO matar run no meio.

    No stop (``systemctl restart`` manda SIGTERM), em vez de o processo morrer
    cru — matando as threads das runs em voo — a gente DRENA: para de aceitar e
    espera até ``TRADINGDEGENS_DRAIN_SECONDS`` as runs ativas terminarem. O que
    não fechar a tempo não se perde: o descritor em disco + o checkpoint por-nó
    fazem o próximo boot RETOMAR do último estágio. ``httpd.shutdown()`` roda em
    outra thread (não pode ser chamado de dentro do serve_forever/handler)."""
    stopping = threading.Event()

    def _handler(signum, _frame):
        if stopping.is_set():
            return
        stopping.set()
        drain = float(os.getenv("TRADINGDEGENS_DRAIN_SECONDS", "8"))
        deadline = time.time() + max(0.0, drain)
        # Deixa as runs quase-prontas fecharem; as demais serão retomadas no boot.
        # A agenda para PRIMEIRO: uma passada nova começando durante o dreno só
        # atrasaria o restart e escreveria no ledger de um processo que já está saindo.
        if agendador is not None:
            agendador.stop()
        while time.time() < deadline and runner.active_run_ids():
            time.sleep(0.3)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def main() -> None:
    host = os.getenv("TRADINGDEGENS_WEB_HOST", "0.0.0.0")
    port = int(os.getenv("TRADINGDEGENS_WEB_PORT", "8781"))
    # DEPLOY DO FRONT: o restart do serviço — que já é o passo explícito de deploy em
    # toda task — publica o front da REVISÃO COMMITADA. Fica aqui, no entrypoint, e
    # não no import do módulo: a suíte importa ``server`` centenas de vezes e publicar
    # ali faria rodar `git archive` em cada uma (e, pior, uma rodada de testes
    # "deployaria" sem ninguém pedir).
    #
    # Front-only, sem restart: `python -m tradingagents.webui.static_publish` — o
    # servidor lê o diretório publicado a cada requisição, então a nova publicação
    # aparece na hora, com o cache-buster novo.
    global _STATIC_DIR
    try:
        info = static_publish.publicar()
        _STATIC_DIR = static_publish.publicado_dir()
        print(f"Front publicado: {info['revisao'][:12]} → {info['destino']}", flush=True)
    except Exception as exc:  # noqa: BLE001 — publicação nunca impede a subida
        print(f"Aviso: não deu pra publicar o front ({exc}); servindo o que já estava "
              f"em {_STATIC_DIR}", flush=True)
    # Constrói o runner primeiro pra RETOMAR as runs que um restart anterior matou
    # no meio (fila de descritores em disco) ANTES de aceitar tráfego.
    runner = AnalysisRunner()
    try:
        resumed = runner.resume_interrupted()
        if resumed:
            print(f"Retomando {resumed} análise(s) interrompida(s) por restart.",
                  flush=True)
    except Exception as exc:  # noqa: BLE001 — retomada nunca impede a subida
        print(f"Aviso: falha ao retomar runs interrompidas: {exc}", flush=True)
    # AGENDA DO SCAN — o track record para de depender de alguém abrir a tela.
    #
    # Uma passada por CANDLE FECHADO do frame mais rápido que o scan lê (ver
    # :mod:`tradingagents.webui.agenda`: a cadência sai da informação, não do limite da
    # fonte). Vive DENTRO deste processo porque o ledger tem um dono só — o ``ScanLog``
    # do runner, com o seu lock e a de-duplicação que lê o arquivo antes de gravar.
    #
    # Sobe no ENTRYPOINT, nunca no ``make_server``: a suíte levanta servidor em dezenas
    # de testes, e nenhum deles pediu uma varredura de rede em segundo plano.
    agendador = None
    if os.getenv("TRADINGDEGENS_SCAN_AGENDA", "1").strip().lower() not in _DESLIGADO:
        agendador = agenda.AgendaScan(runner.scan_agendado)
        agendador.start()
        print(f"Agenda do scan: uma passada a cada "
              f"{agenda.cadencia_minutos()} min (+{agenda.ATRASO_POS_FECHAMENTO_S}s "
              f"após o fechamento do candle).", flush=True)
    else:
        print("Agenda do scan DESLIGADA (TRADINGDEGENS_SCAN_AGENDA).", flush=True)
    httpd = make_server(host, port, runner=runner)
    _graceful_shutdown(httpd, runner, agendador)
    shown = host if host != "0.0.0.0" else "0.0.0.0 (todas as interfaces — Tailscale incluído)"
    print(f"TradingDegens web em http://{shown}:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()

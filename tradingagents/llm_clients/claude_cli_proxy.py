"""Proxy local: assinatura Claude (CLI OAuth) → API Anthropic, custo por token = $0.

Motivação (task 20260826-030): rodar as chamadas LLM da análise pela ASSINATURA
Claude (Max) via o token OAuth do CLI — sem API key paga. O token OAuth do CLI
(``~/.claude/.credentials.json`` → ``claudeAiOauth.accessToken``, escopo
``user:inference``) autentica a inferência quando enviado como ``Authorization:
Bearer`` + o header ``anthropic-beta: oauth-2025-04-20`` — NÃO como ``x-api-key``.

O SDK Anthropic (e portanto ``langchain_anthropic.ChatAnthropic``) sempre manda
``x-api-key``, que a rota de assinatura rejeita (401). Este proxy resolve isso e,
de quebra, DESACOPLA o token do processo da análise:

* o app da análise fala Anthropic normal contra ``http://127.0.0.1:<porta>`` com
  uma api_key DUMMY (``ClaudeCliClient`` no factory usa ``base_url`` = este proxy);
* o proxy DESCARTA ``x-api-key``/``authorization`` de entrada e injeta o Bearer da
  assinatura + o beta OAuth, encaminhando pra ``https://api.anthropic.com``;
* o token OAuth vive SÓ aqui (lido do arquivo em modo LEITURA a cada requisição —
  nunca escreve/renova, então não interfere na auth do mainbot; o
  ``auto-renew-claude.service`` cuida do refresh e este proxy relê o token fresco).

Só escuta em 127.0.0.1 (nunca exposto). Sem dependências externas (stdlib).

Uso:
    python -m tradingagents.llm_clients.claude_cli_proxy [--host 127.0.0.1] [--port 8791]

Env:
    CLAUDE_CLI_CREDENTIALS  caminho do credentials.json (default ~/.claude/.credentials.json)
    CLAUDE_CLI_PROXY_HOST   host de escuta (default 127.0.0.1)
    CLAUDE_CLI_PROXY_PORT   porta de escuta (default 8791)
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM_HOST = "api.anthropic.com"
OAUTH_BETA = "oauth-2025-04-20"
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"

# Headers que NUNCA repassamos ao upstream (auth do cliente e hop-by-hop). A auth
# real é injetada por nós; o resto o http.client recalcula (host/length).
_DROP_REQUEST_HEADERS = {
    "authorization", "x-api-key", "host", "content-length",
    "connection", "proxy-connection", "keep-alive", "accept-encoding",
}
# Hop-by-hop da RESPOSTA que não repassamos (deixamos o nosso server recomputar o
# enquadramento — Content-Length/chunked — pra streaming SSE funcionar).
_DROP_RESPONSE_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
    "content-encoding",
}


def _credentials_path() -> str:
    return os.environ.get(
        "CLAUDE_CLI_CREDENTIALS", os.path.expanduser("~/.claude/.credentials.json")
    )


def proxy_base_url() -> str:
    """URL do proxy que o provider ``claude-cli`` usa como ``base_url`` do Anthropic.

    Configurável por ``CLAUDE_CLI_PROXY_URL`` (default ``http://127.0.0.1:8791``).
    O SDK Anthropic anexa ``/v1/messages`` a isto.
    """
    return os.environ.get("CLAUDE_CLI_PROXY_URL", "http://127.0.0.1:8791")


def load_oauth_token() -> tuple[str | None, str | None]:
    """Lê o token OAuth da assinatura (LEITURA APENAS). Retorna ``(token, erro)``.

    Fresco a cada chamada: o ``auto-renew-claude.service`` reescreve o arquivo ao
    renovar, então relemos e pegamos sempre o token válido. Nunca escreve.
    """
    path = _credentials_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None, f"credentials não encontrado em {path}"
    except (OSError, ValueError) as exc:
        return None, f"falha ao ler credentials: {type(exc).__name__}"
    oauth = (data or {}).get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        return None, "claudeAiOauth.accessToken ausente (CLI não logado?)"
    exp = oauth.get("expiresAt")
    if isinstance(exp, (int, float)) and exp <= time.time() * 1000:
        # Expirado: reporta o gap honesto — não tenta renovar (isso é do auto-renew).
        return None, "token OAuth expirado (aguarde o auto-renew-claude renovar)"
    return token, None


def _open_upstream(timeout: int = 600):
    """Abre a conexão com o upstream Anthropic. Indireção pra testes poderem
    substituir por um fake sem mexer no ``http.client`` global (que urllib usa)."""
    return http.client.HTTPSConnection(UPSTREAM_HOST, timeout=timeout)


def _merge_beta(existing: str | None) -> str:
    """Garante ``oauth-2025-04-20`` no ``anthropic-beta`` sem perder os do cliente."""
    parts = [p.strip() for p in (existing or "").split(",") if p.strip()]
    if OAUTH_BETA not in parts:
        parts.append(OAUTH_BETA)
    return ", ".join(parts)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "claude-cli-proxy/1.0"

    # silencia o log padrão por requisição (fica ruidoso); erros ainda sobem.
    def log_message(self, *args):  # noqa: D401
        pass

    def _reject(self, code: int, message: str):
        body = json.dumps({
            "type": "error",
            "error": {"type": "proxy_error", "message": message},
        }).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._proxy("GET")

    def do_POST(self):
        self._proxy("POST")

    def _proxy(self, method: str):
        # Health check local (não vai ao upstream): confirma se o token está válido.
        if self.path == "/healthz":
            token, err = load_oauth_token()
            payload = {"ok": token is not None}
            if err:
                payload["error"] = err
            body = json.dumps(payload).encode()
            self.send_response(200 if token else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            return

        token, err = load_oauth_token()
        if not token:
            # Gap honesto: sem token válido, a assinatura não responde. 503 explícito.
            self._reject(503, f"assinatura Claude indisponível — {err}")
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        # Monta os headers do upstream: descarta auth do cliente + hop-by-hop, injeta
        # o Bearer da assinatura e o beta OAuth; preserva o resto (ex.: content-type,
        # betas de prompt-cache que o SDK manda).
        out_headers: dict[str, str] = {}
        client_beta = None
        client_version = None
        for name, value in self.headers.items():
            low = name.lower()
            if low == "anthropic-beta":
                client_beta = value
                continue
            if low == "anthropic-version":
                client_version = value
                continue
            if low in _DROP_REQUEST_HEADERS:
                continue
            out_headers[name] = value
        out_headers["Authorization"] = f"Bearer {token}"
        out_headers["anthropic-beta"] = _merge_beta(client_beta)
        out_headers["anthropic-version"] = client_version or DEFAULT_ANTHROPIC_VERSION

        try:
            conn = _open_upstream()
            conn.request(method, self.path, body=body, headers=out_headers)
            resp = conn.getresponse()
        except Exception as exc:  # noqa: BLE001
            # detalhe completo só no log do servidor; ao cliente, mensagem genérica.
            import traceback
            traceback.print_exc(file=sys.stderr)
            self._reject(502, f"upstream Anthropic inacessível: {type(exc).__name__}")
            return

        # Repassa status + headers e faz STREAMING do corpo (SSE do raciocínio ao
        # vivo funciona: encaminhamos em chunked conforme os bytes chegam).
        self.send_response(resp.status)
        for name, value in resp.getheaders():
            if name.lower() in _DROP_RESPONSE_HEADERS:
                continue
            self.send_header(name, value)
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(f"{len(chunk):X}\r\n".encode())
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            conn.close()


def build_server(host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    host = host or os.environ.get("CLAUDE_CLI_PROXY_HOST", "127.0.0.1")
    port = int(port if port is not None else os.environ.get("CLAUDE_CLI_PROXY_PORT", "8791"))
    return ThreadingHTTPServer((host, port), _Handler)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Proxy local Claude assinatura (CLI OAuth) → Anthropic")
    ap.add_argument("--host", default=os.environ.get("CLAUDE_CLI_PROXY_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("CLAUDE_CLI_PROXY_PORT", "8791")))
    args = ap.parse_args(argv)
    httpd = build_server(args.host, args.port)
    token, err = load_oauth_token()
    status = "token OK" if token else f"SEM TOKEN ({err})"
    print(f"claude-cli-proxy em http://{args.host}:{args.port} → {UPSTREAM_HOST} [{status}]",
          file=sys.stderr, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

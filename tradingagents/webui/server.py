"""Dependency-free HTTP server for the TradingDegens web UI.

Built on the stdlib ``http.server`` (no FastAPI/uvicorn) so it runs under the
existing venv with nothing to install — the right shape for a systemd service
that must come up on boot. Binds ``0.0.0.0`` by default so it answers on the
host's Tailscale IP, not only localhost (the whole point of the deliverable).

Routes:
    GET  /                     -> index.html
    GET  /static/<file>        -> bundled static asset
    GET  /api/health           -> {"ok": true, ...}
    POST /api/analyze          -> {ticker, date} -> {run_id}
    GET  /api/status/<run_id>  -> live run snapshot (progress, cost, result)
    GET  /api/run/<run_id>     -> alias of status (from history if needed)
    GET  /api/history          -> recent run summaries
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from tradingagents.webui.runner import AnalysisRunner

_STATIC_DIR = Path(__file__).parent / "static"
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

    # -- helpers --------------------------------------------------------------
    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
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
        self._send_bytes(path.read_bytes(), ctype)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # -- routing --------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                self._serve_static("index.html")
            elif path.startswith("/static/"):
                self._serve_static(path[len("/static/"):])
            elif path == "/api/health":
                self._send_json({"ok": True, "service": "tradingdegens-web"})
            elif path == "/api/config":
                self._send_json(self.runner.config_info())
            elif path.startswith("/api/status/") or path.startswith("/api/run/"):
                run_id = path.rsplit("/", 1)[-1]
                snap = self.runner.status(run_id)
                if snap is None:
                    self._send_json({"error": "execução desconhecida"}, 404)
                else:
                    self._send_json(snap)
            elif path == "/api/history":
                self._send_json({"runs": self.runner.history()})
            else:
                self._send_json({"error": "não encontrado"}, 404)
        except Exception as exc:  # never leak a stack to the socket as HTML
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/analyze":
                body = self._read_json_body()
                ticker = (body.get("ticker") or "").strip()
                date = (body.get("date") or "").strip()
                if not ticker:
                    self._send_json({"error": "informe um ticker"}, 400)
                    return
                run_id = self.runner.start(ticker, date)
                self._send_json({"run_id": run_id})
            else:
                self._send_json({"error": "não encontrado"}, 404)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
        except Exception as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def log_message(self, fmt, *args) -> None:  # keep the journal readable
        pass


def make_server(host: str, port: int, runner: AnalysisRunner | None = None) -> ThreadingHTTPServer:
    runner = runner or AnalysisRunner()
    handler = type("BoundHandler", (_Handler,), {"runner": runner})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def main() -> None:
    host = os.getenv("TRADINGDEGENS_WEB_HOST", "0.0.0.0")
    port = int(os.getenv("TRADINGDEGENS_WEB_PORT", "8781"))
    httpd = make_server(host, port)
    shown = host if host != "0.0.0.0" else "0.0.0.0 (todas as interfaces — Tailscale incluído)"
    print(f"TradingDegens web em http://{shown}:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()

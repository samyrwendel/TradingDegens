"""Dependency-free HTTP server for the TradingDegens web UI.

Built on the stdlib ``http.server`` (no FastAPI/uvicorn) so it runs under the
existing venv with nothing to install — the right shape for a systemd service
that must come up on boot. Binds ``0.0.0.0`` by default so it answers on the
host's Tailscale IP, not only localhost (the whole point of the deliverable).

Routes:
    GET  /                     -> index.html
    GET  /static/<file>        -> bundled static asset
    GET  /api/health           -> {"ok": true, ...}
    POST /api/analyze          -> {ticker, date[, compare]} -> {run_id}
    POST /api/compare          -> {a, b} -> meta-judge snapshot over two runs
    GET  /api/status/<run_id>  -> live run snapshot (progress, cost, result)
    GET  /api/run/<run_id>     -> alias of status (from history if needed)
    GET  /api/runs?status=running -> live in-process runs (em andamento)
    GET  /api/history          -> recent run summaries (running runs merged in front)
    GET  /api/chart            -> ?ticker=&date=&tf= -> recomputed chart + plan
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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
        # Reescreve o link com o mtime do arquivo — muda quando o arquivo muda.
        if safe == "index.html":
            text = data.decode("utf-8")
            for asset in ("app.js", "style.css"):
                f = _STATIC_DIR / asset
                if f.is_file():
                    text = text.replace(
                        f"/static/{asset}", f"/static/{asset}?v={int(f.stat().st_mtime)}"
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
            elif path == "/api/runs":
                # Only the running set is exposed here (the finished runs live in
                # /api/history); a status filter other than "running" is ignored.
                self._send_json({"runs": self.runner.active_runs()})
            elif path == "/api/history":
                self._send_json({"runs": self.runner.history()})
            elif path == "/api/chart":
                qs = parse_qs(urlparse(self.path).query)
                ticker = (qs.get("ticker", [""])[0] or "").strip()
                date = (qs.get("date", [""])[0] or "").strip()
                tf = (qs.get("tf", ["1d"])[0] or "1d").strip()
                if not ticker:
                    self._send_json({"error": "informe um ticker"}, 400)
                else:
                    try:
                        self._send_json(self.runner.timeframe_view(ticker, date, tf))
                    except ValueError as exc:
                        self._send_json({"error": str(exc)}, 400)
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
                # "comparar Padrão × Erick" (Fase 3): roda as duas leituras + meta-juiz.
                if body.get("compare"):
                    run_id = self.runner.start_compare(ticker, date, timeframe=timeframe)
                    self._send_json({"run_id": run_id})
                    return
                run_id = self.runner.start(
                    ticker, date, method=method or "padrao", timeframe=timeframe
                )
                self._send_json({"run_id": run_id})
            elif path == "/api/compare":
                # Manual confront (task 018): meta-judge over two EXISTING runs of
                # the same ticker (no pipeline re-run). Returns a ready snapshot.
                body = self._read_json_body()
                a = (body.get("a") or "").strip()
                b = (body.get("b") or "").strip()
                self._send_json(self.runner.confront(a, b))
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

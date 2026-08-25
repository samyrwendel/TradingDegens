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
    POST /api/ask              -> {run_id, question} -> grounded Q&A over a run
    POST /api/test-key         -> {} + X-LLM-Key header -> {ok, provider, model}

BYOK (traga sua chave): /analyze, /compare, /ask e /test-key aceitam a chave do
usuário no header ``X-LLM-Key`` (nunca em querystring) e provider/modelo/base_url
no corpo do POST. A chave é usada só em memória pra aquela run e nunca é
persistida/logada; sem chave, cai na env do servidor (ver runner.apply_llm_overrides).
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
        return ov

    def _redact_key(self, text: str) -> str:
        """Redige a chave BYOK (header ``X-LLM-Key``) de um texto de erro antes de
        devolvê-lo — o SDK do provedor pode ecoar a chave numa exceção."""
        key = (self.headers.get("X-LLM-Key") or "").strip()
        if key and text:
            return text.replace(key, "***")
        return text

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
            elif path == "/api/search":
                # Autocomplete do campo ATIVO: termo (nome OU sigla) -> símbolos
                # candidatos com o nome. Keyless (Yahoo search via yfinance), fail-open.
                qs = parse_qs(urlparse(self.path).query)
                term = (qs.get("q", [""])[0] or "").strip()
                self._send_json({"query": term, "results": self.runner.search_symbols(term)})
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
                # BYOK: a chave/provider/modelo do usuário viajam por header+corpo e
                # valem só pra ESTA run (chave do usuário > env do servidor).
                overrides = self._llm_overrides(body)
                # "comparar Padrão × Erick" (Fase 3): roda as duas leituras + meta-juiz.
                if body.get("compare"):
                    run_id = self.runner.start_compare(
                        ticker, date, timeframe=timeframe, overrides=overrides
                    )
                    self._send_json({"run_id": run_id})
                    return
                run_id = self.runner.start(
                    ticker, date, method=method or "padrao", timeframe=timeframe,
                    overrides=overrides,
                )
                self._send_json({"run_id": run_id})
            elif path == "/api/compare":
                # Manual confront (task 018): meta-judge over two EXISTING runs of
                # the same ticker (no pipeline re-run). Returns a ready snapshot.
                body = self._read_json_body()
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
            else:
                self._send_json({"error": "não encontrado"}, 404)
        except ValueError as exc:
            self._send_json({"error": self._redact_key(str(exc))}, 400)
        except Exception as exc:
            self._send_json({"error": self._redact_key(f"{type(exc).__name__}: {exc}")}, 500)

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

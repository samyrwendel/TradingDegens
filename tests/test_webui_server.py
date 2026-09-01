"""HTTP routing over a real socket, driving a fake engine (no LLM calls)."""

import json
import threading
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

import pytest

from tests.test_webui_runner import FINAL_STATE, _blocking_factory, _factory, _FakeGraph
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.server import make_server
from tradingagents.webui.store import HistoryStore


def _dual_factory():
    """Padrão → Buy; Erick (analyst present) → Hold WITH an erick_report, so the
    two sides are a real Padrão × Erick pair the manual confront can meta-judge
    directly (detect_method needs the erick_report to tell them apart)."""
    def make(config, selected, callbacks):
        if "erick" in selected:
            fs = {**FINAL_STATE, "erick_report": "Erick: aguardar o recuo à média."}
            return _FakeGraph(callbacks, fs, "Hold")
        return _FakeGraph(callbacks, FINAL_STATE, "Buy")
    return make


def _stub_enrich(monkeypatch):
    import tradingagents.webui.runner as rm
    monkeypatch.setattr(rm, "fetch_price_chart", lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(rm, "fetch_actionable_plan", lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(rm, "fetch_derivatives_report", lambda t, d: "")


def _make_server(tmp_path, factory):
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path)},
        store=HistoryStore(tmp_path),
        graph_factory=factory,
    )
    httpd = make_server("127.0.0.1", 0, runner=runner)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, f"http://127.0.0.1:{port}"


@pytest.fixture()
def server(tmp_path):
    httpd, base = _make_server(tmp_path, _factory())
    yield base
    httpd.shutdown()


@pytest.fixture()
def dual_server(tmp_path):
    """A server whose fake engine writes a real Erick report — lets the compare
    endpoint exercise the DIRECT Padrão × Erick confront path over HTTP."""
    httpd, base = _make_server(tmp_path, _dual_factory())
    yield base
    httpd.shutdown()


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=5) as resp:
        return resp.status, json.loads(resp.read())


def _post(base, path, payload, headers=None):
    data = json.dumps(payload).encode()
    # Gating BYOK/owner (task 042): endpoints de LLM exigem chave própria OU sessão
    # do dono. Estes testes de roteamento mandam uma chave dummy (o motor é falso e
    # a ignora) só pra passar do gate — o que se testa aqui é o roteamento/fluxo.
    req = urllib.request.Request(
        base + path, data=data,
        headers={"Content-Type": "application/json", "X-LLM-Key": "sk-test",
                 **(headers or {})},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read())


def test_health(server):
    status, body = _get(server, "/api/health")
    assert status == 200 and body["ok"] is True
    # Deploy gracioso (task 022): /api/health expõe as runs ativas pra o restart
    # drenar em vez de matar cego. Sem run em voo, zero.
    assert body["active_runs"] == 0 and body["runs"] == []


def test_health_reports_active_runs(tmp_path):
    """Uma run em voo aparece no /api/health (contagem + id) — o sinal que o drain
    de shutdown usa pra não matar análise no meio (task 022)."""
    gate = threading.Event()
    httpd, base = _make_server(tmp_path, _blocking_factory(gate))
    try:
        _, started = _post(base, "/api/analyze",
                           {"ticker": "AAPL", "date": "2020-01-02"})
        run_id = started["run_id"]
        deadline = time.time() + 3.0
        seen = {}
        while time.time() < deadline:
            _, seen = _get(base, "/api/health")
            if seen.get("active_runs", 0) >= 1:
                break
            time.sleep(0.05)
        assert seen["active_runs"] >= 1
        assert run_id in seen["runs"]
    finally:
        gate.set()
        httpd.shutdown()


def test_health_stays_responsive_while_sanitizing_degraded_report(tmp_path, monkeypatch):
    """Task 025 — a análise NÃO pode congelar o serving. Um relatório degradado com uma
    corrida longa de espaços fazia o passo 4 do sanitizer (``\\s*\\(``) explodir em O(n²):
    o ``re.sub`` segurava o GIL por MINUTOS no thread da run e o ThreadingHTTPServer
    parava de responder (health/progress timavam) — o "caiu/dança/esquece". Prova de
    ponta a ponta: com esse texto na run, o /api/health responde <500ms o TEMPO TODO e a
    run termina rápido (pré-fix: nunca; ficava minutos no re.sub)."""
    _stub_enrich(monkeypatch)
    # 150k espaços contíguos de cada lado: pré-fix isso é O((150k)²) = minutos travando
    # o GIL; pós-fix (``\\s?``) é linear (~centenas de ms).
    pathological = {**FINAL_STATE,
                    "news_report": " " * 150_000 + "dado (NoMarketDataError) ausente" + " " * 150_000}
    httpd, base = _make_server(tmp_path, _factory(final_state=pathological))
    latencies: list[float] = []
    stop = threading.Event()

    def hammer():
        while not stop.is_set():
            t0 = time.time()
            try:
                _get(base, "/api/health")
                latencies.append(time.time() - t0)
            except Exception:
                latencies.append(999.0)   # timeout = servidor congelou
            time.sleep(0.01)

    th = threading.Thread(target=hammer, daemon=True)
    th.start()
    try:
        _, started = _post(base, "/api/analyze", {"ticker": "AAPL", "date": "2020-01-02"})
        run_id = started["run_id"]
        # a run TEM que terminar rápido — pré-fix o sanitize prendia o thread por minutos
        deadline = time.time() + 8.0
        status = None
        while time.time() < deadline:
            _, snap = _get(base, "/api/run/" + run_id)
            status = snap.get("status")
            if status in ("done", "error"):
                break
            time.sleep(0.05)
        assert status == "done", f"run não terminou (status={status}) — sanitize congelou?"
        # health nunca passou de 500ms enquanto a run (e o sanitize) rodava
        assert latencies, "nenhuma amostra de /api/health"
        assert max(latencies) < 0.5, f"/api/health travou: máx {max(latencies) * 1000:.0f}ms"
        # e o vazamento interno foi de fato limpo do relatório
        _, snap = _get(base, "/api/run/" + run_id)
        assert "NoMarketDataError" not in snap["result"]["news_report"]
    finally:
        stop.set()
        th.join(timeout=2)
        httpd.shutdown()


def _cancellable_factory():
    """Grafo cujo ``propagate`` LOOPA batendo nos callbacks (limites de nó) e nunca
    termina sozinho — quando o usuário cancela, o CancelCallbackHandler que o runner
    injeta levanta RunCancelled e o propagate aborta, como num grafo real (task 026)."""
    import uuid as _uuid

    class _Loop:
        def __init__(self, callbacks):
            self.callbacks = callbacks

        def propagate(self, ticker, date, asset_type="stock", timeframe="1d"):
            while True:
                for cb in self.callbacks:
                    cb.on_chain_start({}, {}, run_id=_uuid.uuid4())  # cancel_cb levanta quando setado
                time.sleep(0.02)

    return lambda config, selected, callbacks: _Loop(callbacks)


def test_stop_cancels_run_cleanly_and_frees_active(tmp_path):
    """Task 026 — botão Parar: com a run em andamento, POST cancel a interrompe em
    poucos segundos com estado HONESTO 'cancelled' (não erro, sem result) e active_runs
    volta a 0. Cooperativo — sem thread órfão nem freeze."""
    httpd, base = _make_server(tmp_path, _cancellable_factory())
    try:
        _, started = _post(base, "/api/analyze", {"ticker": "AAPL", "date": "2020-01-02"})
        run_id = started["run_id"]
        deadline = time.time() + 3.0
        health = {}
        while time.time() < deadline:
            _, health = _get(base, "/api/health")
            if health.get("active_runs", 0) >= 1:
                break
            time.sleep(0.03)
        assert run_id in health.get("runs", []), health

        # X-Run-Token (task 007): o Parar é portão de AUTORIA — o run_id sozinho não
        # prova nada (é público em /api/runs). Quem iniciou recebeu o token na resposta.
        st, res = _post(base, "/api/run/" + run_id + "/cancel", {},
                        headers={"X-Run-Token": started["run_token"]})
        assert st == 200 and res["ok"] and res["cancelled"] and res["paused"] is False

        deadline = time.time() + 5.0
        snap = {}
        while time.time() < deadline:
            _, snap = _get(base, "/api/run/" + run_id)
            if snap.get("status") == "cancelled":
                break
            time.sleep(0.05)
        assert snap.get("status") == "cancelled", snap.get("status")
        assert snap.get("result") is None and not snap.get("error")
        # active_runs volta a 0 — a UI fica livre pra nova análise
        _, health2 = _get(base, "/api/health")
        assert health2["active_runs"] == 0
    finally:
        httpd.shutdown()


def _wait_until(pred, timeout=20.0, step=0.03):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(step)
    return False


def test_pause_keeps_descriptor_and_resume_reenqueues(tmp_path):
    """Task 026 — PAUSAR (run resumível de dono/servidor) MANTÉM o descritor da 022 e
    o RETOMAR re-enfileira do checkpoint. Prova pausar→retomar no nível do runner."""
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path)},
        store=HistoryStore(tmp_path),
        graph_factory=_cancellable_factory(),
    )
    # run de dono/servidor (sem chave BYOK, usa a env) → resumível
    rid = runner.start("AAPL", "2020-01-02", overrides={"allow_server_key": True})
    assert _wait_until(lambda: rid in runner.active_run_ids()), "run não ficou ativa"

    res = runner.cancel(rid, keep_resume=True)
    assert res and res["paused"] is True and res["resumable"] is True
    assert _wait_until(lambda: runner.status(rid)["status"] == "cancelled"), "não pausou"
    # PAUSAR guarda o descritor (pra Retomar / boot-resume) — PARAR teria apagado
    assert runner.active.get(rid) is not None

    r2 = runner.resume(rid)
    assert r2 and r2["resuming"] is True
    assert _wait_until(lambda: runner.status(rid)["status"] == "running"), "não retomou"
    runner.cancel(rid, keep_resume=False)   # limpa a run de teste


def test_stop_does_not_keep_descriptor(tmp_path):
    """PARAR (não-retomável) apaga o descritor — nada de boot-resume fantasma."""
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path)},
        store=HistoryStore(tmp_path),
        graph_factory=_cancellable_factory(),
    )
    rid = runner.start("AAPL", "2020-01-02", overrides={"allow_server_key": True})
    assert _wait_until(lambda: rid in runner.active_run_ids())
    runner.cancel(rid, keep_resume=False)   # PARAR
    assert _wait_until(lambda: runner.status(rid)["status"] == "cancelled")
    assert runner.active.get(rid) is None   # descritor apagado


def test_cancel_de_run_alheia_e_403_antes_de_qualquer_busca(server):
    """Task 007: chave própria não é autoria. Sem o token DAQUELA run (nem sessão de
    dono), o Parar é 403 — e nem chega a olhar se a run existe. O 404 de antes era
    justamente a prova de que o portão tinha sido vencido por um header não validado."""
    st = code = None
    try:
        _post(server, "/api/run/nao-existe/cancel", {})
    except urllib.error.HTTPError as e:
        st, code = e.code, json.loads(e.read()).get("error_code")
    assert st == 403 and code == "not_run_owner"


def test_config_endpoint_reports_manaus(server):
    status, body = _get(server, "/api/config")
    assert status == 200
    assert body["tz"] == "America/Manaus"
    assert body["now"].endswith("-04:00")
    assert len(body["today"]) == 10  # YYYY-MM-DD


def test_index_served(server):
    with urllib.request.urlopen(server + "/", timeout=5) as resp:
        html = resp.read().decode()
    assert "TradingDegens" in html
    assert resp.headers["Content-Type"].startswith("text/html")


def test_static_asset_served(server):
    with urllib.request.urlopen(server + "/static/app.js", timeout=5) as resp:
        assert resp.status == 200
        assert "renderMarkdown" in resp.read().decode()


def test_analyze_then_status_flow(server):
    status, body = _post(server, "/api/analyze", {"ticker": "AAPL", "date": "2026-08-22"})
    assert status == 200
    run_id = body["run_id"]
    # poll to completion
    for _ in range(150):
        _, snap = _get(server, "/api/status/" + run_id)
        if snap["status"] != "running":
            break
        time.sleep(0.02)
    assert snap["status"] == "done"
    assert snap["result"]["verdict"] == "Buy"
    # history now lists it
    _, hist = _get(server, "/api/history")
    assert any(r["run_id"] == run_id for r in hist["runs"])


def test_analyze_rejects_empty_ticker(server):
    try:
        _post(server, "/api/analyze", {"ticker": ""})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        return
    raise AssertionError("expected HTTP 400")


def test_unknown_run_is_404(server):
    try:
        _get(server, "/api/status/does-not-exist")
    except urllib.error.HTTPError as e:
        assert e.code == 404
        return
    raise AssertionError("expected HTTP 404")


# ------------------------------------------- nome + busca (task 035) -----------
def test_search_endpoint_returns_candidates(server, monkeypatch):
    """GET /api/search?q= returns name-or-ticker candidates (stubbed, no network)."""
    import tradingagents.webui.runner as rm
    monkeypatch.setattr(rm, "fetch_symbol_search",
                        lambda term, limit=8: [{"symbol": "MSFT", "name": "Microsoft Corporation",
                                                "type": "EQUITY", "exchange": "NMS"}])
    status, body = _get(server, "/api/search?q=Microsoft")
    assert status == 200
    assert body["query"] == "Microsoft"
    assert body["results"][0]["symbol"] == "MSFT"
    assert body["results"][0]["name"] == "Microsoft Corporation"


def test_names_endpoint_batch_resolves(server, monkeypatch):
    """GET /api/names?symbols=A,B resolves each symbol's name for the chips."""
    import tradingagents.webui.runner as rm
    monkeypatch.setattr(rm, "fetch_symbol_names",
                        lambda symbols: {"MSFT": "Microsoft Corporation", "BTC-USD": "Bitcoin USD"})
    status, body = _get(server, "/api/names?symbols=MSFT,BTC-USD")
    assert status == 200
    assert body["names"]["MSFT"] == "Microsoft Corporation"
    assert body["names"]["BTC-USD"] == "Bitcoin USD"


# ------------------------------------------------- timeframe selector (005) -----
def test_chart_endpoint_recomputes_timeframe(server, monkeypatch):
    """GET /api/chart recomputes the chart + plan on the requested frame."""
    import tradingagents.webui.runner as rm
    monkeypatch.setattr(rm, "fetch_price_chart",
                        lambda t, d, tf="1d", method="padrao": {"timeframe": tf, "candles": [{"d": "x"}]})
    monkeypatch.setattr(rm, "fetch_actionable_plan",
                        lambda t, d, tf="1d", method="padrao": {"timeframe": tf, "setup_state": "ativo"})
    status, body = _get(server, "/api/chart?ticker=BTC-USD&date=2026-08-22&tf=4h")
    assert status == 200
    assert body["timeframe"] == "4h"
    assert body["price_chart"]["timeframe"] == "4h"
    assert body["timeframes"] == ["1w", "1d", "4h", "1h", "15m"]


def test_chart_endpoint_recomputes_intraday_for_stock(server, monkeypatch):
    """GET /api/chart?tf=15m now works for an EQUITY too (yfinance keyless intraday):
    it recomputes on the requested frame instead of rejecting with 400."""
    import tradingagents.webui.runner as rm
    monkeypatch.setattr(rm, "fetch_price_chart",
                        lambda t, d, tf="1d", method="padrao": {"timeframe": tf, "candles": [{"d": "x"}]})
    monkeypatch.setattr(rm, "fetch_actionable_plan",
                        lambda t, d, tf="1d", method="padrao": {"timeframe": tf, "setup_state": "ativo"})
    status, body = _get(server, "/api/chart?ticker=AAPL&date=2026-08-22&tf=15m")
    assert status == 200
    assert body["asset_type"] == "stock"
    assert body["timeframe"] == "15m"
    assert body["price_chart"]["timeframe"] == "15m"
    assert body["timeframes"] == ["1w", "1d", "4h", "1h", "15m"]


def test_chart_endpoint_rejects_unknown_frame(server):
    """A frame no asset offers (``3m``) is still a 400 — the ladder is the single
    source of truth and does not include it."""
    try:
        _get(server, "/api/chart?ticker=AAPL&date=2026-08-22&tf=3m")
    except urllib.error.HTTPError as e:
        assert e.code == 400
        return
    raise AssertionError("expected HTTP 400")


# ------------------------------------------- verdict per timeframe (task 012) ---
def test_analyze_accepts_timeframe_and_stamps_verdict(server, monkeypatch):
    """POST /api/analyze with a timeframe runs at that frame and the resulting
    snapshot carries verdict_timeframe."""
    import tradingagents.webui.runner as rm
    # keep the worker hermetic: no exchange/vendor network for the crypto run
    monkeypatch.setattr(rm, "fetch_price_chart", lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(rm, "fetch_actionable_plan", lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(rm, "fetch_derivatives_report", lambda t, d: "")
    status, body = _post(server, "/api/analyze",
                         {"ticker": "BTC-USD", "date": "2026-08-22", "timeframe": "4h"})
    assert status == 200
    run_id = body["run_id"]
    for _ in range(200):
        _, snap = _get(server, "/api/status/" + run_id)
        if snap["status"] != "running":
            break
        time.sleep(0.02)
    assert snap["status"] == "done"
    assert snap["verdict_timeframe"] == "4h"
    assert snap["result"]["verdict_timeframe"] == "4h"


def test_analyze_accepts_intraday_timeframe_for_stock(server, monkeypatch):
    """POST /api/analyze with an equity intraday frame now runs (yfinance keyless):
    the snapshot carries verdict_timeframe=15m instead of a 400."""
    import tradingagents.webui.runner as rm
    monkeypatch.setattr(rm, "fetch_price_chart", lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(rm, "fetch_actionable_plan", lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(rm, "fetch_derivatives_report", lambda t, d: "")
    status, body = _post(server, "/api/analyze",
                         {"ticker": "AAPL", "date": "2026-08-22", "timeframe": "15m"})
    assert status == 200
    run_id = body["run_id"]
    for _ in range(200):
        s, snap = _get(server, f"/api/status/{run_id}")
        if snap["status"] in ("done", "error"):
            break
        time.sleep(0.02)
    assert snap["status"] == "done"
    assert snap["verdict_timeframe"] == "15m"


def test_analyze_rejects_unknown_timeframe_for_stock(server):
    try:
        _post(server, "/api/analyze",
              {"ticker": "AAPL", "date": "2026-08-22", "timeframe": "3m"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        return
    raise AssertionError("expected HTTP 400")


# --------------------------------------------- Padrão × Erick compare (task 017) ---
def test_analyze_compare_flow(server, monkeypatch):
    """POST /api/analyze with compare:true runs both readings and returns a
    compare block (two columns + meta-judge)."""
    import tradingagents.webui.runner as rm
    monkeypatch.setattr(rm, "fetch_price_chart", lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(rm, "fetch_actionable_plan", lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(rm, "fetch_derivatives_report", lambda t, d: "")
    status, body = _post(server, "/api/analyze",
                         {"ticker": "AAPL", "date": "2026-08-22", "compare": True})
    assert status == 200
    run_id = body["run_id"]
    for _ in range(400):
        _, snap = _get(server, "/api/status/" + run_id)
        if snap["status"] != "running":
            break
        time.sleep(0.02)
    assert snap["status"] == "done"
    cmp = snap["result"]["compare"]
    assert set(("a", "b", "meta")).issubset(cmp)
    assert cmp["meta"]["agreement"] in ("concordam", "divergem", "parcial")


def _run_on(base, payload):
    _, body = _post(base, "/api/analyze", payload)
    rid = body["run_id"]
    for _ in range(400):
        _, snap = _get(base, "/api/status/" + rid)
        if snap["status"] != "running":
            break
        time.sleep(0.02)
    return rid


def test_compare_endpoint_confronts_two_runs(dual_server, monkeypatch):
    """POST /api/compare directly meta-judges a valid Padrão × Erick pair (same
    frame/date) — no re-run, ``manual`` flagged."""
    _stub_enrich(monkeypatch)
    a = _run_on(dual_server, {"ticker": "AAPL", "date": "2026-08-22", "method": "padrao"})
    b = _run_on(dual_server, {"ticker": "AAPL", "date": "2026-08-22", "method": "erick"})
    status, snap = _post(dual_server, "/api/compare", {"a": a, "b": b})
    assert status == 200
    cmp = snap["result"]["compare"]
    assert cmp["manual"] is True
    assert set(("a", "b", "meta")).issubset(cmp)
    assert cmp["a"]["method"] == "padrao" and cmp["b"]["method"] == "erick"


def test_compare_endpoint_reroutes_same_method(server, monkeypatch):
    """POST /api/compare with two SAME-method runs never yields método×ele-mesmo:
    it reroutes to a real Padrão × Erick run (task 024). The default fake engine
    writes no erick_report, so both runs read as Padrão → reroute."""
    _stub_enrich(monkeypatch)
    a = _run_on(server, {"ticker": "AAPL", "date": "2026-08-22", "method": "padrao"})
    b = _run_on(server, {"ticker": "AAPL", "date": "2026-08-22", "method": "padrao"})
    status, snap = _post(server, "/api/compare", {"a": a, "b": b})
    assert status == 200
    assert snap.get("rerouted") is True and "compare" not in (snap.get("result") or {})
    rid = snap["run_id"]
    for _ in range(400):
        _, done = _get(server, "/api/status/" + rid)
        if done["status"] != "running":
            break
        time.sleep(0.02)
    cmp = done["result"]["compare"]
    assert cmp["a"]["method"] == "padrao" and cmp["b"]["method"] == "erick"
    assert cmp["meta"]["agreement"] != "invalido"


def test_compare_endpoint_rejects_cross_ticker(server):
    try:
        _post(server, "/api/compare", {"a": "does-not-exist-1", "b": "does-not-exist-2"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        return
    raise AssertionError("expected HTTP 400")


def test_chart_endpoint_requires_ticker(server):
    try:
        _get(server, "/api/chart?tf=1d")
    except urllib.error.HTTPError as e:
        assert e.code == 400
        return
    raise AssertionError("expected HTTP 400")


# ------------------------------------------------ background runs (task 010) ---
def test_runs_and_history_expose_in_flight(tmp_path):
    """A run still executing is reachable both at /api/runs and merged into
    /api/history as ``running`` — so the UI can show it as em-andamento and re-open
    it without the analysis ever being cancelled."""
    gate = threading.Event()
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path)},
        store=HistoryStore(tmp_path),
        graph_factory=_blocking_factory(gate),
    )
    httpd = make_server("127.0.0.1", 0, runner=runner)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        _, started = _post(base, "/api/analyze", {"ticker": "AAPL", "date": "2026-08-22"})
        run_id = started["run_id"]
        _, runs = _get(base, "/api/runs?status=running")
        assert any(r["run_id"] == run_id and r["status"] == "running" for r in runs["runs"])
        _, hist = _get(base, "/api/history")
        assert any(r["run_id"] == run_id and r["status"] == "running" for r in hist["runs"])
    finally:
        gate.set()
        httpd.shutdown()


# --------------------------------------------- scan de portfólio + 1-2-3 (28/08) --
def test_scan_endpoint_classifies_watchlist(server, monkeypatch):
    """GET /api/scan varre a watchlist (semeada do histórico na 1ª vez) e devolve
    a classificação por urgência — $0 de LLM, sem gate."""
    import tradingagents.webui.runner as rm

    monkeypatch.setattr(
        rm, "scan_watchlist",
        lambda tickers, date, frames=("1d", "4h"): {
            "date": date, "frames": ["1d", "4h"],
            "resumo": {"em_gatilho": 1},
            "ativos": [{"ticker": "MSFT", "frames": [], "melhor": {"estado": "em_gatilho"}}],
        })
    status, body = _get(server, "/api/scan?date=2026-08-28")
    assert status == 200
    assert body["resumo"]["em_gatilho"] == 1
    assert body["ativos"][0]["ticker"] == "MSFT"


def test_watchlist_read_is_public_write_is_owner_only(server):
    """GET /api/watchlist é público (como /api/chart); POST sem sessão de dono → 403."""
    status, body = _get(server, "/api/watchlist")
    assert status == 200 and "tickers" in body
    # sem dono logado e sem chave: a EDIÇÃO é barrada (a lista é curada pelo dono)
    req = urllib.request.Request(
        server + "/api/watchlist", data=json.dumps({"action": "add", "ticker": "NVDA"}).encode(),
        headers={"Content-Type": "application/json", "X-LLM-Key": "sk-test"})
    try:
        urllib.request.urlopen(req, timeout=5)
        raise AssertionError("POST devia ser 403 sem dono")
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_watchlist_owner_edits(tmp_path):
    """Dono logado adiciona e remove da watchlist — persistida no disco."""
    import os

    from tradingagents.webui.auth import OwnerAuth
    from tradingagents.webui.store import HistoryStore

    os.environ["TRADINGDEGENS_OWNER_TOKEN"] = "pw-scan"
    try:
        runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                                store=HistoryStore(tmp_path), graph_factory=_factory())
        httpd = make_server("127.0.0.1", 0, runner=runner, auth=OwnerAuth())
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"
        try:
            op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

            def post(path, payload):
                req = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                                             headers={"Content-Type": "application/json"})
                with op.open(req, timeout=5) as resp:   # opener carrega o cookie de dono
                    return resp.status, json.loads(resp.read())

            assert post("/api/login", {"password": "pw-scan"})[0] == 200
            _, body = post("/api/watchlist", {"action": "add", "ticker": "nvda"})
            assert body["ok"] is True and "NVDA" in [w["ticker"] for w in body["tickers"]]
            _, body = post("/api/watchlist", {"action": "remove", "ticker": "NVDA"})
            assert "NVDA" not in [w["ticker"] for w in body["tickers"]]
            # persistiu de verdade (arquivo no disco da store)
            assert (tmp_path / "watchlist.json").exists()
        finally:
            httpd.shutdown()
    finally:
        os.environ.pop("TRADINGDEGENS_OWNER_TOKEN", None)


@pytest.mark.parametrize("payload,porque", [
    ({"method": "setup123", "compare": True},
     "setup123+compare caía em start_compare (Padrão × Erick × meta-juiz) na chave do servidor"),
    ({"method": "SETUP123", "compare": True},
     "o método é normalizado pra minúsculo — o bypass não pode voltar pelo caixa alta"),
    ({"method": "setup123", "compare": 1},
     "truthy que não é bool tem que contar igual"),
])
def test_setup123_nao_e_passe_livre_pra_rota_com_llm(server, payload, porque):
    """Nenhum caminho ANÔNIMO dispara LLM (C1).

    A isenção de gate do atalho 1-2-3 era avaliada pelo RÓTULO do método, antes do
    ramo ``compare`` — que é quem decide a rota de verdade. Sem sessão de dono e sem
    ``X-LLM-Key``, estes corpos tinham que ser 403 e criavam run paga.
    """
    req = urllib.request.Request(
        server + "/api/analyze",
        data=json.dumps({"ticker": "MSFT", "date": "2026-08-28", **payload}).encode(),
        headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=5)
    assert ei.value.code == 403, porque
    assert json.loads(ei.value.read()).get("error_code") == "need_key"


def test_atalho_puro_segue_livre_o_portao_nao_engordou(server, monkeypatch):
    """Contra-prova do fix: fechar o bypass não pode fechar o atalho legítimo."""
    import tradingagents.webui.runner as rm

    monkeypatch.setattr(rm, "fetch_price_chart",
                        lambda t, d, tf="1d", method="padrao": {"candles": [{"c": 1.0}]})
    monkeypatch.setattr(rm, "fetch_actionable_plan",
                        lambda t, d, tf="1d", method="padrao":
                        {"price": 100.0, "pattern": None, "setup_state": "sem_setup"})
    req = urllib.request.Request(
        server + "/api/analyze",
        data=json.dumps({"ticker": "MSFT", "date": "2026-08-28",
                         "method": "setup123", "compare": False}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200


def test_setup123_run_is_instant_free_and_ungated(server, monkeypatch):
    """POST /api/analyze com method=setup123: run instantânea $0 — SEM chave de LLM
    (o gate protege custo que não existe), status done com o plano estrutural."""
    import tradingagents.webui.runner as rm

    monkeypatch.setattr(rm, "fetch_price_chart",
                        lambda t, d, tf="1d", method="padrao": {"candles": [{"c": 1.0}]})
    monkeypatch.setattr(rm, "fetch_actionable_plan",
                        lambda t, d, tf="1d", method="padrao":
                        {"price": 100.0, "pattern": {"trigger": 100.5, "state": "formando",
                                                     "direction": "compra"},
                         "setup_state": "aguardar_rompimento"})
    # SEM X-LLM-Key e sem dono: o 1-2-3 roda mesmo assim (é $0)
    req = urllib.request.Request(
        server + "/api/analyze",
        data=json.dumps({"ticker": "MSFT", "date": "2026-08-28", "method": "setup123"}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        body = json.loads(resp.read())
    assert resp.status == 200
    # a run termina sozinha quase na hora (worker determinístico)
    deadline = time.time() + 3
    snap = None
    while time.time() < deadline:
        snap = json.loads(urllib.request.urlopen(
            f"{server}/api/status/{body['run_id']}", timeout=5).read())
        if snap["status"] != "running":
            break
        time.sleep(0.05)
    assert snap and snap["status"] == "done"
    assert snap["method"] == "setup123"
    assert snap["cost"]["usd"] == 0
    assert (snap["result"] or {}).get("setup123") is True
    assert snap["result"]["actionable"]["pattern"]["trigger"] == 100.5

"""ATUALIZAR uma etapa com DADO FRESCO (task 002 / DA-062).

Samyr, olhando uma análise que continuou de onde parou: *"as fases devem aparecer
verdes e ter um botão de atualizar se eu achar que quero dados atualizados em uma
delas"*. São dois comportamentos distintos, cobertos aqui:

  • **retomada honesta** — a run que volta do checkpoint entra no stepper com as
    etapas preservadas já CONCLUÍDAS (e o parecer delas no painel), em vez de cinza
    fingindo que nada rodou;
  • **atualizar uma etapa** — re-roda SÓ ela com dado novo (cache de preço do ativo
    invalidado + checkpoint rebobinado), reaproveitando as anteriores. Não é o
    ESCALAR da 027: ali muda o LLM, aqui muda o DADO.

O rebobinar contra o LangGraph de verdade vive em ``test_checkpoint_rewind.py``;
aqui a asserção é sobre a ORQUESTRAÇÃO do runner (porteiro, pausa antes de mexer no
checkpoint, re-enfileiramento) com um fake de grafo — nenhum LLM é chamado.
"""

import threading

import pytest

import tradingagents.webui.runner as runner_module
from tests.test_webui_runner import FINAL_STATE, _blocking_factory, _FakeGraph, _wait
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.store import HistoryStore

MARKET = "Market Analyst"


@pytest.fixture(autouse=True)
def _stub_enrich(monkeypatch):
    monkeypatch.setattr(runner_module, "fetch_price_chart",
                        lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_actionable_plan",
                        lambda t, d, tf="1d", method="padrao": {})
    monkeypatch.setattr(runner_module, "fetch_derivatives_report", lambda t, d: "")


def _counting_factory(calls):
    def make(config, selected_analysts, callbacks):
        calls.append(dict(config))
        return _FakeGraph(callbacks, FINAL_STATE, "Buy")
    return make


def _runner(tmp_path, calls, **kw):
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    return AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "openai",
                     "data_cache_dir": str(cache)},
        store=HistoryStore(tmp_path),
        graph_factory=kw.get("factory") or _counting_factory(calls),
    )


def _put_resumable(runner, rid, resumable=True):
    runner.active.put(rid, {
        "run_id": rid, "ticker": "AAPL", "date": "2020-01-02",
        "asset_type": "stock", "timeframe": "1d", "method": "padrao",
        "selected_analysts": ["market", "social", "news", "fundamentals"],
        "started_at": "2026-08-27T10:00:00-04:00", "resumable": resumable,
        "overrides": {"allow_server_key": True} if resumable else {},
    })


# --------------------------------------------------------------- porteiro ------
def test_refresh_unknown_run_is_none(tmp_path):
    """Sem descritor não há o que atualizar — 404 honesto, não um re-run às cegas."""
    assert _runner(tmp_path, []).refresh_step("nao-existe", MARKET) is None


def test_refresh_unknown_step_is_rejected(tmp_path):
    """Nó que não é etapa do pipeline não rebobina nada — recusa antes de tocar
    no checkpoint."""
    runner = _runner(tmp_path, [])
    _put_resumable(runner, "r-01")
    res = runner.refresh_step("r-01", "Nó Inventado")
    assert res["ok"] is False and res["code"] == "bad_step"


def test_refresh_rejects_non_resumable_run(tmp_path):
    """Run BYOK não é retomável (a chave não fica salva): indisponível HONESTO, sem
    prometer uma atualização que não dá pra fazer."""
    calls: list = []
    runner = _runner(tmp_path, calls)
    _put_resumable(runner, "byok-01", resumable=False)
    res = runner.refresh_step("byok-01", MARKET)
    assert res["ok"] is False and res["code"] == "not_resumable"
    assert "chave própria" in res["error"]
    assert calls == []                                  # nada re-rodou


# ------------------------------------------------------- atualizar de verdade ---
def test_refresh_reruns_the_run_and_drops_the_stale_price_cache(tmp_path):
    """O clique no 🔄 re-enfileira a run E apaga o dado de preço que podia estar
    velho — sem isso a etapa re-rodaria lendo o MESMO candle do cache e o usuário
    'atualizaria' pra receber o que já tinha."""
    calls: list = []
    runner = _runner(tmp_path, calls)
    cache = tmp_path / "cache"
    stale = cache / "AAPL-YFin-5y.csv"
    stale.write_text("Date,Close\n2020-01-02,100\n", encoding="utf-8")
    keep = cache / "AAPL-YFin-intraday-15m-2020-01-02.csv"   # dia passado: imutável
    keep.write_text("Date,Close\n", encoding="utf-8")
    _put_resumable(runner, "r-02")

    res = runner.refresh_step("r-02", MARKET)
    assert res["ok"] is True and res["node"] == MARKET
    assert res["paused_first"] is False                 # run já parada: nada a pausar
    snap = _wait(runner, "r-02")
    assert snap["status"] == "done"
    assert len(calls) == 1                              # re-rodou uma vez
    assert not stale.exists()                           # dado volátil: invalidado
    assert keep.exists()                                # histórico imutável: preservado


def test_refresh_pauses_a_live_run_before_touching_the_checkpoint(tmp_path):
    """Mexer no checkpoint sob um grafo em execução o corromperia: a atualização
    PAUSA a run primeiro e só então rebobina e re-entra."""
    gate = threading.Event()
    calls: list = []
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "openai",
                     "data_cache_dir": str(tmp_path)},
        store=HistoryStore(tmp_path), graph_factory=_blocking_factory(gate))
    # chave do SERVIDOR (dono logado) — é o que torna a run retomável/atualizável
    rid = runner.start("AAPL", "2020-01-02", overrides={"allow_server_key": True})
    try:
        res = runner.refresh_step(rid, MARKET)
        assert res["ok"] is True and res["paused_first"] is True
        # enquanto o pausar→rebobinar→re-entrar acontece, o snapshot DIZ isso — a UI
        # não pode piscar "pausada" no meio de um gesto que ainda está em curso
        live = runner.status(rid)
        assert live["refreshing"]["node"] == MARKET
        run = runner._runs[rid]
        assert run.cancel_event.is_set() and run.pause_keep_resume is True
    finally:
        gate.set()
    _wait(runner, rid, timeout=8.0)
    assert calls == []                                  # (fábrica bloqueante)


def test_refresh_is_idempotent_while_in_flight(tmp_path):
    """Dois cliques no mesmo 🔄 não disparam duas atualizações."""
    runner = _runner(tmp_path, [])
    _put_resumable(runner, "r-03")
    runner._refreshing["r-03"] = {"node": MARKET, "label": "x"}
    res = runner.refresh_step("r-03", MARKET)
    assert res["ok"] is True and res.get("paused_first") is None


# --------------------------------------------- retomada pinta o que foi salvo ---
def test_resumed_run_marks_checkpointed_stages_as_done(tmp_path, monkeypatch):
    """A run que CONTINUA de onde parou entra com as etapas preservadas concluídas no
    stepper (verdes, marcadas ``reused``) e com o parecer delas no painel — o motor
    não re-executa nó concluído, então sem isto a tela mostraria cinza."""
    calls: list = []
    runner = _runner(tmp_path, calls)
    monkeypatch.setattr(
        "tradingagents.graph.checkpointer.completed_reports",
        lambda data_dir, ticker, date, signature="": {
            MARKET: "## Mercado\nrelatório preservado do checkpoint",
            "Sentiment Analyst": "## Sentimento\npreservado",
        },
    )
    _put_resumable(runner, "dead-01")
    assert runner.resume_interrupted() == 1
    snap = _wait(runner, "dead-01")

    states = {s["node"]: s["state"] for s in snap["progress"]["steps"]}
    assert states[MARKET] == "reused"
    assert states["Sentiment Analyst"] == "reused"
    # o que o fake DE FATO rodou neste run continua contando como executado
    assert states["Portfolio Manager"] == "done"
    cards = {c["id"]: c for c in snap["thinking"]}
    assert cards[MARKET]["reused"] is True
    assert "preservado" in cards[MARKET]["text"]


def test_fresh_run_has_no_reused_stages(tmp_path):
    """Run normal (sem retomada) não marca nada como reaproveitado — o ♻ só aparece
    quando há trabalho preservado de verdade."""
    calls: list = []
    runner = _runner(tmp_path, calls)
    rid = runner.start("AAPL", "2020-01-02")
    snap = _wait(runner, rid)
    states = {s["state"] for s in snap["progress"]["steps"]}
    assert "reused" not in states
    assert all(c["reused"] is False for c in snap["thinking"])

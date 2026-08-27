"""History store round-trips records and returns them newest-first."""

from tradingagents.webui.store import HistoryStore


def _rec(run_id, ticker, verdict):
    return {
        "run_id": run_id, "ticker": ticker, "date": "2026-08-22",
        "asset_type": "stock", "status": "done", "verdict": verdict,
        "cost_usd": 0.026, "elapsed": 100.0, "finished_at": "2026-08-22T12:00:00",
        "result": {"bull": "b", "bear": "r", "verdict": verdict},
    }


def test_save_and_get_roundtrip(tmp_path):
    store = HistoryStore(tmp_path)
    store.save(_rec("r1", "AAPL", "Buy"))
    got = store.get("r1")
    assert got["ticker"] == "AAPL"
    assert got["result"]["bear"] == "r"


def test_recent_is_newest_first(tmp_path):
    store = HistoryStore(tmp_path)
    for i in range(3):
        store.save(_rec(f"r{i}", f"T{i}", "Hold"))
    recent = store.recent()
    assert [r["run_id"] for r in recent] == ["r2", "r1", "r0"]
    # summaries only — no heavy result payload in the index
    assert "result" not in recent[0]


def test_recent_limit(tmp_path):
    store = HistoryStore(tmp_path)
    for i in range(10):
        store.save(_rec(f"r{i}", "X", "Sell"))
    assert len(store.recent(limit=4)) == 4


def test_watchlist_only_grows_beyond_recent_window(tmp_path):
    # Lista de observação (task 011): TODO ticker já pesquisado aparece, mesmo que seus
    # runs tenham saído da janela dos 25 mais recentes. 30 runs de T0..T29 + um run
    # ANTIGO de OLD (o 1º de todos): recent(25) não o vê, watchlist() SIM.
    store = HistoryStore(tmp_path)
    store.save(_rec("old", "OLD", "Buy"))          # o mais antigo de todos
    for i in range(30):
        store.save(_rec(f"r{i}", f"T{i}", "Hold"))
    recent = [r["ticker"] for r in store.recent(25)]
    assert "OLD" not in recent                       # caiu da janela dos 25
    wl = store.watchlist()
    tickers = [w["ticker"] for w in wl]
    assert "OLD" in tickers                          # a watchlist só CRESCE
    assert len(tickers) == 31 and len(set(tickers)) == 31   # um por ticker, todos


def test_watchlist_one_per_ticker_most_recent_with_count(tmp_path):
    store = HistoryStore(tmp_path)
    store.save(_rec("m1", "MCD", "Buy"))
    store.save(_rec("b1", "BTC", "Hold"))
    store.save(_rec("m2", "MCD", "Sell"))            # mais recente do MCD
    wl = store.watchlist()
    # ordenado pela atividade mais recente: MCD (m2) no topo, depois BTC
    assert [w["ticker"] for w in wl] == ["MCD", "BTC"]
    mcd = next(w for w in wl if w["ticker"] == "MCD")
    assert mcd["run_id"] == "m2" and mcd["verdict"] == "Sell"   # o run mais recente
    assert mcd["count"] == 2                          # 2 análises do MCD
    assert next(w for w in wl if w["ticker"] == "BTC")["count"] == 1


def test_watchlist_empty_when_no_history(tmp_path):
    assert HistoryStore(tmp_path).watchlist() == []


def test_get_unknown_returns_none(tmp_path):
    assert HistoryStore(tmp_path).get("nope") is None


def test_recent_empty_when_no_history(tmp_path):
    assert HistoryStore(tmp_path).recent() == []


def test_delete_ticker_removes_all_runs_and_files(tmp_path):
    # A lista lateral é por ATIVO, então o × remove o ativo inteiro (todas as
    # análises daquele ticker). Some do índice E os arquivos de run são apagados.
    store = HistoryStore(tmp_path)
    store.save(_rec("m1", "MCD", "Buy"))
    store.save(_rec("m2", "MCD", "Hold"))   # segundo run do MESMO ativo
    store.save(_rec("b1", "BTC", "Buy"))
    removed = store.delete_ticker("mcd")     # case-insensitive
    assert removed == 2
    assert [r["run_id"] for r in store.recent()] == ["b1"]
    assert store.get("m1") is None and store.get("m2") is None
    assert store.get("b1") is not None
    assert not (store.runs_dir / "m1.json").exists()
    assert not (store.runs_dir / "m2.json").exists()
    assert (store.runs_dir / "b1.json").exists()


def test_delete_ticker_idempotent_and_guards(tmp_path):
    store = HistoryStore(tmp_path)
    store.save(_rec("b1", "BTC", "Buy"))
    assert store.delete_ticker("NADA") == 0   # ticker inexistente
    assert store.delete_ticker("") == 0       # vazio é no-op
    assert store.delete_ticker("BTC") == 1
    assert store.delete_ticker("BTC") == 0    # idempotente
    assert store.recent() == []

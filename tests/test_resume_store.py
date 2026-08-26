"""The active-run descriptor store: put on start, remove on terminal, list the
survivors on boot (task 022)."""

from tradingagents.webui.resume_store import ActiveRunStore


def test_put_list_remove_roundtrip(tmp_path):
    store = ActiveRunStore(tmp_path / "active")
    assert store.list_pending() == []

    store.put("r1", {"run_id": "r1", "ticker": "AAPL", "resumable": True})
    store.put("r2", {"run_id": "r2", "ticker": "MSFT", "resumable": False})
    pending = {d["run_id"]: d for d in store.list_pending()}
    assert set(pending) == {"r1", "r2"}
    assert pending["r1"]["ticker"] == "AAPL"

    store.remove("r1")
    ids = {d["run_id"] for d in store.list_pending()}
    assert ids == {"r2"}


def test_remove_is_idempotent(tmp_path):
    store = ActiveRunStore(tmp_path / "active")
    store.remove("nope")            # never written — must not raise
    store.put("r1", {"run_id": "r1"})
    store.remove("r1")
    store.remove("r1")             # already gone — still fine
    assert store.list_pending() == []


def test_put_overwrites_same_run(tmp_path):
    store = ActiveRunStore(tmp_path / "active")
    store.put("r1", {"run_id": "r1", "step": "market"})
    store.put("r1", {"run_id": "r1", "step": "trader"})
    pending = store.list_pending()
    assert len(pending) == 1 and pending[0]["step"] == "trader"


def test_corrupt_descriptor_is_skipped(tmp_path):
    store = ActiveRunStore(tmp_path / "active")
    store.put("r1", {"run_id": "r1"})
    # A half-written / garbage file must not break enumeration of the good ones.
    (store.base / "broken.json").write_text("{not json", encoding="utf-8")
    ids = {d["run_id"] for d in store.list_pending()}
    assert ids == {"r1"}


def test_descriptor_without_run_id_ignored(tmp_path):
    store = ActiveRunStore(tmp_path / "active")
    (store.base / "orphan.json").write_text('{"ticker": "AAPL"}', encoding="utf-8")
    assert store.list_pending() == []

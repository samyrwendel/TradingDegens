"""LangGraph checkpoint support for resumable analysis runs.

Per-ticker SQLite databases so concurrent tickers don't contend.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from tradingagents.dataflows.utils import safe_ticker_component


def _db_path(data_dir: str | Path, ticker: str) -> Path:
    """Return the SQLite checkpoint DB path for a ticker."""
    # Reject ticker values that would escape the checkpoints directory.
    safe = safe_ticker_component(ticker).upper()
    p = Path(data_dir) / "checkpoints"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{safe}.db"


def thread_id(ticker: str, date: str, signature: str = "") -> str:
    """Deterministic thread ID for a ticker+date pair.

    ``signature`` folds in graph-shape-affecting run choices so a resume under a
    different graph can't reuse this checkpoint (#1089); omitting it keeps the
    legacy ID.
    """
    base = f"{ticker.upper()}:{date}"
    if signature:
        base = f"{base}:{signature}"
    return hashlib.sha256(base.encode()).hexdigest()[:16]


@contextmanager
def get_checkpointer(data_dir: str | Path, ticker: str) -> Generator[SqliteSaver, None, None]:
    """Context manager yielding a SqliteSaver backed by a per-ticker DB."""
    db = _db_path(data_dir, ticker)
    conn = sqlite3.connect(str(db), check_same_thread=False)
    try:
        saver = SqliteSaver(conn)
        saver.setup()
        yield saver
    finally:
        conn.close()


def has_checkpoint(data_dir: str | Path, ticker: str, date: str, signature: str = "") -> bool:
    """Check whether a resumable checkpoint exists for ticker+date."""
    return checkpoint_step(data_dir, ticker, date, signature) is not None


def checkpoint_step(data_dir: str | Path, ticker: str, date: str, signature: str = "") -> int | None:
    """Return the step number of the latest checkpoint, or None if none exists."""
    db = _db_path(data_dir, ticker)
    if not db.exists():
        return None
    tid = thread_id(ticker, date, signature)
    with get_checkpointer(data_dir, ticker) as saver:
        config = {"configurable": {"thread_id": tid}}
        cp = saver.get_tuple(config)
        if cp is None:
            return None
        return cp.metadata.get("step")


def clear_all_checkpoints(data_dir: str | Path) -> int:
    """Remove all checkpoint DBs. Returns number of files deleted."""
    cp_dir = Path(data_dir) / "checkpoints"
    if not cp_dir.exists():
        return 0
    dbs = list(cp_dir.glob("*.db"))
    for db in dbs:
        db.unlink()
    return len(dbs)


def clear_checkpoint(data_dir: str | Path, ticker: str, date: str, signature: str = "") -> None:
    """Remove checkpoint for a specific ticker+date by deleting the thread's rows."""
    db = _db_path(data_dir, ticker)
    if not db.exists():
        return
    tid = thread_id(ticker, date, signature)
    conn = sqlite3.connect(str(db))
    try:
        for table in ("writes", "checkpoints"):
            conn.execute(f"DELETE FROM {table} WHERE thread_id = ?", (tid,))
        conn.commit()
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()


# ── Which stages are already DONE inside a checkpoint (task 002 / DA-062) ──────
# LangGraph never re-executes a node that the checkpoint already completed, so on a
# resume its progress callback never fires and the stepper would paint the preserved
# work grey — as if nothing had run. The checkpoint itself knows better: each node
# leaves its output on a state channel. Reading those channels turns "what came back
# from the checkpoint" into a fact the UI can show green, and gives the per-stage
# "refresh with fresh data" button a precise place to rewind to.
#
# A dotted path addresses a sub-field of a state dict (the debates keep their turns
# inside one channel). Order = pipeline order.
_NODE_OUTPUT: dict[str, tuple[str, ...]] = {
    "Market Analyst":       ("market_report",),
    "Sentiment Analyst":    ("sentiment_report",),
    "News Analyst":         ("news_report",),
    "Fundamentals Analyst": ("fundamentals_report",),
    "Erick Analyst":        ("erick_report",),
    "Bull Researcher":      ("investment_debate_state.bull_history",),
    "Bear Researcher":      ("investment_debate_state.bear_history",),
    "Research Manager":     ("investment_plan",),
    "Trader":               ("trader_investment_plan",),
    "Aggressive Analyst":   ("risk_debate_state.aggressive_history",),
    "Conservative Analyst": ("risk_debate_state.conservative_history",),
    "Neutral Analyst":      ("risk_debate_state.neutral_history",),
    "Portfolio Manager":    ("final_trade_decision",),
}


def node_output_channels(node: str) -> tuple[str, ...]:
    """State channel(s) a pipeline node fills when it completes; ``()`` if unknown."""
    return _NODE_OUTPUT.get(node, ())


def _channel_value(values: dict, path: str):
    """Value at ``path`` (``"a"`` or ``"a.b"``) inside a checkpoint's channel values."""
    cur = values
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _is_filled(value) -> bool:
    """Whether a channel actually carries a node's output.

    An analyst mid-tool-loop writes an EMPTY report before the real one, and the
    debate dicts exist from the initial state — so presence alone would claim a
    stage finished when it did not. Only non-empty content counts.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def completed_reports(data_dir: str | Path, ticker: str, date: str,
                      signature: str = "") -> dict[str, str]:
    """``{node: text}`` for every stage already finished in the latest checkpoint.

    The text is the stage's own output, so a resumed run can show the preserved work
    instead of an empty panel. Empty dict when there is no checkpoint. Never raises —
    a resume must not die because the recovery DB is unreadable.
    """
    db = _db_path(data_dir, ticker)
    if not db.exists():
        return {}
    tid = thread_id(ticker, date, signature)
    try:
        with get_checkpointer(data_dir, ticker) as saver:
            cp = saver.get_tuple({"configurable": {"thread_id": tid}})
            if cp is None:
                return {}
            values = cp.checkpoint.get("channel_values") or {}
    except Exception:  # noqa: BLE001 — introspection is best-effort
        return {}
    out: dict[str, str] = {}
    for node, paths in _NODE_OUTPUT.items():
        for path in paths:
            value = _channel_value(values, path)
            if _is_filled(value):
                out[node] = value if isinstance(value, str) else str(value)
                break
    return out


def completed_nodes(data_dir: str | Path, ticker: str, date: str,
                    signature: str = "") -> list[str]:
    """Pipeline nodes already finished in the latest checkpoint, in pipeline order."""
    return list(completed_reports(data_dir, ticker, date, signature))


def rewind_checkpoint(data_dir: str | Path, ticker: str, date: str,
                      signature: str = "", *, channels: Sequence[str]) -> str | None:
    """Rewind a thread to just BEFORE the stage that fills ``channels`` first ran.

    This is the engine behind "atualizar esta etapa": drop every checkpoint written
    from that stage onwards (plus the head's pending writes, which ARE that stage's
    output) so the next resume re-executes it with fresh data, while every earlier
    stage stays checkpointed and costs nothing.

    Returns the checkpoint id kept as the new head, or ``None`` when there is
    nothing to rewind (no checkpoint, or the stage never ran here). Downstream
    stages necessarily re-run too — they were judged on the old output, and keeping
    them would be a verdict built on data the user just replaced.
    """
    if not channels:
        return None
    db = _db_path(data_dir, ticker)
    if not db.exists():
        return None
    tid = thread_id(ticker, date, signature)
    try:
        with get_checkpointer(data_dir, ticker) as saver:
            cfg = {"configurable": {"thread_id": tid}}
            head = None
            ran = False
            # Newest → oldest. Channels only accumulate, so the newest checkpoint
            # where the stage's output is still empty is exactly the state it ran
            # from. If it is never filled, the stage never completed here.
            for tup in saver.list(cfg):
                values = tup.checkpoint.get("channel_values") or {}
                if any(_is_filled(_channel_value(values, p)) for p in channels):
                    ran = True
                    continue
                head = tup.config["configurable"]["checkpoint_id"]
                break
            if head is None or not ran:
                return None
    except Exception:  # noqa: BLE001
        return None
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "DELETE FROM checkpoints WHERE thread_id = ? AND checkpoint_id > ?",
            (tid, head),
        )
        # ``>=`` on purpose: the writes stored AT the head are the outputs of the
        # tasks launched from it — the very stage being refreshed. Leaving them
        # would replay the stale answer instead of re-running the stage.
        conn.execute(
            "DELETE FROM writes WHERE thread_id = ? AND checkpoint_id >= ?",
            (tid, head),
        )
        conn.commit()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return head


def rewind_before_node(data_dir: str | Path, ticker: str, date: str,
                       signature: str = "", *, node: str) -> str | None:
    """:func:`rewind_checkpoint` addressed by pipeline node name."""
    return rewind_checkpoint(data_dir, ticker, date, signature,
                             channels=node_output_channels(node))

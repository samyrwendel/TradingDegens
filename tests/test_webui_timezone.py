"""All user-facing times resolve in America/Manaus (GMT-4), never UTC.

The headline case (task 008 acceptance #3): late evening in Manaus is already the
next calendar day in UTC — the date default must follow Manaus, or a run silently
carries tomorrow's date.
"""

from datetime import datetime, timezone

import pytest

import tradingagents.webui.runner as runner_module
from tradingagents.webui import timeutil
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.store import HistoryStore
from tests.test_webui_runner import _factory, _wait


def test_today_uses_manaus_not_utc_across_midnight():
    # 01:30 UTC on the 24th is 21:30 on the 23rd in Manaus (the brief's scenario).
    ref = datetime(2026, 8, 24, 1, 30, tzinfo=timezone.utc)
    assert timeutil.today(ref) == "2026-08-23"


def test_today_just_after_manaus_midnight():
    # 04:30 UTC on the 24th is 00:30 on the 24th in Manaus — now it IS the 24th.
    ref = datetime(2026, 8, 24, 4, 30, tzinfo=timezone.utc)
    assert timeutil.today(ref) == "2026-08-24"


def test_today_accepts_aware_manaus_reference():
    ref = datetime(2026, 8, 23, 21, 30, tzinfo=timeutil.MANAUS)
    assert timeutil.today(ref) == "2026-08-23"


def test_today_naive_reference_treated_as_utc():
    # naive 00:30 (treated as UTC) -> previous day in Manaus
    assert timeutil.today(datetime(2026, 8, 24, 0, 30)) == "2026-08-23"


def test_stamp_carries_minus_four_offset():
    s = timeutil.stamp(datetime(2026, 8, 24, 1, 30, tzinfo=timezone.utc))
    assert s == "2026-08-23T21:30:00-04:00"


def test_run_id_stamp_is_manaus_compact():
    assert timeutil.run_id_stamp(datetime(2026, 8, 24, 1, 30, tzinfo=timezone.utc)) == "20260823-213000"


def test_start_defaults_date_to_manaus_today(tmp_path, monkeypatch):
    monkeypatch.setattr(timeutil, "today", lambda reference=None: "2026-08-23")
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    run_id = runner.start("AAPL", "")  # empty date -> Manaus today
    snap = _wait(runner, run_id)
    assert snap["date"] == "2026-08-23"


def test_start_keeps_explicit_date(tmp_path):
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    run_id = runner.start("AAPL", "2026-01-15")
    snap = _wait(runner, run_id)
    assert snap["date"] == "2026-01-15"


def test_finished_at_is_manaus_offset(tmp_path):
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    run_id = runner.start("AAPL", "2026-08-22")
    snap = _wait(runner, run_id)
    assert snap["finished_at"].endswith("-04:00")
    # persisted history carries the same offset-aware stamp
    assert runner.store.get(run_id)["finished_at"].endswith("-04:00")


def test_config_info_reports_manaus(tmp_path):
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    cfg = runner.config_info()
    assert cfg["tz"] == "America/Manaus"
    assert "Manaus" in cfg["tz_label"]
    assert cfg["today"] == timeutil.today()
    assert cfg["now"].endswith("-04:00")

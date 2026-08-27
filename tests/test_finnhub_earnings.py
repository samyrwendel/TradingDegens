"""Resultado reportado do âncora via Finnhub — o catalisador da leitura do Erick.

Sem rede: o histórico de surpresas e o calendário são injetados (monkeypatch do
único seam ``_finnhub_get``). Cobrem: escolher o trimestre reportado mais recente
já público, o date-guard por folga (backtest não vaza), a data real de anúncio do
calendário virando guarda exata, "bateu × ficou abaixo", ausência de chave =
indisponível (sem inventar), e a linha markdown.
"""
import pytest

from tradingagents.agents.utils.date_guard import base_date
from tradingagents.dataflows import finnhub_earnings as fe


# NVDA-ish surprise history (mais recente primeiro), period = fim do trimestre fiscal.
_HISTORY = [
    {"period": "2026-06-30", "actual": 1.87, "estimate": 1.79, "surprise": 0.08,
     "surprisePercent": 4.34, "quarter": 1, "year": 2027},
    {"period": "2026-03-31", "actual": 1.62, "estimate": 1.56, "surprise": 0.06,
     "surprisePercent": 3.62, "quarter": 4, "year": 2026},
    {"period": "2025-12-31", "actual": 0.90, "estimate": 1.00, "surprise": -0.10,
     "surprisePercent": -10.0, "quarter": 3, "year": 2026},
]


@pytest.fixture()
def has_key(monkeypatch):
    monkeypatch.setattr(fe, "get_api_key", lambda: "TESTKEY")


@pytest.fixture()
def no_calendar(monkeypatch):
    # Calendário grátis não cobre histórico → sem data de anúncio real.
    monkeypatch.setattr(fe, "_fetch_announce_date", lambda symbol, period_end: None)


@pytest.fixture()
def stub_history(monkeypatch):
    monkeypatch.setattr(fe, "_fetch_surprise_history", lambda symbol: [dict(r) for r in _HISTORY])


@pytest.mark.unit
def test_no_key_is_unavailable(monkeypatch):
    monkeypatch.setattr(fe, "get_api_key", lambda: None)
    assert fe.get_reported_earnings("NVDA", "2026-08-26") is None


@pytest.mark.unit
def test_live_run_takes_most_recent_beat(has_key, stub_history, no_calendar):
    ev = fe.get_reported_earnings("NVDA", "2026-08-26")
    assert ev is not None
    assert ev["period"] == "2026-06-30"
    assert ev["eps_actual"] == pytest.approx(1.87)
    assert ev["eps_estimate"] == pytest.approx(1.79)
    assert ev["surprise_pct"] == pytest.approx(4.34)
    assert ev["beat"] is True
    assert ev["announce_date"] is None  # calendário não cobriu → cita o trimestre


@pytest.mark.unit
def test_backtest_lag_guard_hides_not_yet_public(has_key, stub_history, no_calendar):
    # Backtest logo após o fim do trimestre (2026-07-10): o resultado de 2026-06-30
    # ainda NÃO era público (large cap divulga ~8 semanas depois) → cai no anterior.
    with base_date("2026-07-10"):
        ev = fe.get_reported_earnings("NVDA", "2026-07-10")
    assert ev is not None
    assert ev["period"] == "2026-03-31"  # o de junho fica escondido pela folga


@pytest.mark.unit
def test_real_announce_date_becomes_exact_guard(has_key, stub_history, monkeypatch):
    # Calendário devolve a data REAL de anúncio do trimestre de junho.
    def announce(symbol, period_end):
        from datetime import date
        if period_end == date(2026, 6, 30):
            return date(2026, 8, 26)
        return None

    monkeypatch.setattr(fe, "_fetch_announce_date", announce)
    # No dia do anúncio: aparece, com data exata e days_since 0 (recent).
    with base_date("2026-08-26"):
        ev = fe.get_reported_earnings("NVDA", "2026-08-26")
    assert ev["announce_date"] == "2026-08-26"
    assert ev["days_since"] == 0
    assert ev["recent"] is True
    # Um dia antes do anúncio: ainda não público → cai no trimestre anterior.
    with base_date("2026-08-25"):
        ev2 = fe.get_reported_earnings("NVDA", "2026-08-25")
    assert ev2["period"] == "2026-03-31"


@pytest.mark.unit
def test_miss_is_flagged(has_key, no_calendar, monkeypatch):
    monkeypatch.setattr(fe, "_fetch_surprise_history", lambda symbol: [dict(_HISTORY[2])])
    ev = fe.get_reported_earnings("NVDA", "2026-08-26")
    assert ev["beat"] is False
    assert ev["surprise_pct"] == pytest.approx(-10.0)


@pytest.mark.unit
def test_source_down_is_unavailable(has_key, monkeypatch):
    def boom(symbol):
        raise RuntimeError("finnhub 429")

    monkeypatch.setattr(fe, "_fetch_surprise_history", boom)
    assert fe.get_reported_earnings("NVDA", "2026-08-26") is None


@pytest.mark.unit
def test_format_line_beat_and_miss():
    beat = {"beat": True, "announce_date": "2026-08-26", "period": "2026-06-30",
            "eps_actual": 1.87, "eps_estimate": 1.79, "surprise_pct": 4.34,
            "quarter": 1, "year": 2027}
    line = fe.format_reported_line(beat)
    assert "✅" in line and "bateu" in line
    assert "1,87" in line and "1,79" in line and "+4,3%" in line
    assert "2026-08-26" in line

    miss = {"beat": False, "announce_date": None, "period": "2025-12-31",
            "eps_actual": 0.90, "eps_estimate": 1.00, "surprise_pct": -10.0,
            "quarter": 3, "year": 2026}
    line2 = fe.format_reported_line(miss)
    assert "❌" in line2 and "abaixo" in line2
    assert "trimestre fiscal encerrado em 2025-12-31" in line2


@pytest.mark.unit
def test_section_shows_reported_result(monkeypatch):
    from tradingagents.dataflows import earnings_calendar as ec

    monkeypatch.setattr(ec, "get_next_earnings", lambda sym, cd: None)
    monkeypatch.setattr(
        ec, "_reported_earnings",
        lambda sym, cd: {"beat": True, "announce_date": "2026-08-26",
                         "period": "2026-06-30", "eps_actual": 1.87,
                         "eps_estimate": 1.79, "surprise_pct": 4.34,
                         "quarter": 1, "year": 2027} if sym == "NVDA" else None,
    )
    section = ec.build_earnings_section("AVGO", "2026-08-26", "stock")
    assert "NVDA" in section and "✅" in section and "bateu" in section
    assert "reportado 1,87" in section

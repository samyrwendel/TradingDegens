"""Calendário de earnings — próxima data de resultado, cacheada e date-guarded.

Provedor novo (brief 24/08): o eixo da análise do Erick é o EVENTO ("resultado da
NVDA sai quarta 26/08"). Estes testes injetam um DataFrame de earnings falso (sem
rede) e checam: escolher a próxima data ESTRITAMENTE depois da base, o "após o
fechamento", o clamp do date_guard, a fonte caída virando "indisponível" (sem
inventar data), e o vazio em cripto.
"""
import pandas as pd
import pytest

from tradingagents.agents.utils.date_guard import base_date
from tradingagents.dataflows import earnings_calendar as ec


def _fake_df():
    idx = pd.to_datetime(
        ["2026-08-26 16:00", "2026-05-20 16:00", "2026-02-25 16:00"]
    ).tz_localize("America/New_York")
    return pd.DataFrame(
        {
            "EPS Estimate": [2.09, 1.77, 1.54],
            "Reported EPS": [float("nan"), 1.87, 1.62],
            "Surprise(%)": [float("nan"), 5.54, 5.32],
        },
        index=idx,
    )


class _FakeTicker:
    def __init__(self, symbol):
        self.symbol = symbol

    def get_earnings_dates(self, limit=16):
        return _fake_df()


@pytest.fixture()
def fake_yf(monkeypatch):
    import yfinance
    monkeypatch.setattr(yfinance, "Ticker", _FakeTicker)


# ------------------------------------------------------- próxima data ----------
@pytest.mark.unit
def test_next_earnings_after_base(fake_yf):
    ev = ec.get_next_earnings("NVDA", "2026-08-01")
    assert ev["date"] == "2026-08-26"
    assert ev["after_close"] is True
    assert ev["eps_estimate"] == pytest.approx(2.09)


@pytest.mark.unit
def test_skips_dates_at_or_before_base(fake_yf):
    # Base em junho: a de maio (05-20) fica pra trás; a próxima é a de agosto.
    ev = ec.get_next_earnings("NVDA", "2026-06-01")
    assert ev["date"] == "2026-08-26"


@pytest.mark.unit
def test_date_guard_clamps_curr_date(fake_yf):
    # Base pinada em junho; um curr_date no futuro é clampado -> não "vê" além.
    with base_date("2026-06-01"):
        ev = ec.get_next_earnings("NVDA", "2026-12-31")
    assert ev["date"] == "2026-08-26"


@pytest.mark.unit
def test_source_down_is_unavailable_not_invented(monkeypatch):
    def boom(symbol, base):
        raise RuntimeError("yahoo 429")

    monkeypatch.setattr(ec, "_fetch_next_earnings", boom)
    ev = ec.get_next_earnings("NVDA", "2026-08-01")
    assert ev is None


@pytest.mark.unit
def test_no_future_event_returns_none(fake_yf):
    # Base depois da última data conhecida -> não há próximo evento.
    ev = ec.get_next_earnings("NVDA", "2027-01-01")
    assert ev is None


# -------------------------------------------------------------- seção ----------
@pytest.mark.unit
def test_section_shows_symbol_and_anchor(fake_yf):
    section = ec.build_earnings_section("AMD", "2026-08-01", "stock")
    assert "Calendário de earnings" in section
    assert "AMD" in section and "NVDA" in section
    assert "2026-08-26" in section


@pytest.mark.unit
def test_section_unavailable_wording(monkeypatch):
    monkeypatch.setattr(ec, "get_next_earnings", lambda sym, cd: None)
    section = ec.build_earnings_section("AMD", "2026-08-01", "stock")
    assert "indisponível" in section.lower()
    assert "inventada" in section.lower()


@pytest.mark.unit
def test_section_empty_for_crypto():
    assert ec.build_earnings_section("BTC-USD", "2026-08-01", "crypto") == ""


# ----------------------------------------------------------- coverage ----------
@pytest.mark.unit
def test_coverage_skips_when_empty(monkeypatch):
    from tradingagents.agents.utils import earnings_coverage as ecov

    monkeypatch.setattr(ecov, "build_earnings_section", lambda *a, **k: "")
    # Cripto (seção vazia): não anexa, relatório intacto.
    assert ecov.ensure_earnings_coverage("intacto", "BTC-USD", "2026-08-01", "crypto") == "intacto"


@pytest.mark.unit
def test_coverage_appends_and_fail_open(monkeypatch):
    from tradingagents.agents.utils import earnings_coverage as ecov

    monkeypatch.setattr(ecov, "build_earnings_section", lambda *a, **k: "## 📅 Earnings")
    out = ecov.ensure_earnings_coverage("corpo", "AMD", "2026-08-01", "stock")
    assert "corpo" in out and "Earnings" in out

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(ecov, "build_earnings_section", boom)
    assert ecov.ensure_earnings_coverage("intacto", "AMD", "2026-08-01", "stock") == "intacto"

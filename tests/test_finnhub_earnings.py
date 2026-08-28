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
    # Calendário grátis não cobre histórico → sem data de anúncio real (nem ancorado no
    # fim de trimestre, nem ancorado em curr_date) → cai na história de surpresas + folga.
    monkeypatch.setattr(fe, "_fetch_announce_date", lambda symbol, period_end: None)
    monkeypatch.setattr(fe, "_fetch_recent_announcement", lambda symbol, base: None)


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
    # Calendário ancorado em curr_date não cobre (backtest) → exercita o fallback + a
    # guarda exata por _fetch_announce_date (por período).
    monkeypatch.setattr(fe, "_fetch_recent_announcement", lambda symbol, base: None)
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


# Ano fiscal DESLOCADO (NVDA): o Finnhub rotula o Q2 já reportado com period ~1 ano à
# frente ('2027-06-30'); o Q1 anterior fica '2026-06-30'. O calendário traz a data real.
_HISTORY_SHIFTED = [
    {"period": "2027-06-30", "actual": 2.22, "estimate": 2.1384, "surprise": 0.0816,
     "surprisePercent": 3.8159, "quarter": 2, "year": 2027},
    {"period": "2026-06-30", "actual": 1.87, "estimate": 1.7922, "surprise": 0.0778,
     "surprisePercent": 4.341, "quarter": 1, "year": 2027},
]


@pytest.mark.unit
def test_shifted_fiscal_uses_real_announce_not_period_end(has_key, monkeypatch):
    """NVDA — ano fiscal DESLOCADO: o report certo (EPS 2,22, divulgado 26/ago) tem
    period fiscal '2027-06-30' (à frente). A seleção pela DATA REAL de divulgação o
    escolhe — days_since/recent pela divulgação, não pelo fim de trimestre — e expõe a
    receita, em vez de cair no de um ano atrás (1,87)."""
    from datetime import date

    monkeypatch.setattr(fe, "_fetch_surprise_history",
                        lambda symbol: [dict(r) for r in _HISTORY_SHIFTED])
    monkeypatch.setattr(fe, "_fetch_recent_announcement", lambda symbol, base: {
        "date": date(2026, 8, 26), "eps_actual": 2.22, "eps_estimate": 2.1384,
        "revenue_actual": 96_221_000_000, "revenue_estimate": 94_008_645_045,
        "quarter": 2, "year": 2027,
    })
    with base_date("2026-08-27"):
        ev = fe.get_reported_earnings("NVDA", "2026-08-27")
    assert ev["eps_actual"] == pytest.approx(2.22)   # o report certo, não 1,87
    assert ev["announce_date"] == "2026-08-26"
    assert ev["days_since"] == 1 and ev["recent"] is True
    assert ev["period"] == "2027-06-30" and ev["quarter"] == 2 and ev["year"] == 2027
    assert ev["beat"] is True
    assert ev["revenue_actual"] == pytest.approx(96_221_000_000)
    assert ev["revenue_surprise_pct"] == pytest.approx(2.353, abs=0.02)
    line = fe.format_reported_line(ev)
    assert "2,22" in line and "96,22 B" in line and "receita" in line


@pytest.mark.unit
def test_normal_fiscal_calendar_exposes_revenue(has_key, monkeypatch):
    """Fiscal normal (MSFT): o calendário traz a data real + receita reportada × estimada;
    days_since 29 (não recente pelos 14d, mas bateu)."""
    from datetime import date

    hist = [{"period": "2026-06-30", "actual": 4.74, "estimate": 4.3274,
             "surprise": 0.41, "surprisePercent": 9.53, "quarter": 4, "year": 2026}]
    monkeypatch.setattr(fe, "_fetch_surprise_history", lambda symbol: [dict(r) for r in hist])
    monkeypatch.setattr(fe, "_fetch_recent_announcement", lambda symbol, base: {
        "date": date(2026, 7, 29), "eps_actual": 4.74, "eps_estimate": 4.3274,
        "revenue_actual": 90_007_000_000, "revenue_estimate": 89_373_722_644,
        "quarter": 4, "year": 2026,
    })
    with base_date("2026-08-27"):
        ev = fe.get_reported_earnings("MSFT", "2026-08-27")
    assert ev["announce_date"] == "2026-07-29" and ev["days_since"] == 29
    assert ev["recent"] is False and ev["beat"] is True
    assert ev["revenue_actual"] == pytest.approx(90_007_000_000)
    assert ev["revenue_estimate"] == pytest.approx(89_373_722_644)
    assert ev["revenue_surprise_pct"] is not None


@pytest.mark.unit
def test_recent_announcement_excludes_future_and_picks_latest(monkeypatch):
    """``_fetch_recent_announcement``: só linhas com ``epsActual`` e ``date <= base``;
    devolve a MAIS RECENTE. Anti-look-ahead REAL — divulgação depois de base não entra."""
    from datetime import date

    calendar = {"earningsCalendar": [
        {"symbol": "NVDA", "date": "2026-08-26", "epsActual": 2.22, "epsEstimate": 2.1384,
         "revenueActual": 96_221_000_000, "revenueEstimate": 94_008_645_045,
         "quarter": 2, "year": 2027},
        {"symbol": "NVDA", "date": "2026-05-27", "epsActual": 1.87, "epsEstimate": 1.7922,
         "revenueActual": 80_000_000_000, "revenueEstimate": 79_000_000_000,
         "quarter": 1, "year": 2027},
        {"symbol": "NVDA", "date": "2026-11-19", "epsActual": None, "epsEstimate": 2.5,
         "quarter": 3, "year": 2027},   # futuro, sem actual → ignorado
    ]}
    monkeypatch.setattr(fe, "_finnhub_get", lambda path, params: calendar)
    ann = fe._fetch_recent_announcement("NVDA", date(2026, 8, 27))
    assert ann["date"] == date(2026, 8, 26) and ann["eps_actual"] == pytest.approx(2.22)
    assert ann["revenue_actual"] == 96_221_000_000
    # base ANTES do report de agosto: pega o de maio, NÃO vaza o de agosto
    ann2 = fe._fetch_recent_announcement("NVDA", date(2026, 6, 1))
    assert ann2["date"] == date(2026, 5, 27) and ann2["eps_actual"] == pytest.approx(1.87)


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

    monkeypatch.setattr(ec, "get_next_earnings_status",
                        lambda sym, cd: (None, ec.STATUS_SEM_AGENDA))
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

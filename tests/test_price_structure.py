"""Deterministic price-structure / setup detection (fork brief 23/08).

The market analyst describes indicators but never identifies the setup the
product owner trades: a pullback-to-a-rising-average buy region and the 1-2-3
reversal. These tests pin the detector on a synthetic series with a KNOWN
structure, so they run offline and can't drift — and they enforce the two hard
rules: nothing is fabricated (every point comes from the series) and nothing sees
a future candle (date guard).
"""
import json

import pandas as pd
import pytest

from tradingagents.dataflows import price_structure as ps
from tradingagents.dataflows.intraday import IntradayUnavailableError
from tradingagents.agents.utils.price_structure_coverage import (
    ensure_price_structure_coverage,
)


def _frame() -> pd.DataFrame:
    """A smooth uptrend with a shallow pullback to MA20 (a buy region) followed by
    a clean 1-2-3: low -> high -> higher-low -> breakout above the high."""
    closes: list[float] = []
    closes += [100 + i for i in range(60)]        # uptrend 100 -> 159
    closes += [156, 150, 146, 150, 156]           # pullback to ~MA20, bounce
    closes += [162 + i for i in range(20)]        # resume up 162 -> 181
    closes += [175, 168, 160, 150, 145]           # down to L1 (~145)
    closes += [150, 160, 172, 185, 190]           # up to H / P2 (~190)
    closes += [182, 175, 168, 162, 166]           # down to L3 (~162) > L1
    closes += [192 + i for i in range(15)]        # breakout above the high
    dates = pd.bdate_range("2025-01-01", periods=len(closes))
    c = pd.Series(closes, dtype=float)
    return pd.DataFrame({
        "Date": dates.strftime("%Y-%m-%d"),
        "Open": c.shift(1).fillna(c).values,
        "High": (c * 1.01).values,
        "Low": (c * 0.99).values,
        "Close": c.values,
        "Volume": [1000] * len(c),
    })


def _top_frame() -> pd.DataFrame:
    """Vertical mirror of :func:`_frame` (closes reflected around 300): a smooth
    DOWNTREND with a clean 1-2-3 de venda — high -> low -> lower-high -> breakdown
    below point 2's low. The product owner trades short, so this must be detected
    just like the bottom reversal is."""
    buy: list[float] = []
    buy += [100 + i for i in range(60)]
    buy += [156, 150, 146, 150, 156]
    buy += [162 + i for i in range(20)]
    buy += [175, 168, 160, 150, 145]
    buy += [150, 160, 172, 185, 190]
    buy += [182, 175, 168, 162, 166]
    buy += [192 + i for i in range(15)]
    closes = [300 - c for c in buy]  # reflect: swing lows become swing highs
    dates = pd.bdate_range("2025-01-01", periods=len(closes))
    c = pd.Series(closes, dtype=float)
    return pd.DataFrame({
        "Date": dates.strftime("%Y-%m-%d"),
        "Open": c.shift(1).fillna(c).values,
        "High": (c * 1.01).values,
        "Low": (c * 0.99).values,
        "Close": c.values,
        "Volume": [1000] * len(c),
    })


@pytest.fixture
def synth(monkeypatch):
    df = _frame()
    monkeypatch.setattr(ps, "load_ohlcv", lambda symbol, curr_date: df.copy())
    return df


@pytest.fixture
def top(monkeypatch):
    df = _top_frame()
    monkeypatch.setattr(ps, "load_ohlcv", lambda symbol, curr_date: df.copy())
    return df


CURR = "2025-12-31"  # after the whole frame; load_ohlcv (mocked) returns it all


@pytest.mark.unit
def test_detects_123_reversal(synth):
    s = ps.detect_price_structure("SYN", CURR)
    assert s.pattern is not None
    p = s.pattern
    # ascending bottom + trigger is point 2's high, and it broke out -> acionado
    assert p.direction == "compra"
    assert p.p3["price"] > p.p1["price"]
    assert p.trigger == p.p2["price"]
    assert p.state == "acionado"
    # points are ordered in time
    assert p.p1["date"] < p.p2["date"] < p.p3["date"]


@pytest.mark.unit
def test_detects_123_de_venda(top):
    """Descending-top 1-2-3: H -> L -> lower H, trigger = break BELOW point 2's low."""
    s = ps.detect_price_structure("SYN", CURR)
    assert s.pattern is not None
    p = s.pattern
    assert p.direction == "venda"
    # descending top: point 3's high sits BELOW point 1's high
    assert p.p3["price"] < p.p1["price"]
    # trigger is point 2's LOW (break below it), and the series broke down -> acionado
    assert p.trigger == p.p2["price"]
    assert p.state == "acionado"
    assert p.p1["date"] < p.p2["date"] < p.p3["date"]
    # nothing fabricated: every reported price is a real high/low of the series
    df = _top_frame()
    prices = set(round(float(v), 2) for v in df["High"]) | set(round(float(v), 2) for v in df["Low"])
    for pt in (p.p1, p.p2, p.p3):
        assert pt["price"] in prices
    assert p.trigger in prices


@pytest.mark.unit
def test_venda_section_and_plan_carry_direction(top):
    section = ps.build_price_structure_section("SYN", CURR)
    assert "Padrão 1-2-3 de venda" in section
    assert "topo descendente" in section
    plan = ps.build_actionable_plan("SYN", CURR)
    assert plan.pattern is not None and plan.pattern["direction"] == "venda"


@pytest.mark.unit
def test_detects_buy_region_at_average(synth):
    s = ps.detect_price_structure("SYN", CURR)
    assert s.buy_regions, "the pullback to MA20 must surface as a buy region"
    r = s.buy_regions[0]
    assert r.ma_label.startswith("MMS")
    # the region reacted up afterwards (the series bounces)
    assert r.reaction_pct is not None and r.reaction_pct > 0


# ------------------------------------ estrutura CIENTE DO MÉTODO (task 031) ---
@pytest.mark.unit
def test_method_config_maps():
    """Padrão lê MMS (janela larga), Erick lê EMA 8/21 (timing curto)."""
    assert ps._method_mas("padrao") == (("MMS20", "MA20"), ("MMS50", "MA50"), ("MMS200", "MA200"))
    assert ps._method_mas("erick") == (("EMA8", "EMA8"), ("EMA21", "EMA21"))
    assert ps._method_k("padrao") == ps._SWING_K
    assert ps._method_k("erick") == 3
    # método desconhecido/ausente cai no Padrão (default seguro)
    assert ps._method_mas("qualquer") == ps._method_mas("padrao")
    assert ps._method_k(None) == ps._SWING_K


@pytest.mark.unit
def test_padrao_default_is_unchanged(synth):
    """O caminho Padrão (default) é idêntico ao de antes: MMS + k padrão. Sem o
    argumento method, tem que dar exatamente o mesmo que method='padrao'."""
    default = ps.detect_price_structure("SYN", CURR).as_dict()
    padrao = ps.detect_price_structure("SYN", CURR, method="padrao").as_dict()
    assert default == padrao
    for r in ps.detect_price_structure("SYN", CURR, method="padrao").buy_regions:
        assert r.ma_label.startswith("MMS")
    act = ps.detect_price_structure("SYN", CURR, method="padrao").active_region
    if act:
        assert act.ma_label.startswith("MMS")


@pytest.mark.unit
def test_erick_keys_on_ema(synth):
    """No método Erick a região/recuo sai da EMA 8/21 — rótulos EMA, nunca MMS."""
    s = ps.detect_price_structure("SYN", CURR, method="erick")
    for r in s.buy_regions:
        assert r.ma_label.startswith("EMA")
    if s.active_region:
        assert s.active_region.ma_label.startswith("EMA")
    # a família de médias do recuo mudou de verdade
    labels_padrao = {r.ma_label for r in ps.detect_price_structure("SYN", CURR, "1d", "padrao").buy_regions}
    labels_erick = {r.ma_label for r in s.buy_regions}
    assert not (labels_padrao & labels_erick) or not labels_padrao or not labels_erick


@pytest.mark.unit
def test_actionable_plan_method_aware(synth):
    """O plano operável do Erick ancora o buy_zone numa EMA; o do Padrão numa MMS."""
    padrao = ps.build_actionable_plan_dict("SYN", CURR, method="padrao")
    erick = ps.build_actionable_plan_dict("SYN", CURR, method="erick")
    bz_p = (padrao.get("buy_zone") or {}).get("label", "")
    bz_e = (erick.get("buy_zone") or {}).get("label", "")
    if bz_p:
        assert "MMS" in bz_p
    if bz_e:
        assert "EMA" in bz_e


@pytest.mark.unit
def test_chart_markers_method_aware(synth):
    """As velas e AS DUAS famílias de média sempre são desenhadas; só os MARCADORES
    (região/1-2-3) seguem o método — então o Erick pode marcar EMA."""
    ch_p = ps.build_price_chart("SYN", CURR, method="padrao")
    ch_e = ps.build_price_chart("SYN", CURR, method="erick")
    # o payload de candles/médias é o mesmo (só marcadores mudam)
    assert ch_p["candles"] == ch_e["candles"]
    assert set(ch_p["ma"]) == {"20", "50", "200"} and set(ch_p["ema"]) == {"8", "21", "50"}
    regs_e = ch_e["markers"]["buy_regions"]
    for r in regs_e:
        assert r["ma_label"].startswith("EMA")


@pytest.mark.unit
def test_nothing_is_fabricated(synth):
    """Every reported price is a real high/low of the series; every date is real."""
    df = _frame()
    dates = set(df["Date"])
    prices = set(round(float(v), 2) for v in df["High"]) | set(round(float(v), 2) for v in df["Low"])
    s = ps.detect_price_structure("SYN", CURR)
    for r in s.buy_regions:
        assert r.date in dates
        assert r.low in prices
    if s.pattern:
        for pt in (s.pattern.p1, s.pattern.p2, s.pattern.p3):
            assert pt["date"] in dates
            assert pt["price"] in prices
        assert s.pattern.trigger in prices


@pytest.mark.unit
def test_date_guard_no_future_point(monkeypatch):
    """Detection on a past date only sees bars up to it — the mocked load_ohlcv
    returns a frame cut at ``cut``; no reported date may exceed it."""
    df = _frame()
    cut = df["Date"].iloc[80]  # pretend "today" is bar 80
    cut_df = df[df["Date"] <= cut].reset_index(drop=True)
    monkeypatch.setattr(ps, "load_ohlcv", lambda symbol, curr_date: cut_df.copy())
    s = ps.detect_price_structure("SYN", cut)
    chart = ps.build_price_chart("SYN", cut)
    assert chart["candles"][-1]["d"] <= cut
    for r in s.buy_regions:
        assert r.date <= cut
    if s.pattern:
        for pt in (s.pattern.p1, s.pattern.p2, s.pattern.p3):
            assert pt["date"] <= cut


@pytest.mark.unit
def test_thin_series_reports_no_setup(monkeypatch):
    tiny = _frame().head(3).reset_index(drop=True)
    monkeypatch.setattr(ps, "load_ohlcv", lambda symbol, curr_date: tiny.copy())
    s = ps.detect_price_structure("SYN", CURR)
    assert not s.buy_regions and s.pattern is None
    section = ps.build_price_structure_section("SYN", CURR)
    assert "## Estrutura de preço / setups" in section
    assert "nenhum setup identificado" in section.lower()


@pytest.mark.unit
def test_section_data_failure_is_graceful(monkeypatch):
    def boom(symbol, curr_date):
        raise RuntimeError("no data")
    monkeypatch.setattr(ps, "load_ohlcv", boom)
    section = ps.build_price_structure_section("SYN", CURR)
    assert "## Estrutura de preço / setups" in section
    assert "indisponível" in section.lower()
    # the section is always shown (never omitted) — on a data failure the guard
    # appends the explicit "indisponível" note, not a fabricated read.
    out = ensure_price_structure_coverage("CORPO", "SYN", CURR)
    assert out.startswith("CORPO")
    assert "indisponível" in out.lower()


@pytest.mark.unit
def test_section_lists_region_and_pattern(synth):
    section = ps.build_price_structure_section("SYN", CURR)
    assert "Regiões de compra na média" in section
    assert "Padrão 1-2-3 de compra" in section
    assert "Ponto 1" in section and "Gatilho" in section


@pytest.mark.unit
def test_ensure_appends_section(synth):
    out = ensure_price_structure_coverage("Relatório do analista.", "SYN", CURR)
    assert out.startswith("Relatório do analista.")
    assert "## Estrutura de preço / setups" in out


@pytest.mark.unit
def test_chart_payload_is_serializable_and_aligned(synth):
    chart = ps.build_price_chart("SYN", CURR)
    json.dumps(chart)  # must not raise
    n = len(chart["candles"])
    assert n > 2
    for w in ("20", "50", "200"):
        assert len(chart["ma"][w]) == n
    markers = chart["markers"]
    assert set(markers) == {"buy_regions", "active_region", "pattern_123"}
    # markers reference candles inside the window
    window = {c["d"] for c in chart["candles"]}
    for r in markers["buy_regions"]:
        assert r["date"] in window


# --------------------------------------------------------------- EMA -----------
@pytest.mark.unit
def test_chart_carries_ema_alongside_mms(synth):
    """EMA 8/21/50 are computed and charted ALONGSIDE the simple averages — both
    families present, aligned to the candles (fork brief 24/08, criterion 1)."""
    chart = ps.build_price_chart("SYN", CURR)
    json.dumps(chart)  # still serializable with the extra series
    n = len(chart["candles"])
    assert chart["ema_windows"] == [8, 21, 50]
    for w in ("8", "21", "50"):
        assert len(chart["ema"][w]) == n
    # the simple averages are still there too — EMA is additive, not a replacement
    for w in ("20", "50", "200"):
        assert len(chart["ma"][w]) == n
    # a fast EMA has a real value early (ewm needs no full window), unlike MMS200
    assert any(v is not None for v in chart["ema"]["8"][:10])


# ------------------------------------------------------ intraday timeframe -----
def _intraday_frame() -> pd.DataFrame:
    """The same known 1-2-3 structure as :func:`_frame`, but on a 15-minute clock
    so the detector is exercised on an intraday series."""
    base = _frame()
    ts = pd.date_range("2026-08-20 00:00", periods=len(base), freq="15min")
    out = base.copy()
    out["Date"] = ts.strftime("%Y-%m-%d %H:%M")
    return out


@pytest.mark.unit
def test_intraday_crypto_structure_runs(monkeypatch):
    """Region + 1-2-3 run on a 15m crypto series, and every point carries the
    time-of-day (not just a date) so intraday bars stay distinct (criterion 2)."""
    df = _intraday_frame()
    monkeypatch.setattr(ps, "load_intraday_ohlcv", lambda symbol, curr_date, tf: df.copy())
    s = ps.detect_price_structure("BTC-USD", "2026-08-20", "15m")
    assert s.pattern is not None
    # intraday points are stamped with HH:MM, not a bare date
    assert ":" in s.pattern.p1["date"]
    chart = ps.build_price_chart("BTC-USD", "2026-08-20", timeframe="15m")
    assert chart["timeframe"] == "15m"
    assert ":" in chart["candles"][-1]["d"]
    assert chart["ema_windows"] == [8, 21, 50]
    section = ps.build_price_structure_section("BTC-USD", "2026-08-20", "15m")
    assert "15 minutos" in section


@pytest.mark.unit
def test_4h_timeframe_runs_and_is_labelled(monkeypatch):
    """The 4h frame (native exchange candle, task 005) runs the detector and stamps
    '4 horas' on the section/chart/plan — Erick decides the 1-2-3 on 15m/4h."""
    base = _frame()
    ts = pd.date_range("2026-08-01 00:00", periods=len(base), freq="4h")
    df = base.copy()
    df["Date"] = ts.strftime("%Y-%m-%d %H:%M")
    monkeypatch.setattr(ps, "load_intraday_ohlcv", lambda symbol, curr_date, tf: df.copy())

    s = ps.detect_price_structure("BTC-USD", "2026-08-20", "4h")
    assert s.pattern is not None
    assert ":" in s.pattern.p1["date"]           # intraday stamp, not a bare date

    chart = ps.build_price_chart("BTC-USD", "2026-08-20", timeframe="4h")
    assert chart["timeframe"] == "4h"

    section = ps.build_price_structure_section("BTC-USD", "2026-08-20", "4h")
    assert "4 horas" in section
    plan = ps.build_actionable_plan("BTC-USD", "2026-08-20", "4h")
    assert "4 horas" in plan.timeframe


@pytest.mark.unit
def test_intraday_date_guard_no_future_bar(monkeypatch):
    """Detection on a past intraday date only sees bars up to it (criterion 4)."""
    df = _intraday_frame()
    cut = df["Date"].iloc[80]
    cut_df = df[df["Date"] <= cut].reset_index(drop=True)
    monkeypatch.setattr(ps, "load_intraday_ohlcv", lambda symbol, curr_date, tf: cut_df.copy())
    s = ps.detect_price_structure("BTC-USD", cut, "15m")
    for r in s.buy_regions:
        assert r.date <= cut
    if s.pattern:
        for pt in (s.pattern.p1, s.pattern.p2, s.pattern.p3):
            assert pt["date"] <= cut


@pytest.mark.unit
def test_intraday_unavailable_declared_not_fabricated(monkeypatch):
    """A non-crypto intraday request declares it unavailable — no invented bar
    (criterion 3). The loader raises; section + plan both say so plainly."""
    def boom(symbol, curr_date, tf):
        raise IntradayUnavailableError(symbol, None, "sem candle intradiário keyless")

    monkeypatch.setattr(ps, "load_intraday_ohlcv", boom)

    section = ps.build_price_structure_section("AAPL", "2026-08-20", "15m")
    assert "indisponível" in section.lower()
    assert "inventado" in section.lower()

    plan = ps.build_actionable_plan("AAPL", "2026-08-20", "15m")
    assert plan.setup_state == "intradiario_indisponivel"
    assert plan.buy_zone is None and plan.price is None
    d = ps.build_actionable_plan_dict("AAPL", "2026-08-20", "15m")
    assert d["setup_state"] == "intradiario_indisponivel"


# ------------------------------------------------------ weekly timeframe (007) --
def _weekly_frame() -> pd.DataFrame:
    """The known 1-2-3 structure from :func:`_frame`, but with each bar one calendar
    week apart (Mondays), so a ``W-SUN`` resample maps exactly one bar per week and
    preserves the structure on the weekly frame — the detector must find it there."""
    base = _frame()
    dates = pd.date_range("2022-01-03", periods=len(base), freq="7D")  # Mondays
    out = base.copy()
    out["Date"] = dates.strftime("%Y-%m-%d")
    return out


@pytest.mark.unit
def test_weekly_structure_runs_and_is_labelled(monkeypatch):
    """Region + 1-2-3 recompute on the resampled WEEKLY series (crypto), the frame
    is stamped 'semanal' on section + plan, and weekly points are date-only —
    proving the selector's Semanal button drives a real weekly recompute (task 007)."""
    df = _weekly_frame()
    monkeypatch.setattr(ps, "load_ohlcv", lambda symbol, curr_date: df.copy())
    curr = "2026-12-31"  # after the whole frame

    s = ps.detect_price_structure("BTC-USD", curr, "1w")
    assert s.pattern is not None
    assert ":" not in s.pattern.p1["date"]        # weekly is a bare date, no HH:MM

    chart = ps.build_price_chart("BTC-USD", curr, timeframe="1w")
    assert chart["timeframe"] == "1w"
    assert ":" not in chart["candles"][-1]["d"]
    # one bar per week: far fewer candles than the daily source would carry
    assert 0 < len(chart["candles"]) <= len(df)

    section = ps.build_price_structure_section("BTC-USD", curr, "1w")
    assert "semanal" in section.lower()
    plan = ps.build_actionable_plan("BTC-USD", curr, "1w")
    assert "semanal" in plan.timeframe.lower()


@pytest.mark.unit
def test_weekly_works_for_equity(monkeypatch):
    """Weekly is resampled from the daily series, so it is operable for an EQUITY too
    (unlike intraday). No IntradayUnavailableError, a real weekly chart (task 007)."""
    df = _weekly_frame()
    monkeypatch.setattr(ps, "load_ohlcv", lambda symbol, curr_date: df.copy())
    # If the weekly path wrongly reached the intraday loader, this would raise.
    monkeypatch.setattr(ps, "load_intraday_ohlcv",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("weekly must not hit intraday")))
    chart = ps.build_price_chart("AAPL", "2026-12-31", timeframe="1w")
    assert chart["timeframe"] == "1w"
    assert chart["candles"]                        # weekly candles exist for a stock
    section = ps.build_price_structure_section("AAPL", "2026-12-31", "1w")
    assert "indisponível" not in section.lower()   # not the intraday-unavailable read
    assert "semanal" in section.lower()


@pytest.mark.unit
def test_weekly_resample_aggregates_and_guards_forming_week(monkeypatch):
    """A W-SUN resample aggregates the days in each week (first open, max high, min
    low, last close) and the date guard drops the still-forming current week — a
    weekly candle whose ending Sunday is after curr_date never appears (task 007)."""
    # Mon 2025-01-06 .. Wed 2025-01-29: three full weeks + a partial current week.
    dates = pd.date_range("2025-01-06", "2025-01-29", freq="D")
    n = len(dates)
    close = pd.Series(range(100, 100 + n), dtype=float)
    daily = pd.DataFrame({
        "Date": dates.strftime("%Y-%m-%d"),
        "Open": close.values,
        "High": (close + 2).values,
        "Low": (close - 2).values,
        "Close": close.values,
        "Volume": [10] * n,
    })
    monkeypatch.setattr(ps, "load_ohlcv", lambda symbol, curr_date: daily.copy())

    curr = "2025-01-29"  # a Wednesday — its week (ending Sun 2025-02-02) is not closed
    weekly = ps._load_frame("X", curr, "1w")
    weekly["Date"] = pd.to_datetime(weekly["Date"])
    weeks = set(weekly["Date"])

    # date guard: the forming week (Sun 02-02, in the future vs curr) is dropped;
    # the last visible weekly bar is the last CLOSED week (Sun 01-26).
    assert pd.Timestamp("2025-02-02") not in weeks
    assert weekly["Date"].max() == pd.Timestamp("2025-01-26")
    assert (weekly["Date"] <= pd.Timestamp(curr)).all()

    # aggregation of the first full week (Mon 01-06 .. Sun 01-12).
    row = weekly.set_index("Date").loc[pd.Timestamp("2025-01-12")]
    span = daily[(daily["Date"] >= "2025-01-06") & (daily["Date"] <= "2025-01-12")]
    assert row["Open"] == float(span["Open"].iloc[0])
    assert row["High"] == float(span["High"].max())
    assert row["Low"] == float(span["Low"].min())
    assert row["Close"] == float(span["Close"].iloc[-1])

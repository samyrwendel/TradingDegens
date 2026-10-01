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

from tradingagents.agents.utils.price_structure_coverage import (
    ensure_price_structure_coverage,
)
from tradingagents.dataflows import price_structure as ps
from tradingagents.dataflows.intraday import IntradayUnavailableError


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
    """Padrão lê MMS (janela larga), analista lê EMA 8/21 (timing curto)."""
    assert ps._method_mas("padrao") == (("MMS20", "MA20"), ("MMS50", "MA50"), ("MMS200", "MA200"))
    assert ps._method_mas("analista") == (("EMA8", "EMA8"), ("EMA21", "EMA21"))
    assert ps._method_k("padrao") == ps._SWING_K
    assert ps._method_k("analista") == 3
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
def test_analista_keys_on_ema(synth):
    """No método do analista a região/recuo sai da EMA 8/21 — rótulos EMA, nunca MMS."""
    s = ps.detect_price_structure("SYN", CURR, method="analista")
    for r in s.buy_regions:
        assert r.ma_label.startswith("EMA")
    if s.active_region:
        assert s.active_region.ma_label.startswith("EMA")
    # a família de médias do recuo mudou de verdade
    labels_padrao = {r.ma_label for r in ps.detect_price_structure("SYN", CURR, "1d", "padrao").buy_regions}
    labels_analista = {r.ma_label for r in s.buy_regions}
    assert not (labels_padrao & labels_analista) or not labels_padrao or not labels_analista


@pytest.mark.unit
def test_actionable_plan_method_aware(synth):
    """O plano operável do analista ancora o buy_zone numa EMA; o do Padrão numa MMS."""
    padrao = ps.build_actionable_plan_dict("SYN", CURR, method="padrao")
    analista = ps.build_actionable_plan_dict("SYN", CURR, method="analista")
    bz_p = (padrao.get("buy_zone") or {}).get("label", "")
    bz_e = (analista.get("buy_zone") or {}).get("label", "")
    if bz_p:
        assert "MMS" in bz_p
    if bz_e:
        assert "EMA" in bz_e


@pytest.mark.unit
def test_chart_markers_method_aware(synth):
    """As velas e AS DUAS famílias de média sempre são desenhadas; só os marcadores
    de REGIÃO de compra seguem o método (analista marca EMA). O 1-2-3 é canônico e
    igual pros dois — ver test_123_pattern_is_method_independent."""
    ch_p = ps.build_price_chart("SYN", CURR, method="padrao")
    ch_e = ps.build_price_chart("SYN", CURR, method="analista")
    # o payload de candles/médias é o mesmo (só marcadores mudam)
    assert ch_p["candles"] == ch_e["candles"]
    assert set(ch_p["ma"]) == {"20", "50", "200"} and set(ch_p["ema"]) == {"8", "21", "50"}
    regs_e = ch_e["markers"]["buy_regions"]
    for r in regs_e:
        assert r["ma_label"].startswith("EMA")


def _jiggle_frame() -> pd.DataFrame:
    """A series whose MAJOR swings sit ~8 bars apart but with small +/- jiggles every
    ~3 bars, so the tight (analista, k=3) swing horizon registers many extra pivots the
    canonical (Padrão, k=5) horizon smooths away. That divergence is exactly what let
    an analista run draw a different 1-2-3 than the canonical report text (AAOI: chart
    gatilho 91,50 vs texto 160,87)."""
    import math
    closes: list[float] = []
    for blk in range(9):
        base = 100 + (blk % 2) * 40             # alternate 100 / 140 major swing
        for i in range(8):
            closes.append(base + (i - 4) * 3 + 6 * math.sin(i * 1.1))
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


@pytest.mark.unit
def test_123_pattern_is_method_independent(synth):
    """The 1-2-3 is a pure price-SWING structure, not "on the EMA" or "on the MMS";
    it must be IDENTICAL across methods so the report text (always canonical), the
    chart annotation and the actionable plan can never disagree on the trigger. Bug:
    an analista run drew the tighter-swing 1-2-3 on the chart while the report text read
    the canonical one — two triggers for "the" pattern."""
    p = ps.detect_price_structure("SYN", CURR, method="padrao").pattern
    e = ps.detect_price_structure("SYN", CURR, method="analista").pattern
    assert p is not None and e is not None
    assert p.as_dict() == e.as_dict()
    # chart + actionable key on the SAME canonical trigger whatever the method
    ch_e = ps.build_price_chart("SYN", CURR, method="analista")
    assert ch_e["markers"]["pattern_123"]["trigger"] == p.trigger
    plan_e = ps.build_actionable_plan_dict("SYN", CURR, method="analista")
    assert (plan_e.get("pattern") or {}).get("trigger") == p.trigger
    # task 031 preserved: buy-region markers still key on the method's MA family
    labels_e = {r["ma_label"] for r in ch_e["markers"]["buy_regions"]}
    assert not labels_e or all(lbl.startswith("EMA") for lbl in labels_e)


@pytest.mark.unit
def test_nothing_is_fabricated(synth):
    """Every reported price is a real high/low of the series; every date is real."""
    df = _frame()
    dates = set(df["Date"])
    prices = set(round(float(v), 2) for v in df["High"]) | set(round(float(v), 2) for v in df["Low"])
    closes = set(round(float(v), 2) for v in df["Close"])
    s = ps.detect_price_structure("SYN", CURR)
    for r in s.buy_regions:
        assert r.date in dates
        assert r.low in prices
    if s.pattern:   # Setup123 do motor: velas 1/2a pela mínima, vela 3 pelo fechamento
        for pt in (s.pattern.p1, s.pattern.p2):
            assert pt["date"] in dates
            assert pt["price"] in prices
        if s.pattern.p3:
            assert s.pattern.p3["date"] in dates and s.pattern.p3["price"] in closes
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
            assert pt is None or pt["date"] <= cut


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
    # "Regiões de compra na média" virou "Regiões de recuo à média": o relatório
    # trazia as duas seções chamando o setup de "compra", e o 1-2-3 de compra
    # logo abaixo é OUTRO setup (ver tests/test_setup_naming_collision.py).
    assert "Regiões de recuo à média" in section
    assert "Regiões de compra na média" not in section
    assert "Setup123 (motor do Nest Egg" in section
    assert "Vela 1" in section and "Gatilho" in section


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
    # o Setup123 é o do motor, que só decide em D/S (DAS-326, DA-024)
    assert s.pattern is None and s.buy_regions
    assert all(":" in r.date for r in s.buy_regions)
    chart = ps.build_price_chart("BTC-USD", "2026-08-20", timeframe="15m")
    assert chart["timeframe"] == "15m"
    assert ":" in chart["candles"][-1]["d"]
    assert chart["ema_windows"] == [8, 21, 50]
    section = ps.build_price_structure_section("BTC-USD", "2026-08-20", "15m")
    assert "15 minutos" in section


@pytest.mark.unit
def test_4h_timeframe_runs_and_is_labelled(monkeypatch):
    """The 4h frame (native exchange candle, task 005) runs the detector and stamps
    '4 horas' on the section/chart/plan — analista decides the 1-2-3 on 15m/4h."""
    base = _frame()
    ts = pd.date_range("2026-08-01 00:00", periods=len(base), freq="4h")
    df = base.copy()
    df["Date"] = ts.strftime("%Y-%m-%d %H:%M")
    monkeypatch.setattr(ps, "load_intraday_ohlcv", lambda symbol, curr_date, tf: df.copy())

    s = ps.detect_price_structure("BTC-USD", "2026-08-20", "4h")
    assert s.pattern is None                     # Setup123 do motor: só D/S (DA-024)

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
            assert pt is None or pt["date"] <= cut


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


@pytest.mark.unit
def test_overlapping_buy_and_realize_declares_degeneracy(monkeypatch):
    """Defeito B (TSM): âncoras distintas (EMA 21 × topo anterior) com bandas que se
    cobrem NÃO são duas zonas independentes — comprar e realizar no mesmo preço é
    setup degenerado, e o plano DECLARA a sobreposição (overlap_note em ambas).
    Curva calibrada: EMA21@126,87 e topo@128,37, bandas ±0,5·ATR se cobrindo."""
    closes = ([100 + 0.3 * i for i in range(100)]          # alta suave → 129,7
              + [129.7 - 0.8 * i for i in range(1, 6)]     # recuo → 125,7
              + [126.4, 126.7, 127.0, 127.6,               # rali curto: topo próximo
                 127.0, 126.8, 126.6, 126.4, 126.2])
    dates = pd.bdate_range("2025-01-01", periods=len(closes))
    c = pd.Series(closes, dtype=float)
    df = pd.DataFrame({
        "Date": dates.strftime("%Y-%m-%d"),
        "Open": c.shift(1).fillna(c).values,
        "High": (c * 1.006).values,
        "Low": (c * 0.994).values,
        "Close": c.values,
        "Volume": [1000] * len(closes),
    })
    monkeypatch.setattr(ps, "load_ohlcv", lambda symbol, curr_date: df.copy())
    plan = ps.build_actionable_plan_dict("SYN", CURR, method="analista")
    bz, rz = plan.get("buy_zone") or {}, plan.get("realize_zone") or {}
    # a curva FOI construída para sobrepor — se deixar de sobrepor, o teste avisa
    assert bz.get("high") is not None and rz.get("low") is not None
    assert bz["high"] >= rz["low"], "curva de teste perdeu a sobreposição"
    assert "overlap_note" in bz and "overlap_note" in rz
    assert "setup degenerado" in bz["overlap_note"]
    assert bz["overlap_note"] == rz["overlap_note"]


@pytest.mark.unit
def test_non_overlapping_zones_carry_no_warning(synth):
    """Bandas afastadas (o caso comum) continuam limpas — o aviso é só pra
    sobreposição real, não vira ruído em toda run."""
    plan = ps.build_actionable_plan_dict("SYN", CURR)
    for zone in (plan.get("buy_zone"), plan.get("realize_zone")):
        assert not (zone or {}).get("overlap_note")

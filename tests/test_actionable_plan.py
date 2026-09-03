"""The verdict must be operable: price @ analysis, horizon, timeframe, and price
zones — or an explicit "sem nível definido" (fork brief 23/08).

The product owner's complaint: the verdict showed no asset price at analysis time,
no time horizon, no timeframe, and no buy/realize/pullback regions. These tests pin
the deterministic ``build_actionable_plan`` on a synthetic series with a KNOWN
structure, offline, and enforce the two hard rules: every level is a real number
from the series (a close, a moving average, or a prior swing high), and when there
is no basis the level is ``None`` — never a fabricated or vague value.
"""
import pandas as pd
import pytest

from tradingagents.dataflows import price_structure as ps


def _frame() -> pd.DataFrame:
    """Same known series as test_price_structure: uptrend, a pullback to MA20, a
    1-2-3, then a breakout that leaves price in new-high air at the end."""
    closes: list[float] = []
    closes += [100 + i for i in range(60)]
    closes += [156, 150, 146, 150, 156]
    closes += [162 + i for i in range(20)]
    closes += [175, 168, 160, 150, 145]
    closes += [150, 160, 172, 185, 190]
    closes += [182, 175, 168, 162, 166]
    closes += [192 + i for i in range(15)]
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


CURR = "2025-12-31"


@pytest.fixture
def synth(monkeypatch):
    df = _frame()
    monkeypatch.setattr(ps, "load_ohlcv", lambda symbol, curr_date: df.copy())
    return df


@pytest.mark.unit
def test_price_and_date_present(synth):
    """Preço no momento da análise + data — the headline the owner said was missing."""
    p = ps.build_actionable_plan("SYN", CURR)
    assert p.price == round(float(synth["Close"].iloc[-1]), 2)
    assert p.as_of == synth["Date"].iloc[-1]
    # price is a real close of the series, not fabricated
    assert p.price in set(round(float(v), 2) for v in synth["Close"])


@pytest.mark.unit
def test_timeframe_declared(synth):
    p = ps.build_actionable_plan("SYN", CURR)
    assert "diário" in p.timeframe  # reference timeframe is stated, not left to guess


@pytest.mark.unit
def test_horizon_is_explicit_not_vague(synth):
    """Horizon must be in days/weeks/months, never the rejected 'médio prazo'."""
    p = ps.build_actionable_plan("SYN", CURR)
    assert p.horizon and "médio prazo" not in p.horizon.lower()
    assert any(w in p.horizon.lower() for w in ("dias", "semanas", "meses"))


@pytest.mark.unit
def test_active_setup_has_real_buy_zone(synth):
    """On this frame price sits on the rising MA20 → live setup, buy zone = that MA."""
    p = ps.build_actionable_plan("SYN", CURR)
    assert p.setup_state == "ativo"
    struct = ps.detect_price_structure("SYN", CURR)
    assert struct.active_region is not None
    assert p.buy_zone is not None
    assert p.buy_zone["price"] == struct.active_region.ma_value  # real MA, not invented
    # price is in new-high air here → nothing overhead → explicit "no level"
    assert p.realize_zone is None
    # live setup → no pullback to wait for
    assert p.pullback_zone is None


@pytest.mark.unit
def test_realize_zone_is_a_real_prior_high(monkeypatch):
    """Cut mid-pullback (price below a prior top): the realization region is a real
    swing high sitting overhead, taken from the series — never fabricated."""
    df = _frame()
    cut = df["Date"].iloc[89]  # price ~145, below earlier tops
    cut_df = df[df["Date"] <= cut].reset_index(drop=True)
    monkeypatch.setattr(ps, "load_ohlcv", lambda s, d: cut_df.copy())
    p = ps.build_actionable_plan("SYN", cut)
    assert p.realize_zone is not None
    assert p.realize_zone["price"] > p.price  # resistance is above current price
    highs = set(round(float(v), 2) for v in cut_df["High"])
    assert p.realize_zone["price"] in highs  # a real high, not invented
    assert p.as_of <= cut  # date guard: nothing past the cut


@pytest.mark.unit
def test_thin_series_is_honest_no_levels(monkeypatch):
    tiny = _frame().head(8).reset_index(drop=True)
    monkeypatch.setattr(ps, "load_ohlcv", lambda s, d: tiny.copy())
    p = ps.build_actionable_plan("SYN", CURR)
    assert p.setup_state == "sem_dado"
    assert p.buy_zone is None and p.realize_zone is None and p.pullback_zone is None
    assert "sem" in p.horizon.lower()  # no operable horizon claimed


@pytest.mark.unit
def test_data_failure_never_fabricates(monkeypatch):
    def boom(symbol, curr_date):
        raise RuntimeError("no data")
    monkeypatch.setattr(ps, "load_ohlcv", boom)
    d = ps.build_actionable_plan_dict("SYN", CURR)  # must not raise
    assert d["setup_state"] == "sem_dado"
    assert d["price"] is None
    assert d["buy_zone"] is None and d["realize_zone"] is None and d["pullback_zone"] is None
    # sem série não há estrutura → sem invalidação, sem stop, sem alvo, sem R:R
    assert d["invalidation"] is None and d["stop"] is None
    assert d["target"] is None and d["risk_reward"] is None


@pytest.mark.unit
def test_dict_is_json_serializable(synth):
    import json
    d = ps.build_actionable_plan_dict("SYN", CURR)
    json.dumps(d)  # must not raise
    assert set(d) == {
        "symbol", "as_of", "price", "timeframe", "horizon",
        # setup_source diz DE QUAL setup veio o setup_state (recuo à média × 1-2-3).
        # Sem ele "Setup ativo agora" não dizia de quem falava, e os dois setups
        # convivem na mesma tela — ver tests/test_setup_naming_collision.py.
        "setup_state", "setup_source",
        "buy_zone", "realize_zone", "pullback_zone", "pattern",
        "invalidation", "stop", "target", "risk_reward",
        # projecao_p3 é a FAIXA onde o ponto 3 precisa nascer quando o padrão está em
        # gestação ou morreu (task 20260830-013). Vazia com padrão vivo — ali o ponto
        # 3 já existe —, mas a CHAVE existe sempre: a tela não pode ter que adivinhar
        # se o campo sumiu porque não se aplica ou porque o plano esqueceu.
        "projecao_p3",
        # cronologia é a ORDEM dos eventos do padrão (DA-124): desde quando ele
        # existe, quando invalidou, e em que ordem o preço tocou gatilho, alvo e
        # stop. Sem ela a tela mostra o preço passando pelo alvo com um rótulo
        # "invalidado" ao lado, e a leitura natural erra — num sentido ou no outro,
        # conforme a ordem real. A CHAVE existe sempre, pelo mesmo motivo do
        # projecao_p3: "não se aplica" e "o plano esqueceu" não podem se confundir.
        "cronologia",
    }


@pytest.mark.unit
def test_no_fabricated_numbers_anywhere(synth):
    """Every non-null zone price traces to the series: buy/pullback are moving
    averages the detector produced; realize is a real swing high."""
    p = ps.build_actionable_plan("SYN", CURR)
    struct = ps.detect_price_structure("SYN", CURR)
    ma_values = set()
    if struct.active_region:
        ma_values.add(struct.active_region.ma_value)
    ma_values |= {r.ma_value for r in struct.buy_regions}
    highs = set(round(float(v), 2) for v in synth["High"])
    if p.buy_zone:
        assert p.buy_zone["price"] in ma_values
    if p.pullback_zone and p.setup_state == "aguardar_pullback":
        assert p.pullback_zone["price"] in ma_values
    if p.realize_zone:
        assert p.realize_zone["price"] in highs


@pytest.mark.unit
def test_zone_is_a_band_derived_from_atr(synth):
    """A region is a FAIXA (mín–máx), not a centavo — width is ±0.5·ATR read off
    the real series, and the band brackets the anchor. No cosmetic percentage."""
    p = ps.build_actionable_plan("SYN", CURR)
    df = ps._prep("SYN", CURR)
    atr = ps._atr(df)
    assert atr is not None and atr > 0
    z = p.buy_zone
    assert z is not None and z["low"] is not None and z["high"] is not None
    # the band is exactly ±0.5·ATR around the anchor — a derived, documented width
    assert z["low"] == round(z["price"] - 0.5 * atr, 2)
    assert z["high"] == round(z["price"] + 0.5 * atr, 2)
    assert z["low"] < z["price"] < z["high"]
    assert z["band_basis"] == ps._BAND_BASIS


@pytest.mark.unit
def test_zone_without_atr_basis_is_an_honest_point(monkeypatch, synth):
    """No ATR basis → the level degrades to a point (low/high None), never a
    fabricated cosmetic band."""
    monkeypatch.setattr(ps, "_atr", lambda df, period=ps._ATR_PERIOD: None)
    p = ps.build_actionable_plan("SYN", CURR)
    z = p.buy_zone
    assert z is not None and z["price"] is not None
    assert z["low"] is None and z["high"] is None and z["band_basis"] is None


@pytest.mark.unit
def test_1_2_3_trigger_stays_a_point_not_a_band(monkeypatch):
    """When the operable step is a 1-2-3 breakout trigger it is a precise line, so
    it must NOT be widened into a cosmetic band even though ATR exists."""
    df = _frame()
    # cut just before the breakout so the most-recent 1-2-3 is still 'formando'
    cut = df["Date"].iloc[99]
    cut_df = df[df["Date"] <= cut].reset_index(drop=True)
    monkeypatch.setattr(ps, "load_ohlcv", lambda s, d: cut_df.copy())
    p = ps.build_actionable_plan("SYN", cut)
    if p.setup_state == "aguardar_rompimento":
        assert p.pullback_zone is not None
        assert p.pullback_zone["low"] is None and p.pullback_zone["high"] is None


@pytest.mark.unit
def test_pattern_is_attached_to_plan(synth):
    """The 1-2-3 the detector found must reach the card — sentido, 3 pontos,
    gatilho, estado — not die on the way to the screen (fork brief 24/08)."""
    p = ps.build_actionable_plan("SYN", CURR)
    struct = ps.detect_price_structure("SYN", CURR)
    assert struct.pattern is not None  # this frame has a 1-2-3 de compra
    assert p.pattern is not None
    assert p.pattern["direction"] in ("compra", "venda")
    for key in ("p1", "p2", "p3", "trigger", "state", "direction"):
        assert key in p.pattern
    for pt in (p.pattern["p1"], p.pattern["p2"], p.pattern["p3"]):
        assert "date" in pt and "price" in pt


# ---------------------------------------------------------------------------
# EIXO TEMPORAL ÚNICO (DA-205) — o carimbo do último candle sai da FONTE já no
# fuso da tela (Manaus), offset-aware, no intradiário; só a DATA no diário/semanal.
#
# O bug da 019: a série intradiária carrega ``Date`` em UTC-naive (ver
# ``intraday._yf_intraday_to_df``); ``as_of`` mandava essa hora crua pro front, que
# a lia como se fosse Manaus — 4h adiantada, um candle no FUTURO do "agora". A
# conversão vive em ``_as_of_stamp`` e é isto que estes testes fixam.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_as_of_intraday_vira_instante_manaus_offset_aware():
    """Um candle 4h carimbado 17:30 UTC (o que a série entrega, naive) vira 13:30
    Manaus, offset-aware — a hora que o usuário lê no relógio dele."""
    ts = pd.Timestamp("2026-09-03 17:30")   # UTC-naive, como a série intradiária entrega
    assert ps._as_of_stamp(ts, "4h") == "2026-09-03T13:30-04:00"
    assert ps._as_of_stamp(pd.Timestamp("2026-09-03 19:30"), "1h") == "2026-09-03T15:30-04:00"


@pytest.mark.unit
def test_as_of_diario_e_semanal_sao_so_a_data_nunca_hora():
    """A barra do dia/semana é do DIA — hora não tem sentido, e converter a
    meia-noite naive ainda ROLARIA a data pro dia anterior. Fica só a DATA."""
    ts = pd.Timestamp("2026-09-03 00:00")
    assert ps._as_of_stamp(ts, "1d") == "2026-09-03"
    assert ps._as_of_stamp(ts, "1w") == "2026-09-03"
    # sem 'T', sem hora, sem fuso: o front nunca inventa horário sobre uma data
    assert "T" not in ps._as_of_stamp(ts, "1d") and ":" not in ps._as_of_stamp(ts, "1d")

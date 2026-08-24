"""Correlação entre ativos + FORÇA RELATIVA, dos candles cacheados (sem rede).

Provedor novo (brief 24/08): o método do Erick mapeia ativos por correlação com um
ÂNCORA (NVDA) diante de um evento, e extrai FORÇA RELATIVA — quem não cai quando o
líder cai. Estes testes alimentam séries sintéticas via monkeypatch de
``load_ohlcv`` (nada de rede) e checam: banda do Erick, correlação alta/baixa dos
log-retornos, o caso do próprio âncora, a força relativa num recorte de queda, a
seção pt-BR, e o degradê quando falta candle (nada inventado).
"""
import pandas as pd
import pytest

from tradingagents.dataflows import correlation as corr

# Retornos com variação (constante daria variância zero -> Pearson indefinido).
_RETS = [0.02, -0.01, 0.015, -0.02, 0.005, -0.012, 0.018, -0.008, 0.01, -0.015]


def _closes_from_rets(rets, base=100.0):
    out, px = [base], base
    for r in rets:
        px *= (1 + r)
        out.append(px)
    return out


def _df(closes, start="2025-01-01"):
    dates = pd.date_range(start=start, periods=len(closes), freq="D")
    return pd.DataFrame(
        {
            "Date": dates,
            "Open": closes,
            "High": [c * 1.01 for c in closes],
            "Low": [c * 0.99 for c in closes],
            "Close": closes,
            "Volume": [1000] * len(closes),
        }
    )


def _loader(frames):
    """Fake load_ohlcv: symbol (upper) -> DataFrame."""
    def load(symbol, curr_date):
        key = str(symbol).upper()
        if key not in frames:
            raise RuntimeError(f"no such symbol {key}")
        return frames[key]
    return load


# ------------------------------------------------------------- bandas ----------
@pytest.mark.unit
def test_classify_correlation_bands():
    assert corr.classify_correlation(0.86)[0] == "alta"
    assert corr.classify_correlation(0.70)[0] == "alta"
    assert corr.classify_correlation(0.60)[0] == "moderada-alta"
    assert corr.classify_correlation(0.40)[0] == "moderada"
    assert corr.classify_correlation(0.10)[0] == "baixa"
    assert corr.classify_correlation(-0.30)[0] == "baixa"


# --------------------------------------------------------- correlação ----------
@pytest.mark.unit
def test_high_correlation_moves_together(monkeypatch):
    rets = (_RETS * 12)[:100]
    anc = _closes_from_rets(rets)
    sym = _closes_from_rets(rets, base=50.0)  # mesmos retornos, escala diferente
    monkeypatch.setattr(corr, "load_ohlcv", _loader({"NVDA": _df(anc), "AMD": _df(sym)}))
    out = corr.compute_correlation("AMD", "2026-01-01", "NVDA")
    assert out["available"] and not out["is_anchor"]
    w60 = out["windows"][60]
    assert w60["r"] == pytest.approx(1.0, abs=1e-6)
    assert w60["label"] == "alta"


@pytest.mark.unit
def test_low_correlation_when_inverse(monkeypatch):
    rets = (_RETS * 12)[:100]
    anc = _closes_from_rets(rets)
    sym = _closes_from_rets([-r for r in rets], base=80.0)  # retornos invertidos
    monkeypatch.setattr(corr, "load_ohlcv", _loader({"NVDA": _df(anc), "MCD": _df(sym)}))
    out = corr.compute_correlation("MCD", "2026-01-01", "NVDA")
    w60 = out["windows"][60]
    # Log-retornos de retornos simples invertidos não dão -1 exato, mas a
    # correlação é fortemente negativa e cai na banda "baixa" (protegido).
    assert w60["r"] < -0.9
    assert w60["label"] == "baixa"


@pytest.mark.unit
def test_symbol_is_the_anchor(monkeypatch):
    monkeypatch.setattr(corr, "load_ohlcv", _loader({"NVDA": _df(_closes_from_rets(_RETS))}))
    out = corr.compute_correlation("NVDA", "2026-01-01", "NVDA")
    assert out["is_anchor"] is True
    section = corr.build_correlation_section("NVDA", "2026-01-01")
    assert "próprio âncora" in section


@pytest.mark.unit
def test_insufficient_history_marked(monkeypatch):
    short = _closes_from_rets(_RETS[:5])  # ~6 closes -> ~5 retornos < _MIN_OBS
    monkeypatch.setattr(corr, "load_ohlcv", _loader({"NVDA": _df(short), "AMD": _df(short)}))
    out = corr.compute_correlation("AMD", "2026-01-01", "NVDA")
    assert out["windows"][30]["insufficient"] is True


# ----------------------------------------------------- força relativa ----------
@pytest.mark.unit
def test_relative_strength_flags_who_fell_less(monkeypatch):
    # Âncora: sobe 100->120 (pico no dia 20), cai 120->108 (vale no dia 30, -10%).
    anc = [100 + i for i in range(21)] + [120 - 1.2 * i for i in range(1, 11)]
    # Símbolo: no mesmo recorte [dia20, dia30] cai só de 50 para 49 (-2%).
    sym = [40 + 0.5 * i for i in range(21)] + [50 - 0.1 * i for i in range(1, 11)]
    monkeypatch.setattr(corr, "load_ohlcv", _loader({"NVDA": _df(anc), "MCD": _df(sym)}))
    rs = corr.compute_relative_strength("MCD", "2026-01-01", "NVDA")
    assert rs["has_window"] is True
    assert rs["anchor_ret"] == pytest.approx(-0.10, abs=1e-6)
    assert rs["symbol_ret"] == pytest.approx(-0.02, abs=1e-6)
    assert rs["diff"] > 0
    assert "força relativa" in rs["verdict"]


@pytest.mark.unit
def test_relative_strength_no_drawdown_window(monkeypatch):
    up = _closes_from_rets([0.01] * 40)  # só sobe -> sem queda relevante
    monkeypatch.setattr(corr, "load_ohlcv", _loader({"NVDA": _df(up), "AMD": _df(up)}))
    rs = corr.compute_relative_strength("AMD", "2026-01-01", "NVDA")
    assert rs["has_window"] is False


# -------------------------------------------------------------- seção ----------
@pytest.mark.unit
def test_section_shows_correlation_and_window(monkeypatch):
    rets = (_RETS * 12)[:100]
    anc = _closes_from_rets(rets)
    sym = _closes_from_rets(rets, base=50.0)
    monkeypatch.setattr(corr, "load_ohlcv", _loader({"NVDA": _df(anc), "AMD": _df(sym)}))
    section = corr.build_correlation_section("AMD", "2026-01-01", "stock")
    assert "Correlação com o âncora (NVDA)" in section
    assert "60d" in section
    assert "Força relativa" in section


@pytest.mark.unit
def test_section_unavailable_when_no_candle(monkeypatch):
    monkeypatch.setattr(corr, "load_ohlcv", _loader({}))  # tudo levanta
    section = corr.build_correlation_section("BADSYM", "2026-01-01")
    assert "indisponível" in section.lower()
    assert "inventado" in section.lower()


@pytest.mark.unit
def test_default_anchor_by_asset_type():
    assert corr.default_anchor("stock") == "NVDA"
    assert corr.default_anchor("crypto") == "BTC-USD"


# ----------------------------------------------------------- coverage ----------
@pytest.mark.unit
def test_coverage_appends_section(monkeypatch):
    from tradingagents.agents.utils import correlation_coverage as cc

    monkeypatch.setattr(
        cc, "build_correlation_section", lambda *a, **k: "## 🔗 Correlação"
    )
    out = cc.ensure_correlation_coverage("relatório existente", "AMD", "2026-01-01")
    assert "relatório existente" in out and "Correlação" in out


@pytest.mark.unit
def test_coverage_fail_open(monkeypatch):
    from tradingagents.agents.utils import correlation_coverage as cc

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(cc, "build_correlation_section", boom)
    assert cc.ensure_correlation_coverage("intacto", "AMD", "2026-01-01") == "intacto"

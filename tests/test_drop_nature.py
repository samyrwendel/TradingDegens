"""Natureza da queda — liquidação de longs (saudável) × fraqueza (evitar).

A REGRA de leitura da lacuna nº 3, exercitada sobre snapshots sintéticos (sem
rede): o âncora bateu + regime de fundo intacto + recuo a uma média que sobe →
'liquidação saudável'; estrutura rompida + âncora não bateu → 'fraqueza'; sinais
mistos ou sem queda → 'indefinido' (nunca um chute bullish).
"""
import pytest

from tradingagents.agents.utils import drop_nature as dn


def _snap(**over):
    """Snapshot diário base = queda forte, recuo a uma 200 que sobe (caso AVGO)."""
    base = {
        "price": 355.0, "recent_high": 428.0, "dd_pct": -17.0,
        "ma50": 386.0, "ma200": 368.0, "ma200_rising": True,
        "above_ma50": False, "trend": "baixa",
        "active_rising_label": "MMS200",
        "pattern_dir": "compra", "pattern_state": "rompeu_retracou",
    }
    base.update(over)
    return base


def _wire(monkeypatch, asset_snap, anchor_snap, anchor_ev):
    """Injeta os snapshots por símbolo e o resultado do âncora."""
    def snap(symbol, curr_date):
        return anchor_snap if symbol.upper() == "NVDA" else asset_snap

    monkeypatch.setattr(dn, "_daily_snapshot", snap)
    monkeypatch.setattr(
        dn, "_anchor_beat_recent",
        lambda symbol, curr_date: (
            bool(anchor_ev and anchor_ev.get("beat")), anchor_ev
        ),
    )


_ANCHOR_UP = {"trend": "alta", "above_ma50": True}
_BEAT = {"beat": True, "surprise_pct": 4.3, "days_since": 3}
_MISS = {"beat": False, "surprise_pct": -8.0, "days_since": 3}


@pytest.mark.unit
def test_liquidacao_when_anchor_beat_and_structure_intact(monkeypatch):
    _wire(monkeypatch, _snap(), _ANCHOR_UP, _BEAT)
    res = dn.classify_drop_nature("AVGO", "2026-08-26", "stock")
    assert res["classification"] == "liquidacao_saudavel"
    joined = " ".join(res["reasons"]).lower()
    assert "recuo" in joined and "bateu" in joined


@pytest.mark.unit
def test_liquidacao_line_reframes_mechanical_cash(monkeypatch):
    _wire(monkeypatch, _snap(), _ANCHOR_UP, _BEAT)
    line = dn.build_drop_nature_line("AVGO", "2026-08-26", "stock", mechanical_estado="CAIXA")
    assert "liquidação de longs" in line.lower()
    assert "segue comprador" in line.lower()
    # Re-leitura do CAIXA mecânico como recuo comprável.
    assert "recuo comprável" in line.lower() or "comprável" in line.lower()


@pytest.mark.unit
def test_fraqueza_when_breakdown_and_no_beat(monkeypatch):
    weak = _snap(
        ma200_rising=False, active_rising_label=None,
        pattern_dir="venda", pattern_state="acionado",
    )
    _wire(monkeypatch, weak, {"trend": "baixa", "above_ma50": False}, _MISS)
    res = dn.classify_drop_nature("SOXL", "2026-08-26", "stock")
    assert res["classification"] == "fraqueza"
    line = dn.build_drop_nature_line("SOXL", "2026-08-26", "stock", mechanical_estado="CAIXA")
    assert "evitar" in line.lower()


@pytest.mark.unit
def test_indefinido_when_signals_mixed(monkeypatch):
    # Queda + recuo a média que sobe, MAS o âncora não bateu → nem uma coisa nem outra.
    _wire(monkeypatch, _snap(), _ANCHOR_UP, _MISS)
    res = dn.classify_drop_nature("AVGO", "2026-08-26", "stock")
    assert res["classification"] == "indefinido"
    assert any("beat" in r.lower() for r in res["reasons"])


@pytest.mark.unit
def test_no_drop_returns_indefinido_and_no_line(monkeypatch):
    calm = _snap(dd_pct=-1.0)  # recuo trivial
    _wire(monkeypatch, calm, _ANCHOR_UP, _BEAT)
    res = dn.classify_drop_nature("AVGO", "2026-08-26", "stock")
    assert res["classification"] == "indefinido"
    # Sem queda relevante → não anexa bloco (não polui o relatório).
    assert dn.build_drop_nature_line("AVGO", "2026-08-26", "stock") is None


@pytest.mark.unit
def test_fail_open_on_missing_data(monkeypatch):
    monkeypatch.setattr(dn, "_daily_snapshot", lambda s, d: None)
    res = dn.classify_drop_nature("XYZ", "2026-08-26", "stock")
    assert res["classification"] == "indefinido"


@pytest.mark.unit
def test_liquidacao_requires_pullback_not_breakdown(monkeypatch):
    # Âncora bateu e fundo intacto, MAS 1-2-3 de venda acionado = rompimento → não é
    # liquidação saudável (a queda rompeu estrutura, não recuou à média).
    broke = _snap(pattern_dir="venda", pattern_state="acionado", active_rising_label=None)
    _wire(monkeypatch, broke, _ANCHOR_UP, _BEAT)
    res = dn.classify_drop_nature("AVGO", "2026-08-26", "stock")
    assert res["classification"] != "liquidacao_saudavel"

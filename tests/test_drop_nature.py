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
def test_liquidacao_line_explains_state_derivation(monkeypatch):
    # A linha explica a DERIVAÇÃO do Estado (já computado A PARTIR da classificação),
    # não uma re-leitura que o contradiz: liquidação + Estado AGUARDAR ⇒ "e não CAIXA".
    _wire(monkeypatch, _snap(), _ANCHOR_UP, _BEAT)
    line = dn.build_drop_nature_line("AVGO", "2026-08-26", "stock", estado="AGUARDAR")
    assert "liquidação de longs" in line.lower()
    assert "segue comprador" in line.lower()
    assert "deriva desta classificação" in line.lower()
    assert "aguardar" in line.lower() and "não caixa" in line.lower()
    # A string de "re-leitura" contraditória saiu de vez.
    assert "re-leitura" not in line.lower()


@pytest.mark.unit
def test_fraqueza_when_breakdown_and_no_beat(monkeypatch):
    weak = _snap(
        ma200_rising=False, active_rising_label=None,
        pattern_dir="venda", pattern_state="acionado",
    )
    _wire(monkeypatch, weak, {"trend": "baixa", "above_ma50": False}, _MISS)
    res = dn.classify_drop_nature("SOXL", "2026-08-26", "stock")
    assert res["classification"] == "fraqueza"
    line = dn.build_drop_nature_line("SOXL", "2026-08-26", "stock", estado="CAIXA")
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


# ---------------------------------------- classify_safe (fonte única) ----------
@pytest.mark.unit
def test_classify_safe_returns_dict_and_none_on_error(monkeypatch):
    _wire(monkeypatch, _snap(), _ANCHOR_UP, _BEAT)
    res = dn.classify_drop_nature_safe("AVGO", "2026-08-26", "stock")
    assert res and res["classification"] == "liquidacao_saudavel"
    # Qualquer exceção na classificação vira None (fail-open) — nunca propaga.
    monkeypatch.setattr(dn, "classify_drop_nature", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert dn.classify_drop_nature_safe("AVGO", "2026-08-26", "stock") is None


# ---------------------------------------- guardrail de coerência ---------------
_LIQ = {"classification": "liquidacao_saudavel", "reasons": ["queda de -17,0% recuou a uma média que sobe"]}
_FRA = {"classification": "fraqueza", "reasons": ["queda de -17,0% com estrutura rompida"]}


@pytest.mark.unit
def test_guardrail_strips_contradicting_sentence_for_liquidacao():
    text = ("A queda é liquidação de longs, recuo comprável à média. "
            "Melhor evitar esta queda: a tendência de baixa virou fraqueza. "
            "O âncora bateu o resultado.")
    out, flags = dn.enforce_drop_nature_coherence(text, _LIQ)
    assert flags["removed"] == 1
    assert "evitar esta queda" not in out.lower()
    assert "recuo comprável" in out.lower()          # a frase coerente fica
    assert "âncora bateu" in out.lower()
    assert "coerência" in out.lower()                # nota anexada


@pytest.mark.unit
def test_guardrail_strips_contradicting_sentence_for_fraqueza():
    text = ("A estrutura rompeu, caixa é a posição. "
            "Ainda assim a queda é uma oportunidade de compra comprável.")
    out, flags = dn.enforce_drop_nature_coherence(text, _FRA)
    assert flags["removed"] == 1
    assert "oportunidade de compra" not in out.lower()
    assert "caixa é a posição" in out.lower()


@pytest.mark.unit
def test_guardrail_untouched_for_indefinido_and_none():
    text = "A queda pode ser evitar ou comprável, sinais mistos."
    out, flags = dn.enforce_drop_nature_coherence(text, {"classification": "indefinido"})
    assert out == text and flags["removed"] == 0
    # drop=None (fail-open): prosa intacta byte-a-byte.
    out2, flags2 = dn.enforce_drop_nature_coherence(text, None)
    assert out2 == text and flags2["removed"] == 0


@pytest.mark.unit
def test_guardrail_no_offender_returns_text_unchanged():
    text = "A queda é liquidação de longs, recuo comprável. O âncora bateu."
    out, flags = dn.enforce_drop_nature_coherence(text, _LIQ)
    assert out == text and flags["removed"] == 0


# ---------------------------------------- campo estruturado --------------------
@pytest.mark.unit
def test_drop_nature_field_shape_and_indisponivel():
    res = {
        "classification": "liquidacao_saudavel",
        "reasons": ["r1", "r2"],
        "evidence": {"anchor": {"name": "NVDA", "beat_recent": True, "trend": "alta",
                                "earnings": {"huge": "raw snapshot"}}},
    }
    field = dn.drop_nature_field(res, {"removed": 1})
    assert field["classification"] == "liquidacao_saudavel"
    assert field["reasons"] == ["r1", "r2"]
    assert field["anchor"] == {"name": "NVDA", "beat_recent": True, "trend": "alta"}
    assert field["coherence_flags"] == {"removed": 1}
    assert "earnings" not in field["anchor"]          # snapshot cru não vaza
    # None → indisponivel (nunca quebra o campo).
    empty = dn.drop_nature_field(None)
    assert empty["classification"] == "indisponivel" and empty["anchor"] == {}

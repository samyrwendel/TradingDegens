"""DA-024: o Setup123 do TradingDegens é o do MOTOR do Nest Egg — importado, nunca copiado —
e o Storm123 (+ Éden) está desativado.

As velas entram pelo seam ``setup123_motor._velas_brutas`` (em produção: as velas BRUTAS do
motor, ``nest_egg.motor.sr.candles``); o detector é o motor de verdade, sem mock.
"""
import calendar
import inspect
import pathlib

import pytest

from tradingagents.dataflows import price_structure as ps, setup123_motor

_RAIZ = pathlib.Path(__file__).resolve().parents[1] / "tradingagents"


def _velas(ohlc, inicio=(2026, 8, 3)):
    t0 = calendar.timegm((*inicio, 0, 0, 0))
    return [[t0 + i * 86400, *v, 0.0] for i, v in enumerate(ohlc)]


# 20 velas de alta (base do ATR14) + vela 1 de queda + vela 2a de queda que perde a
# mínima da 1 + vela 3 que FECHA acima da máxima da 2a.
_BASE = [[100 + i, 101 + i, 99 + i, 100.5 + i] for i in range(20)]
_V1 = [121, 121.5, 118, 118.5]
_V2A = [118.4, 119, 116, 116.5]
_V3 = [116.6, 120, 116.2, 119.5]


@pytest.fixture
def confirmado(monkeypatch):
    velas = _velas(_BASE + [_V1, _V2A, _V3])
    monkeypatch.setattr(setup123_motor, "_velas_brutas",
                        lambda s, d, f: velas if f == "1d" else [])
    return velas


def test_a_leitura_e_a_do_motor(confirmado):
    from nest_egg.motor import setup123 as motor

    L = setup123_motor.leitura("X", "2026-09-30", "1d")
    assert L["estado"] == "CONFIRMADO"
    assert L["gatilho"] == 119 and L["invalidacao"] == 116 and L["entrada"] == 119.5
    o, h, lo, c = ([v[k] for v in confirmado] for k in (1, 2, 3, 4))
    e = motor.estado_atual(o, h, lo, c, len(c) - 1, frame="1d")
    assert L["stop_posicao"] == round(motor.stop_posicao(e, o, h, lo, c, "1d"), 2)
    assert L["vela1"]["date"] < L["vela2"]["date"] < L["vela3"]["date"]


def test_o_motor_so_decide_em_D_e_S(confirmado):
    assert setup123_motor.leitura("X", "2026-09-30", "4h") is None
    assert setup123_motor.leitura("X", "2026-09-30", "1h") is None


def test_data_passada_so_ve_o_que_ja_tinha_fechado(confirmado):
    # na data da vela 2a o 123 ainda esperava a vela 3: FORMANDO, sem entrada própria
    d2a = setup123_motor.leitura("X", "2026-09-30", "1d")["vela2"]["date"]
    L = setup123_motor.leitura("X", d2a, "1d")
    assert L["estado"] == "FORMANDO" and L["vela3"] is None and L["entrada"] == L["gatilho"]


def test_o_plano_publica_os_niveis_do_motor_e_nao_inventa_alvo(confirmado, monkeypatch):
    import pandas as pd

    df = pd.DataFrame([{"Date": pd.Timestamp(v[0], unit="s").strftime("%Y-%m-%d"), "Open": v[1],
                        "High": v[2], "Low": v[3], "Close": v[4], "Volume": 0} for v in confirmado])
    monkeypatch.setattr(ps, "_load_frame", lambda s, d, t: df.copy())
    ps.clear_prep_cache()
    plano = ps.build_actionable_plan_dict("X", "2026-09-30", "1d")
    L = setup123_motor.leitura("X", "2026-09-30", "1d")
    assert plano["pattern"]["state"] == "CONFIRMADO"
    assert plano["pattern"]["trigger"] == L["gatilho"]
    assert plano["invalidation"]["price"] == L["invalidacao"]
    assert plano["stop"]["price"] == L["stop_posicao"]
    assert plano["target"] is None and plano["risk_reward"]["rr"] is None
    assert "não tem alvo" in plano["risk_reward"]["note"]
    texto = ps.build_price_structure_section("X", "2026-09-30", "1d")
    assert "Setup123 (motor do Nest Egg" in texto and "**CONFIRMADO**" in texto


def test_nenhum_123_proprio_sobrou_no_td():
    """O critério da DA-024 em código: nenhum detector de 123 vive no TD."""
    assert not hasattr(ps, "_pattern_123") and not hasattr(ps, "Pattern123")
    for f in _RAIZ.rglob("*.py"):
        fonte = f.read_text(encoding="utf-8")
        assert "def estado_atual(" not in fonte, f"cópia do motor em {f}"
        assert "def _pattern_123(" not in fonte, f
    assert "nest_egg.motor import setup123" in inspect.getsource(setup123_motor)


def test_storm123_desligado_em_todo_caminho(tmp_path):
    from tradingagents.webui import runner, scanner
    from tradingagents.webui.store import EstrategiaStore

    assert ps.STORM123_ATIVO is False
    assert scanner._storm_cols("X", "2026-09-30", "1d", 100.0, com_eden=True) == {}
    (tmp_path / "estrategias.json").write_text('{"storm": {"stock": true, "crypto": true}}')
    st = EstrategiaStore(tmp_path)
    assert st.get()["storm"] == {"stock": False, "crypto": False}
    with pytest.raises(ValueError, match="DA-024"):
        st.set("storm", True, "stock")
    assert "incluir_storm and STORM123_ATIVO" in inspect.getsource(runner.plano_com_storm)

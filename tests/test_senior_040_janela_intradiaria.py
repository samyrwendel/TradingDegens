"""Task 20260902-040 (seniorbot) — PATCH B (decisão do Samyr): no intradiário a janela
do toque começa na barra SEGUINTE ao log, não no dia seguinte.

Medido no ledger 28/08→02/09/2026: com a janela por DIA, o 1-2-3 em 1h fecha 10 alvos
e 4 stops (−1,84R); com a janela pela BARRA, 13 alvos e 1 stop (+1,77R) — três
"stops" eram alvos tocados horas depois do log, no mesmo dia, que a régua não via.
"""
from tradingagents.webui import scanner


def test_intradiario_conta_a_barra_seguinte_ao_log_no_mesmo_dia():
    candles = [{"d": "2026-08-31 03:00", "h": 100, "l": 90},
               {"d": "2026-08-31 04:00", "h": 112, "l": 95},    # contém o log: NÃO conta
               {"d": "2026-08-31 05:00", "h": 111, "l": 99},    # toca o alvo 110
               {"d": "2026-09-01 00:00", "h": 100, "l": 80}]    # tocaria o stop 85
    desde = scanner._desde_do_toque("2026-08-31T04:01:00+00:00", "1h")
    assert desde == "2026-08-31 04:01"
    assert scanner._primeiro_toque(candles, desde, 110, 85, False) == {
        "veredito": "bateu_tp", "fechado_em": "2026-08-31", "empate_na_barra": False}
    # a régua antiga (só a data) pulava o dia inteiro e fechava no stop do dia seguinte
    assert scanner._primeiro_toque(candles, "2026-08-31", 110, 85, False)["veredito"] == "bateu_sl"


def test_diario_continua_pulando_o_dia_inteiro_do_log():
    assert scanner._desde_do_toque("2026-08-31T04:01:00+00:00", "1d") == "2026-08-31"
    assert scanner._desde_do_toque("2026-08-31T04:01:00+00:00", "1w") == "2026-08-31"
    candles = [{"d": "2026-08-31", "h": 112, "l": 95}, {"d": "2026-09-01", "h": 100, "l": 80}]
    assert scanner._primeiro_toque(candles, "2026-08-31", 110, 85, False)["veredito"] == "bateu_sl"


def test_fuso_do_log_vira_utc_e_sem_carimbo_nao_ha_janela():
    assert scanner._desde_do_toque("2026-09-01T04:13:17-04:00", "1h") == "2026-09-01 08:13"
    assert scanner._desde_do_toque(None, "1h") == ""
    assert scanner._desde_do_toque("lixo", "4h") == "lixo"[:10]

"""Calendário de earnings — próxima data de resultado, cacheada e date-guarded.

Provedor novo (brief 24/08): o eixo da análise do Erick é o EVENTO ("resultado da
NVDA sai quarta 26/08"). Estes testes injetam um DataFrame de earnings falso (sem
rede) e checam: escolher a próxima data ESTRITAMENTE depois da base, o "após o
fechamento", o clamp do date_guard, a fonte caída virando "indisponível" (sem
inventar data), e o vazio em cripto.

Bug L1 (28/08, task 008): o filtro era ``ts <= base`` e ENGOLIA o balanço do
próprio dia — no dia 27/08 o MRVL divulgava às 16h e a seção dizia "indisponível",
deixando muda a regra "não aumentar posição antes do balanço" na única hora em que
ela importa. Agora a próxima data inclui a de HOJE, e "sem agenda" (a fonte
respondeu e não há data) deixou de se confundir com "fonte fora do ar".
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
def test_skips_dates_before_base(fake_yf):
    # Base em junho: a de maio (05-20) fica pra trás; a próxima é a de agosto.
    ev = ec.get_next_earnings("NVDA", "2026-06-01")
    assert ev["date"] == "2026-08-26"


@pytest.mark.unit
def test_earnings_do_proprio_dia_nao_some(fake_yf):
    """L1: com a base NO dia do balanço, o evento é o de HOJE — não o do trimestre
    que vem. Era exatamente o que sumia (MRVL 27/08, divulgação às 16h)."""
    ev = ec.get_next_earnings("NVDA", "2026-08-26")
    assert ev["date"] == "2026-08-26"
    assert ev["is_today"] is True
    assert ev["days_ahead"] == 0
    assert ev["after_close"] is True


@pytest.mark.unit
def test_dia_seguinte_ao_balanco_nao_reaproveita_o_de_ontem(fake_yf):
    """O inverso do L1: passado o dia, o evento de ontem não pode continuar
    aparecendo como "próximo" — aí sim não há mais data à frente."""
    assert ec.get_next_earnings("NVDA", "2026-08-27") is None


@pytest.mark.unit
def test_secao_grita_quando_o_balanco_e_hoje(fake_yf):
    section = ec.build_earnings_section("NVDA", "2026-08-26", "stock")
    assert "RESULTADO HOJE" in section
    assert "não aumentar posição antes do balanço" in section
    assert "2026-08-26" in section


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
    """Fonte caída: "indisponível", e a seção diz que NÃO SABE se há balanço."""
    monkeypatch.setattr(ec, "get_next_earnings_status",
                        lambda sym, cd: (None, ec.STATUS_FONTE_INDISPONIVEL))
    section = ec.build_earnings_section("AMD", "2026-08-01", "stock")
    assert "indisponível" in section.lower()
    assert "inventada" in section.lower()
    assert "não respondeu" in section.lower()


@pytest.mark.unit
def test_sem_agenda_nao_se_confunde_com_fonte_fora_do_ar(monkeypatch):
    """L1: as duas causas se leem ao CONTRÁRIO uma da outra — "sem data publicada"
    é informação (não há risco de evento conhecido); "fonte fora do ar" é
    ignorância. Não podem sair na mesma frase."""
    monkeypatch.setattr(ec, "get_next_earnings_status",
                        lambda sym, cd: (None, ec.STATUS_SEM_AGENDA))
    section = ec.build_earnings_section("AMD", "2026-08-01", "stock")
    assert "sem data de resultado publicada" in section
    assert "não é falha de fonte" in section
    assert "não respondeu" not in section.lower()


@pytest.mark.unit
def test_status_distingue_as_duas_ausencias(monkeypatch, fake_yf):
    """O status vem do dado, não do texto: fonte que explode → fonte_indisponivel;
    fonte que responde vazio → sem_agenda; com evento → ok."""
    ev, st = ec.get_next_earnings_status("NVDA", "2026-08-01")
    assert st == ec.STATUS_OK and ev is not None

    _, st_vazio = ec.get_next_earnings_status("NVDA", "2027-01-01")
    assert st_vazio == ec.STATUS_SEM_AGENDA

    def boom(symbol, base):
        raise RuntimeError("yahoo 429")
    monkeypatch.setattr(ec, "_fetch_next_earnings", boom)
    _, st_caiu = ec.get_next_earnings_status("AMD", "2026-08-01")
    assert st_caiu == ec.STATUS_FONTE_INDISPONIVEL


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

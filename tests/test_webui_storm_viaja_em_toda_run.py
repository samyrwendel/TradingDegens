"""O STORM É PARTE DO PLANO, NÃO DA RUN (task 20260830-033).

*"eu não vi nenhum desenho do storm123 nos gráficos que analisei."*

A leitura do Storm viajava só quando o MÉTODO era ``storm123``. Numa análise Padrão
ou Erick ela não estava desligada — estava **ausente**, e por isso nem o botão da
camada aparecia. A tela não tinha como anunciar o que não recebeu.

Aqui se trava o contrário: **todo caminho que monta o plano da tela traz o Storm ao
lado** (nunca no lugar), de graça — mesma série cacheada e date-guarded, $0 de LLM.
Quem decide o que é DESENHADO continua sendo a camada, na tela (DA-088).
"""

import pytest

from tradingagents.webui import runner as R

pytestmark = pytest.mark.unit

TICKER = "MSFT"
DATE = "2026-08-29"


@pytest.fixture
def sem_rede(monkeypatch):
    """Os dois produtores viram função pura: o teste mede a MONTAGEM, não o detector."""
    chamadas = {"plano": [], "storm": []}

    def _plano(ticker, date, timeframe="1d", method="padrao"):
        chamadas["plano"].append((ticker, date, timeframe, method))
        return {"symbol": ticker, "price": 465.58, "method": method}

    def _storm(ticker, date, timeframe="1d"):
        chamadas["storm"].append((ticker, date, timeframe))
        return {"symbol": ticker, "opera": True, "pattern": {"direction": "venda"}}

    monkeypatch.setattr(R, "fetch_actionable_plan", _plano)
    monkeypatch.setattr(R, "fetch_storm_plan", _storm)
    return chamadas


@pytest.mark.parametrize("metodo", ["padrao", "erick", "setup123", "storm123"])
def test_o_plano_da_tela_traz_o_storm_em_QUALQUER_metodo(sem_rede, metodo):
    """DENTE: com ``method="padrao"`` o plano voltava sem ``storm``, e a camada do
    Storm nem existia pra ser ligada."""
    p = R.plano_com_storm(TICKER, DATE, "1d", metodo)
    assert p.get("storm"), (metodo, p)
    assert p["storm"]["pattern"], (metodo, p)
    # ao LADO, nunca no lugar: o que a família Padrão/Erick produziu continua inteiro
    assert p["method"] == metodo and p["price"] == 465.58, p


def test_o_storm_le_o_MESMO_frame_do_plano(sem_rede):
    """Um Storm lido no diário sob um plano de 4h seria a tela comparando dois
    candles diferentes com o mesmo nome."""
    R.plano_com_storm(TICKER, DATE, "4h", "erick")
    assert sem_rede["plano"][-1][2] == "4h", sem_rede["plano"]
    assert sem_rede["storm"][-1][2] == "4h", sem_rede["storm"]


def test_o_plano_original_nao_e_mutado(sem_rede, monkeypatch):
    """O dicionário do produtor pode vir de cache: escrever ``storm`` nele plantaria a
    leitura de um símbolo/frame na entrada de outro."""
    original = {"symbol": TICKER, "price": 1.0}
    monkeypatch.setattr(R, "fetch_actionable_plan", lambda *a, **k: original)
    p = R.plano_com_storm(TICKER, DATE, "1d", "padrao")
    assert p.get("storm"), p
    assert "storm" not in original, ("o plano do produtor foi mutado", original)


def test_falha_do_storm_nao_derruba_o_plano(monkeypatch):
    """Fail-open: sem a leitura do Storm a tela ainda tem o plano inteiro — e o
    ``storm`` vazio é o que faz a camada não se oferecer."""
    monkeypatch.setattr(R, "fetch_actionable_plan",
                        lambda *a, **k: {"symbol": TICKER, "price": 465.58})
    monkeypatch.setattr(R, "fetch_storm_plan", lambda *a, **k: {})
    p = R.plano_com_storm(TICKER, DATE, "1d", "padrao")
    assert p["price"] == 465.58 and p["storm"] == {}, p

"""O ESTADO DO ÉDEN em toda linha de gatilho do 1-2-3 + recortes por classe de
ativo e por Éden no painel método×frame (DA-184, task 20260903-018).

Pergunta que isto serve: o único ingrediente EXCLUSIVO do Storm (MME 8 × MME 80)
filtraria um gatilho do 1-2-3 deste projeto que hoje acerta ≤ passeio aleatório
(DA-184)? O campo ``eden`` é MEDIÇÃO — mesma :func:`price_structure._eden`, mesma
série cacheada (:func:`price_structure._prep`) que o Storm já lê — zero influência
na detecção, no gatilho, no stop ou no alvo do 1-2-3.
"""

import json

import pandas as pd
import pytest

import tradingagents.webui.scanner as sc
from tradingagents.dataflows import price_structure as ps
from tradingagents.webui.scanner import ScanLog, scan_verdicts

pytestmark = pytest.mark.unit


# ------------------------------------------------------------- eden_classe -----
def _serie(precos, n=120):
    """Série com candles suficientes pra MME 80 significar alguma coisa (mesmo
    molde de test_eden_vocabulario.py — reusa a mesma _eden, não reinventa)."""
    datas = pd.date_range("2026-01-01", periods=n, freq="D")
    df = pd.DataFrame({"Date": datas, "Open": precos, "High": [p * 1.005 for p in precos],
                       "Low": [p * 0.995 for p in precos], "Close": precos})
    for j in (ps._STORM_EMA_RAPIDA, ps._STORM_EMA_LENTA):
        df[f"EMA{j}"] = df["Close"].ewm(span=j, adjust=False).mean()
    return df


def test_eden_classe_compra(monkeypatch):
    monkeypatch.setattr(ps, "_prep",
                        lambda *a, **k: _serie([100 + i * 0.8 for i in range(120)]))
    assert ps.eden_classe("X", "2026-09-03", "1d") == "compra"


def test_eden_classe_venda(monkeypatch):
    monkeypatch.setattr(ps, "_prep",
                        lambda *a, **k: _serie([200 - i * 0.8 for i in range(120)]))
    assert ps.eden_classe("X", "2026-09-03", "1d") == "venda"


def test_eden_classe_neutro_na_zona_entre_as_medias(monkeypatch):
    def _com_zona_neutra(*a, **k):
        d = _serie([200 - i * 0.8 for i in range(120)])   # tendência de baixa
        rapida, lenta = float(d["EMA8"].iloc[-1]), float(d["EMA80"].iloc[-1])
        assert rapida < lenta
        meio = (rapida + lenta) / 2
        i = d.index[-1]
        d.loc[i, "Low"], d.loc[i, "High"] = meio * 0.999, meio * 1.001
        d.loc[i, "Open"] = d.loc[i, "Close"] = meio
        return d
    monkeypatch.setattr(ps, "_prep", _com_zona_neutra)
    assert ps.eden_classe("X", "2026-09-03", "1d") == "neutro"


def test_eden_classe_sem_dado_quando_a_serie_falha(monkeypatch):
    def _falha(*a, **k):
        raise RuntimeError("sem série")
    monkeypatch.setattr(ps, "_prep", _falha)
    assert ps.eden_classe("X", "2026-09-03", "1d") == "sem_dado"


def test_eden_classe_sem_dado_com_serie_curta(monkeypatch):
    monkeypatch.setattr(ps, "_prep", lambda *a, **k: _serie([100 + i for i in range(10)], n=10))
    assert ps.eden_classe("X", "2026-09-03", "1d") == "sem_dado"


# ------------------------------------------------------- _frame_row / linha ----
def _plano_123():
    return {"price": 100.0, "setup_state": "aguardar_rompimento",
            "pattern": {"trigger": 110.0, "state": "acionado", "direction": "compra"},
            "invalidation": {"price": 90.0}, "stop": {"price": 88.0},
            "target": {"price": 130.0, "low": None, "high": None},
            "risk_reward": {"rr": 0.9, "note": None, "entry": 110.0,
                            "entry_basis": "gatilho", "risk": 22.0, "reward": 20.0}}


def _plano_storm_vazio():
    return {"pattern": None, "leituras": [], "eden": {"direcao": None, "alinhado": False,
             "rotulo_curto": "desalinhado"}, "opera": False, "motivo": "sem padrão"}


def test_frame_row_do_123_carrega_o_eden_calculado(monkeypatch):
    monkeypatch.setattr(sc, "build_actionable_plan_dict", lambda *a, **k: _plano_123())
    monkeypatch.setattr(sc, "build_storm_plan_dict", lambda *a, **k: _plano_storm_vazio())
    monkeypatch.setattr(sc, "_live_price", lambda *_a, **_k: None)
    monkeypatch.setattr(sc, "eden_classe", lambda *a, **k: "venda")
    linha = sc._frame_row("X", "2026-09-03", "1d", live_price=105.0)
    assert linha["eden"] == "venda"
    # e nenhuma decisão do 123 mudou por causa disso
    assert linha["trigger"] == 110.0 and linha["sl"] == 88.0 and linha["tp"] == 130.0


def test_frame_row_eden_sem_dado_quando_a_medicao_falha(monkeypatch):
    monkeypatch.setattr(sc, "build_actionable_plan_dict", lambda *a, **k: _plano_123())
    monkeypatch.setattr(sc, "build_storm_plan_dict", lambda *a, **k: _plano_storm_vazio())
    monkeypatch.setattr(sc, "_live_price", lambda *_a, **_k: None)
    monkeypatch.setattr(sc, "eden_classe", lambda *a, **k: "sem_dado")
    linha = sc._frame_row("X", "2026-09-03", "1d", live_price=105.0)
    assert linha["eden"] == "sem_dado"


# ------------------------------------------------------------- ScanLog.record --
def test_record_grava_eden_pro_123(tmp_path):
    log = ScanLog(tmp_path / "scans.jsonl")
    log.record({"ticker": "AAA", "frame": "1d", "trigger": 10.0, "sl": 9.0,
                "tp": 12.0, "rr": 2.0, "direction": "compra", "setup": "123",
                "eden": "compra"})
    e = log.entries()[0]
    assert e["eden"] == "compra"


def test_record_grava_eden_pro_storm_tambem(tmp_path):
    log = ScanLog(tmp_path / "scans.jsonl")
    log.record({"ticker": "AAA", "frame": "1d", "trigger": 10.0, "sl": 9.0,
                "tp": 12.0, "rr": 2.0, "direction": "compra", "setup": "storm",
                "entrada": "ponto3", "eden": "compra"})
    e = log.entries()[0]
    assert e["eden"] == "compra" and e["entrada"] == "ponto3"


def test_record_sem_eden_nao_grava_a_chave(tmp_path):
    """Linha antiga (pré-DA-184) não tem o campo — e uma linha NOVA sem eden
    (``eden_classe`` indisponível) não inventa um valor, só omite a chave."""
    log = ScanLog(tmp_path / "scans.jsonl")
    log.record({"ticker": "AAA", "frame": "1d", "trigger": 10.0, "sl": 9.0,
                "tp": 12.0, "rr": 2.0, "direction": "compra", "setup": "123"})
    e = log.entries()[0]
    assert "eden" not in e


# ---------------------------------------------------- método×frame: por_classe -
def _linha(ticker, frame, setup, direction="compra", eden=None, ts="2026-08-20T12:00:00+00:00"):
    return {"ticker": ticker, "frame": frame, "setup": setup, "direction": direction,
           "veredito": "bateu_tp", "fechado": True, "eden": eden, "ts": ts,
           "trigger": 100.0, "sl": 95.0, "tp": 110.0, "rr": 2.0}


def test_por_classe_soma_igual_ao_total():
    verdicts = [
        _linha("BTC-USD", "1h", "storm"),
        _linha("ETH-USD", "1h", "storm"),
        _linha("AAPL", "1h", "storm"),
    ]
    mf = sc._metodo_frame(verdicts, banca=100.0, marco=None)
    total = mf["desde_marco"]["storm"]["1h"]["n"]
    classes = mf["por_classe"]["desde_marco"]["storm"]
    soma = sum(bloco["1h"]["n"] for bloco in classes.values() if "1h" in bloco)
    assert soma == total == 3
    assert set(classes) == {"crypto", "stock"}
    assert classes["crypto"]["1h"]["n"] == 2
    assert classes["stock"]["1h"]["n"] == 1


# ------------------------------------------------------ método×frame: por_eden -
def test_por_eden_classifica_alinhado_contra_neutro_sem_dado():
    verdicts = [
        _linha("AAA", "4h", "123", direction="compra", eden="compra"),   # alinhado
        _linha("BBB", "4h", "123", direction="compra", eden="venda"),    # contra
        _linha("CCC", "4h", "123", direction="venda", eden="neutro"),    # neutro
        _linha("DDD", "4h", "123", direction="compra", eden=None),       # sem_dado (linha antiga)
        # storm não entra no recorte por Éden (é só do 123)
        _linha("EEE", "4h", "storm", direction="compra", eden="compra"),
    ]
    mf = sc._metodo_frame(verdicts, banca=100.0, marco=None)
    total_123 = mf["desde_marco"]["123"]["4h"]["n"]
    eden = mf["por_eden"]["desde_marco"]
    assert "storm" not in eden, "o recorte por Éden é só do 123"
    soma = sum(bloco["4h"]["n"] for bloco in eden.values() if "4h" in bloco)
    assert soma == total_123 == 4
    assert eden["alinhado"]["4h"]["n"] == 1
    assert eden["contra"]["4h"]["n"] == 1
    assert eden["neutro"]["4h"]["n"] == 1
    assert eden["sem_dado"]["4h"]["n"] == 1


def test_metodo_frame_ignora_a_flag_de_estrategia_nos_dois_recortes_novos():
    """DA-183/184: metodo_frame (e agora por_classe/por_eden) decompõe os DOIS
    setups sempre — é medição, não vitrine."""
    log_path_verdicts = [
        _linha("BTC-USD", "1h", "storm"),
        _linha("AAA", "4h", "123", eden="compra"),
    ]
    mf = sc._metodo_frame(log_path_verdicts, banca=100.0, marco=None)
    assert "storm" in mf["por_classe"]["desde_marco"]
    assert "123" in mf["por_classe"]["desde_marco"]


# --------------------------------------------------------- integração ledger ---
def test_scan_verdicts_visiveis_off_ainda_alimenta_por_classe_e_por_eden(tmp_path, monkeypatch):
    """O painel método×frame é medição — decompõe os dois recortes novos mesmo
    quando a flag de estratégia (DA-184, task 017) esconde o storm da vitrine."""
    log = ScanLog(tmp_path / "scans.jsonl")
    with open(log.path, "w", encoding="utf-8") as fh:
        for linha in (
            {"ts": "2026-08-20T12:00:00+00:00", "ticker": "BTC-USD", "frame": "1h",
             "direction": "compra", "trigger": 100.0, "sl": 95.0, "tp": 110.0,
             "rr": 2.0, "setup": "storm"},
            {"ts": "2026-08-20T12:00:01+00:00", "ticker": "AAA", "frame": "4h",
             "direction": "compra", "trigger": 100.0, "sl": 95.0, "tp": 110.0,
             "rr": 2.0, "setup": "123", "eden": "compra"},
        ):
            fh.write(json.dumps(linha) + "\n")

    def _serie_fecha(monkeypatch, candles, preco):
        monkeypatch.setattr(sc, "build_actionable_plan_dict",
                            lambda t, d, timeframe="1d", method="padrao":
                            {"price": preco, "pattern": None, "setup_state": "ativo"})
        monkeypatch.setattr(sc, "build_price_chart",
                            lambda t, d, bars=260, timeframe="1d", method="padrao":
                            {"candles": candles})
        monkeypatch.setattr(sc, "_live_price", lambda ticker: preco)
        monkeypatch.setattr(sc, "earnings_window_status",
                            lambda symbol, curr_date, window_days, asset_type="stock": {})

    def _c(d, h, low):
        return {"d": d, "o": low, "h": h, "l": low, "c": low}

    _serie_fecha(monkeypatch, [{"d": "2026-08-20", "o": 99, "h": 100, "l": 99, "c": 99},
                              {"d": "2026-08-21", "o": 100, "h": 111, "l": 100, "c": 111}],
                preco=112.0)
    out = scan_verdicts(log, "2026-08-22", visiveis=frozenset({"123"}))
    mf = out["paper"]["metodo_frame"]
    assert "storm" in mf["por_classe"]["desde_marco"], "medição ignora a flag"
    assert "123" in mf["por_eden"]["desde_marco"] or mf["por_eden"]["desde_marco"], mf["por_eden"]

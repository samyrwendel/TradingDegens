"""Task 20260903-013 — a régua do LEDGER, corrigida pra a MESMA do card (DA-126).

Quatro defeitos que se somavam no painel de track record / paper:

1. **Gatilho não gateava o desfecho.** ``_primeiro_toque`` contava TP/SL a partir da
   barra seguinte ao log sem exigir que o preço tocasse o GATILHO antes — então um
   padrão que morria SEM nunca entrar (o preço foi ao stop sem romper o gatilho)
   virava ``bateu_sl``. Medido em 03/09: 8 dos 41 stops do Storm e 1 dos 7 do
   Setup123 eram padrão que nunca acionou. O card (``_morte_e_desfecho``) já exigia.
2. **Leitura sem dedup.** 12 linhas do MESMO padrão (o Storm diário do BTC-USD, o
   gatilho perseguindo a máxima do dia) contavam 12 × −1R por um único stop.
3. **Rótulo perdido.** ``entrada`` virou PREÇO em 02/09 e engoliu o ``ponto2``/
   ``ponto3`` do Storm — que agora tem campo próprio, ``leitura``.
4. **Acerto alto lido como lucro.** 75% de acerto com R:R 0,20 dá E[R] NEGATIVO. A
   grade método×frame passa a expor n, acerto, E[R] e o PnL nas DUAS leituras.
"""

import json

import pytest

from tradingagents.webui import scanner as sc
from tradingagents.webui.scanner import (
    ScanLog,
    _bloco_metodo_frame,  # noqa: F401 — importado pra garantir que o símbolo existe
    _dedup_por_estrutura,
    _metodo_frame,
    scan_verdicts,
)

pytestmark = pytest.mark.unit


def _c(d, h, low):
    return {"d": d, "o": low, "h": h, "l": low, "c": low}


def _log_com(tmp_path, *entradas):
    path = tmp_path / "scans.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for e in entradas:
            fh.write(json.dumps(e) + "\n")
    return ScanLog(path)


def _serie(monkeypatch, candles_por_ticker, preco):
    monkeypatch.setattr(sc, "build_actionable_plan_dict",
                        lambda t, d, timeframe="1d", method="padrao":
                        {"price": preco.get(t), "pattern": None, "setup_state": "ativo"})
    monkeypatch.setattr(sc, "build_price_chart",
                        lambda t, d, bars=260, timeframe="1d", method="padrao":
                        {"candles": candles_por_ticker.get(t, [])})
    monkeypatch.setattr(sc, "_live_price", lambda ticker: preco.get(ticker))
    monkeypatch.setattr(sc, "earnings_window_status",
                        lambda symbol, curr_date, window_days, asset_type="stock": {})


# ── 1) o gatilho gateia o desfecho ──────────────────────────────────────────
def test_padrao_que_morre_antes_do_gatilho_e_nunca_acionou_nao_stop(tmp_path, monkeypatch):
    """Compra cujo gatilho (100) NUNCA rompe — o preço cai direto ao nível do stop
    (95). Sem entrada não há trade: ``nunca_acionou``, não ``bateu_sl``.
    DENTE: a régua velha (sem gatilho) fechava isso como perda."""
    log = _log_com(tmp_path, {
        "ts": "2026-08-20T12:00:00+00:00", "ticker": "BTC-USD", "frame": "1d",
        "direction": "compra", "trigger": 100.0, "sl": 95.0, "tp": 110.0, "rr": 2.0,
        "setup": "storm"})
    _serie(monkeypatch, {"BTC-USD": [_c("2026-08-21", 98.0, 94.0)]}, {"BTC-USD": 96.0})
    v = scan_verdicts(log, "2026-08-28")["verdicts"][0]
    assert v["veredito"] == "nunca_acionou", v
    assert v["fechado"] is True                 # terminal: o padrão morreu
    # e FICA fora da taxa de acerto e do PnL — não é trade
    out = scan_verdicts(log, "2026-08-29")
    assert out["n_fechados"] == 0 and out["taxa_acerto"] is None


def test_gatilho_tocado_e_depois_stop_conta_bateu_sl(tmp_path, monkeypatch):
    """Rompeu o gatilho (barra 1) e stopou depois (barra 2): trade REAL, ``bateu_sl``."""
    log = _log_com(tmp_path, {
        "ts": "2026-08-20T12:00:00+00:00", "ticker": "C", "frame": "1d",
        "direction": "compra", "trigger": 100.0, "sl": 95.0, "tp": 110.0, "rr": 2.0})
    _serie(monkeypatch, {"C": [_c("2026-08-21", 101.0, 96.0),
                               _c("2026-08-22", 100.0, 94.0)]}, {"C": 95.0})
    v = scan_verdicts(log, "2026-08-28")["verdicts"][0]
    assert v["veredito"] == "bateu_sl", v


def test_gatilho_e_alvo_na_mesma_barra_e_trade_ganho(tmp_path, monkeypatch):
    """Grão do ledger = 1 barra: um candle que rompe o gatilho e toca o alvo no mesmo
    passo entrou E ganhou — não vira nunca_acionou por tecnicismo."""
    log = _log_com(tmp_path, {
        "ts": "2026-08-20T12:00:00+00:00", "ticker": "C", "frame": "1d",
        "direction": "compra", "trigger": 100.0, "sl": 95.0, "tp": 110.0, "rr": 2.0})
    _serie(monkeypatch, {"C": [_c("2026-08-21", 111.0, 99.0)]}, {"C": 105.0})
    v = scan_verdicts(log, "2026-08-28")["verdicts"][0]
    assert v["veredito"] == "bateu_tp", v


def test_setup_ja_acionado_no_log_nao_e_gateado(tmp_path, monkeypatch):
    """Log já ACIONADO: a entrada é o PREÇO, o ``trigger`` é um nível velho. Não se
    re-exige seu toque — senão um trade que estava aberto viraria nunca_acionou."""
    log = _log_com(tmp_path, {
        "ts": "2026-08-20T12:00:00+00:00", "ticker": "A", "frame": "1d",
        "direction": "compra", "pattern_state": "acionado",
        "trigger": 100.0, "sl": 95.0, "tp": 99.8, "rr": 0.4})
    _serie(monkeypatch, {"A": [_c("2026-08-21", 99.9, 99.5)]}, {"A": 99.9})
    v = scan_verdicts(log, "2026-08-28")["verdicts"][0]
    assert v["veredito"] == "bateu_tp", v


def test_venda_que_morre_antes_do_gatilho_tambem_e_nunca_acionou(tmp_path, monkeypatch):
    """Espelho na venda: gatilho ABAIXO (rompimento pra baixo, 100), stop ACIMA
    (105). O preço sobe ao stop sem nunca romper pra baixo → nunca acionou."""
    log = _log_com(tmp_path, {
        "ts": "2026-08-20T12:00:00+00:00", "ticker": "V", "frame": "1d",
        "direction": "venda", "trigger": 100.0, "sl": 105.0, "tp": 90.0, "rr": 2.0,
        "setup": "storm"})
    _serie(monkeypatch, {"V": [_c("2026-08-21", 106.0, 101.0)]}, {"V": 104.0})
    v = scan_verdicts(log, "2026-08-28")["verdicts"][0]
    assert v["veredito"] == "nunca_acionou", v


# ── 2) dedup por estrutura ──────────────────────────────────────────────────
def _reais_btc(n, veredito="bateu_sl"):
    """n linhas do MESMO padrão: BTC-USD 1d compra, SL 76909.35 (o gatilho chasing a
    máxima muda a cada linha, a ESTRUTURA — setup/ticker/frame/direção/SL — não)."""
    return [{"veredito": veredito, "setup": "storm", "ticker": "BTC-USD", "frame": "1d",
             "direction": "compra", "trigger": 77000.0 + i, "sl": 76909.35,
             "tp": 78000.0, "rr": 1.5, "ts": f"2026-08-30T{10 + i:02d}:00:00+00:00",
             "fechado_em": "2026-08-31"} for i in range(n)]


def test_dedup_colapsa_a_mesma_estrutura_a_uma_linha():
    dedup = _dedup_por_estrutura(_reais_btc(12))
    assert len(dedup) == 1, "12 re-logs do MESMO padrão são UMA ordem"
    assert dedup[0]["ts"] == "2026-08-30T10:00:00+00:00"   # a primeira (mais antiga)


def test_carteira_paper_conta_a_estrutura_uma_vez():
    c = sc._carteira_paper(_reais_btc(12), banca=100.0, marco=None)
    assert c["n_fechadas"] == 1, "o BTC 1d de 12 linhas conta UMA vez na carteira"


# ── 3) campo leitura (rótulo ponto2/ponto3) ─────────────────────────────────
def test_ledger_grava_leitura_separada_do_preco(tmp_path):
    log = ScanLog(tmp_path / "scans.jsonl")
    log.record({"ticker": "X", "frame": "1d", "trigger": 105.0, "sl": 90.0,
                "tp": 128.0, "rr": 1.3, "direction": "compra", "setup": "storm",
                "entrada": 105.0, "leitura": "ponto3"})
    log.record({"ticker": "Y", "frame": "1d", "trigger": 10.0, "sl": 9.0,
                "tp": 12.0, "rr": 2.0, "direction": "compra", "setup": "123"})
    e = log.entries()
    assert e[0]["entrada"] == 105.0 and e[0]["leitura"] == "ponto3"
    assert "leitura" not in e[1], "1-2-3 não tem leitura — o campo é do Storm"


# ── 4) a grade método × frame ───────────────────────────────────────────────
def _real(setup, frame, veredito, rr, ts, ticker, sl=95.0, trigger=100.0, tp=110.0):
    return {"veredito": veredito, "setup": setup, "frame": frame, "ticker": ticker,
            "direction": "compra", "trigger": trigger, "sl": sl, "tp": tp, "rr": rr,
            "ts": ts, "fechado_em": ts[:10]}


def test_metodo_frame_expoe_n_acerto_er_e_as_duas_leituras_de_pnl():
    """A armadilha do Setup123 em teste: 75% de acerto (3 TP / 1 SL) com R:R 0,20 dá
    E[R] NEGATIVO. O bloco carrega acerto, E[R] E as duas curvas de dinheiro."""
    verdicts = [
        _real("123", "1d", "bateu_tp", 0.2, "2026-09-04T10:00:00+00:00", "AAA"),
        _real("123", "1d", "bateu_tp", 0.2, "2026-09-04T11:00:00+00:00", "BBB"),
        _real("123", "1d", "bateu_tp", 0.2, "2026-09-04T12:00:00+00:00", "CCC"),
        _real("123", "1d", "bateu_sl", 0.2, "2026-09-04T13:00:00+00:00", "DDD"),
    ]
    mf = _metodo_frame(verdicts, banca=100.0, marco="2026-09-03T00:00:00+00:00")
    bloco = mf["desde_marco"]["123"]["1d"]
    assert bloco["n"] == 4 and bloco["taxa_acerto"] == 0.75
    assert bloco["expectativa_r"] < 0, bloco       # acerto alto NÃO cobre o R:R
    assert bloco["acerto_equilibrio"] == pytest.approx(1 / 1.2, abs=1e-3)
    assert bloco["pnl_fixo_usd"] is not None       # posição fixa
    # risco fixo: 3 alvos (+0,2×100 cada) + 1 stop (−100) = 60 − 100 = −40
    assert bloco["pnl_risco_fixo_usd"] == pytest.approx(-40.0, abs=1e-6)


def test_metodo_frame_separa_o_marco_do_historico_antes_da_regua():
    """O marco = data do fix: o histórico anterior (BTC storm 1d) fica VISÍVEL em
    ``antes_da_regua``, deduplicado a 1, e o trade novo conta pro gate."""
    marco = "2026-09-03T00:00:00+00:00"
    verdicts = _reais_btc(3) + [
        _real("123", "1h", "bateu_tp", 1.0, "2026-09-05T10:00:00+00:00", "NOVO")]
    mf = _metodo_frame(verdicts, banca=100.0, marco=marco)
    assert mf["antes_da_regua"]["storm"]["1d"]["n"] == 1     # dedup do histórico
    assert mf["desde_marco"]["123"]["1h"]["n"] == 1          # o novo conta pro gate
    assert "storm" not in mf["desde_marco"]                  # nada Storm pós-marco
    assert "123" not in mf["antes_da_regua"]                 # nada 123 pré-marco


def test_scan_verdicts_expoe_metodo_frame(tmp_path, monkeypatch):
    """Fim a fim: ``out["paper"]["metodo_frame"]`` existe com os dois recortes."""
    log = _log_com(tmp_path, [] if False else {
        "ts": "2026-08-20T12:00:00+00:00", "ticker": "C", "frame": "1d",
        "direction": "compra", "trigger": 100.0, "sl": 95.0, "tp": 110.0, "rr": 2.0})
    _serie(monkeypatch, {"C": [_c("2026-08-21", 111.0, 99.0)]}, {"C": 105.0})
    mf = scan_verdicts(log, "2026-08-28")["paper"]["metodo_frame"]
    assert set(mf) >= {"marco", "desde_marco", "antes_da_regua"}
    assert mf["desde_marco"]["123"]["1d"]["n"] == 1   # sem marco, tudo é "desde_marco"

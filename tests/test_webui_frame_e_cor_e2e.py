"""A COR TEM DE CONCORDAR COM O NÚMERO, e o FRAME tem de dizer se decide (006).

Quatro prints do MESMO ativo — 1h, diário e 4h, preço 218,40, análise de 28/08,
veredito no 4h. Medido neles:

    1h  → alvo 219,35 · invalidação 210,53 · SL 207,00 · recuo MMS20 211,27
    D   → alvo 227,50 · invalidação 181,00 · SL 175,09 · recuo MMS20 208,95 · R:R 0,21
    4h  → alvo 219,35 · invalidação 181,32 · SL 176,83 · recuo MMS50 178,38

O stop vai de 207,00 a 175,09. São **três trades diferentes na mesma tela**, e
trocar o chip de tempo trocava o plano inteiro com o mesmo peso visual — nada
dizendo qual vale. Existe o carimbo "veredito no 4h" lá no topo, mas ele sai da
tela quando se rola até o gráfico, que é onde a decisão é tomada.

E dois defeitos de SEMÂNTICA, que são piores que o aperto de layout porque a tela
afirma o contrário do que o dado diz:

  (a) ``R:R 0,21:1`` saía em VERDE dentro da faixa verde do alvo. 0,21 é arriscar
      ~5x o que se pretende ganhar, e verde é o vocabulário de "pode ir" na tela
      inteira;
  (b) ``recuo à média (MMS20) — não ativa agora`` também saía em VERDE, com o
      próprio texto dizendo que NÃO está ativo.

Mais dois de informação que falta:

  (c) o R:R só apareceu no diário; no 1h e no 4h não havia número **nem palavra**;
  (d) o carimbo da análise muda de hora por frame (é o último candle daquele
      frame) e, sem rótulo, parecia inconsistência de dado.

Cada teste aqui mede uma dessas quatro coisas no navegador, no desktop e no
telefone — nenhum audita CSS por seletor, porque seletor não diz o que aparece.
"""

import json
import re
import threading

import pytest

from tradingagents.webui import timeutil
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.server import make_server
from tradingagents.webui.store import HistoryStore

pytestmark = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None

_HOJE = timeutil.today()

TELEFONE = {"width": 390, "height": 844}
DESKTOP = {"width": 1500, "height": 1100}


def _verde(hexa):
    """A cor é do FAMÍLIA VERDE? Mede o canal, não o nome — é o que o olho lê.

    Serve pra provar "R:R < 1 nunca em verde" sem prender o teste a um hex
    específico: trocar #26de81 por outro verde continuaria reprovando, que é o
    comportamento certo.
    """
    h = str(hexa or "").lstrip("#")
    if len(h) != 6:
        return False
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return g > r + 30 and g > b + 30


# ── o plano do 4h (o do VEREDITO) e o do 1h (exploratório), com os números reais ──
def _plano(frame, *, sl, inval, alvo, ma, ma_preco, rr):
    """Um plano por frame. O ponto do teste é que eles DISCORDAM — é o dado real."""
    return {
        "symbol": "AMD", "price": 218.40, "as_of": f"2026-08-28 {frame['hora']}",
        "timeframe": frame["longo"], "horizon": "dias", "setup_state": "aguardar_rompimento",
        "setup_source": "123",
        "buy_zone": {"label": f"{ma} — preço acima da média, fora da faixa",
                     "price": ma_preco, "low": ma_preco - 4, "high": ma_preco + 4,
                     "band_basis": "±0.5·ATR14", "ma_label": ma, "setup": "recuo_media",
                     "tag": f"recuo à média ({ma})", "active_now": False,
                     "distance_pct": 3.4},
        "realize_zone": None, "pullback_zone": None,
        "pattern": {"p1": {"date": "2026-06-10", "price": 190.0},
                    "p2": {"date": "2026-07-02", "price": alvo},
                    "p3": {"date": "2026-07-20", "price": inval},
                    "trigger": alvo, "state": "formando", "direction": "compra"},
        "invalidation": {"label": "perda do ponto 3", "price": inval,
                         "meaning": f"o setup morre se perder {inval:,.2f}"},
        "stop": {"label": "stop (SL)", "price": sl, "anchor": inval, "atr": 5.0,
                 "basis": "invalidação + folga de 0.5·ATR14"},
        # Sem R:R o alvo TAMBÉM não existe — é a mesma ausência, e é o que o backend
        # produz: sem topo anterior à frente da entrada não há alvo NEM razão.
        "target": ({"label": "topo anterior", "price": 227.50, "same_as_realize": False}
                   if (rr or {}).get("rr") is not None else None),
        "risk_reward": rr,
    }


_F4H = {"longo": "4 horas (intradiário)", "hora": "17:30"}
_F1H = {"longo": "1 hora (intradiário)", "hora": "19:30"}
_FD = {"longo": "diário (referência)", "hora": "00:00"}

# 4h: o frame do VEREDITO. Sem topo anterior à frente da entrada → o backend manda
# rr=None COM o motivo escrito (é o caso dos prints: R:R mudo no 1h e no 4h).
_ACT_4H = _plano(_F4H, sl=176.83, inval=181.32, alvo=219.35, ma="MMS50", ma_preco=178.38,
                 rr={"entry": 219.35, "entry_basis": "gatilho", "risk": 42.52,
                     "reward": None, "rr": None,
                     "note": "sem alvo estrutural à frente da entrada — não há topo "
                             "anterior acima dela nesta série, então não há retorno "
                             "a projetar (o risco continua medido)."})
# 1h: OUTRO trade — stop 207,00 contra 176,83. É a discordância do print.
_ACT_1H = _plano(_F1H, sl=207.00, inval=210.53, alvo=219.35, ma="MMS20", ma_preco=211.27,
                 rr={"entry": 219.35, "entry_basis": "gatilho", "risk": 12.35,
                     "reward": None, "rr": None, "note": "sem alvo estrutural à frente da entrada."})
# diário: o R:R que EXISTE e é ruim — 0,21 é arriscar ~4,8x o que se pretende ganhar.
_ACT_D = _plano(_FD, sl=175.09, inval=181.00, alvo=227.50, ma="MMS20", ma_preco=208.95,
                rr={"entry": 219.35, "entry_basis": "gatilho", "risk": 44.26,
                    "reward": 8.15, "rr": 0.21, "note": None})


def _snap(actionable, tf="4h"):
    return {
        "run_id": "R-006", "ticker": "AMD", "date": "2026-08-29", "asset_type": "stock",
        "status": "done", "elapsed": 3, "cost": {"usd": 0.0},
        "verdict": None, "verdict_timeframe": "4h",
        "result": {
            "setup123": True, "verdict": None, "final_decision": "",
            "timeframe": tf, "as_of_price": 218.40, "actionable": actionable,
            "live_price": {"price": 218.40, "change_pct": 1.1, "currency": "USD",
                           "sessao": "fechado", "rotulo": "último fechamento",
                           "as_of": "29/08 16:00", "regular_price": 218.40,
                           "fuso": "America/New_York", "em": _HOJE},
            "price_chart": dict(_CHART, timeframe=tf), "degraded": [],
            "bull": "", "bear": "", "research_manager": "", "investment_plan": "",
            "trader_plan": "", "risk_decision": "", "market_report": "",
            "sentiment_report": "", "news_report": "", "fundamentals_report": "",
            "erick_report": "", "drop_nature": {}, "derivatives_report": "",
        },
    }


# Série sintética só pra o card do gráfico EXISTIR (ele se esconde com menos de 3
# candles, e sem ele não há seletor de frame pra clicar). Os números que os testes
# medem não saem daqui — saem do plano de cada frame.
_CANDLES = [{"o": 175.0, "h": 177.0, "l": 172.3, "c": 174.3, "d": "2026-01-01"}, {"o": 174.3, "h": 176.85, "l": 172.3, "c": 174.85, "d": "2026-01-02"}, {"o": 174.85, "h": 178.65, "l": 172.85, "c": 176.65, "d": "2026-01-03"}, {"o": 176.65, "h": 181.69, "l": 174.65, "c": 179.69, "d": "2026-01-04"}, {"o": 179.69, "h": 183.14, "l": 177.69, "c": 181.14, "d": "2026-01-05"}, {"o": 181.14, "h": 185.78, "l": 179.14, "c": 183.78, "d": "2026-01-06"}, {"o": 183.78, "h": 189.57, "l": 181.78, "c": 187.57, "d": "2026-01-07"}, {"o": 187.57, "h": 194.48, "l": 185.57, "c": 192.48, "d": "2026-01-08"}, {"o": 192.48, "h": 197.66, "l": 190.48, "c": 195.66, "d": "2026-01-09"}, {"o": 195.66, "h": 201.87, "l": 193.66, "c": 199.87, "d": "2026-01-10"}, {"o": 199.87, "h": 207.05, "l": 197.87, "c": 205.05, "d": "2026-01-11"}, {"o": 205.05, "h": 213.15, "l": 203.05, "c": 211.15, "d": "2026-01-12"}, {"o": 211.15, "h": 217.31, "l": 209.15, "c": 215.31, "d": "2026-01-13"}, {"o": 215.31, "h": 222.27, "l": 213.31, "c": 220.27, "d": "2026-01-14"}, {"o": 220.27, "h": 227.97, "l": 218.27, "c": 225.97, "d": "2026-01-15"}, {"o": 225.97, "h": 234.35, "l": 223.97, "c": 232.35, "d": "2026-01-16"}, {"o": 232.35, "h": 238.54, "l": 230.35, "c": 236.54, "d": "2026-01-17"}, {"o": 236.54, "h": 243.29, "l": 234.54, "c": 241.29, "d": "2026-01-18"}, {"o": 241.29, "h": 248.54, "l": 239.29, "c": 246.54, "d": "2026-01-19"}, {"o": 246.54, "h": 254.23, "l": 244.54, "c": 252.23, "d": "2026-01-20"}, {"o": 252.23, "h": 257.51, "l": 250.23, "c": 255.51, "d": "2026-01-21"}, {"o": 255.51, "h": 261.13, "l": 253.51, "c": 259.13, "d": "2026-01-22"}, {"o": 259.13, "h": 265.04, "l": 257.13, "c": 263.04, "d": "2026-01-23"}, {"o": 263.04, "h": 269.21, "l": 261.04, "c": 267.21, "d": "2026-01-24"}, {"o": 267.21, "h": 270.8, "l": 265.21, "c": 268.8, "d": "2026-01-25"}, {"o": 268.8, "h": 272.58, "l": 266.8, "c": 270.58, "d": "2026-01-26"}, {"o": 270.58, "h": 274.53, "l": 268.58, "c": 272.53, "d": "2026-01-27"}, {"o": 272.53, "h": 276.64, "l": 270.53, "c": 274.64, "d": "2026-01-28"}, {"o": 274.64, "h": 276.64, "l": 272.09, "c": 274.09, "d": "2026-02-01"}, {"o": 274.09, "h": 276.09, "l": 271.69, "c": 273.69, "d": "2026-02-02"}, {"o": 273.69, "h": 275.69, "l": 271.44, "c": 273.44, "d": "2026-02-03"}, {"o": 273.44, "h": 275.44, "l": 271.35, "c": 273.35, "d": "2026-02-04"}, {"o": 273.35, "h": 275.35, "l": 268.64, "c": 270.64, "d": "2026-02-05"}, {"o": 270.64, "h": 272.64, "l": 266.13, "c": 268.13, "d": "2026-02-06"}, {"o": 268.13, "h": 270.13, "l": 263.86, "c": 265.86, "d": "2026-02-07"}, {"o": 265.86, "h": 267.86, "l": 261.86, "c": 263.86, "d": "2026-02-08"}, {"o": 263.86, "h": 265.86, "l": 257.38, "c": 259.38, "d": "2026-02-09"}, {"o": 259.38, "h": 261.38, "l": 253.26, "c": 255.26, "d": "2026-02-10"}, {"o": 255.26, "h": 257.26, "l": 249.55, "c": 251.55, "d": "2026-02-11"}, {"o": 251.55, "h": 253.55, "l": 246.3, "c": 248.3, "d": "2026-02-12"}, {"o": 248.3, "h": 250.3, "l": 240.78, "c": 242.78, "d": "2026-02-13"}, {"o": 242.78, "h": 244.78, "l": 235.84, "c": 237.84, "d": "2026-02-14"}, {"o": 237.84, "h": 239.84, "l": 231.55, "c": 233.55, "d": "2026-02-15"}, {"o": 233.55, "h": 235.55, "l": 227.96, "c": 229.96, "d": "2026-02-16"}, {"o": 229.96, "h": 231.96, "l": 222.34, "c": 224.34, "d": "2026-02-17"}, {"o": 224.34, "h": 226.34, "l": 217.55, "c": 219.55, "d": "2026-02-18"}, {"o": 219.55, "h": 221.55, "l": 213.64, "c": 215.64, "d": "2026-02-19"}, {"o": 215.64, "h": 217.64, "l": 210.68, "c": 212.68, "d": "2026-02-20"}, {"o": 212.68, "h": 214.68, "l": 205.91, "c": 207.91, "d": "2026-02-21"}, {"o": 207.91, "h": 209.91, "l": 202.19, "c": 204.19, "d": "2026-02-22"}, {"o": 204.19, "h": 206.19, "l": 199.56, "c": 201.56, "d": "2026-02-23"}, {"o": 201.56, "h": 203.56, "l": 198.07, "c": 200.07, "d": "2026-02-24"}, {"o": 200.07, "h": 202.07, "l": 194.95, "c": 196.95, "d": "2026-02-25"}, {"o": 196.95, "h": 198.95, "l": 193.03, "c": 195.03, "d": "2026-02-26"}, {"o": 195.03, "h": 197.03, "l": 192.33, "c": 194.33, "d": "2026-02-27"}, {"o": 194.33, "h": 196.87, "l": 192.33, "c": 194.87, "d": "2026-02-28"}, {"o": 194.87, "h": 196.87, "l": 191.87, "c": 193.87, "d": "2026-03-01"}, {"o": 193.87, "h": 196.12, "l": 191.87, "c": 194.12, "d": "2026-03-02"}, {"o": 194.12, "h": 197.62, "l": 192.12, "c": 195.62, "d": "2026-03-03"}, {"o": 195.62, "h": 200.37, "l": 193.62, "c": 198.37, "d": "2026-03-04"}, {"o": 198.37, "h": 201.54, "l": 196.37, "c": 199.54, "d": "2026-03-05"}, {"o": 199.54, "h": 203.91, "l": 197.54, "c": 201.91, "d": "2026-03-06"}, {"o": 201.91, "h": 207.46, "l": 199.91, "c": 205.46, "d": "2026-03-07"}, {"o": 205.46, "h": 212.14, "l": 203.46, "c": 210.14, "d": "2026-03-08"}, {"o": 210.14, "h": 215.12, "l": 208.14, "c": 213.12, "d": "2026-03-09"}, {"o": 213.12, "h": 219.15, "l": 211.12, "c": 217.15, "d": "2026-03-10"}, {"o": 217.15, "h": 224.19, "l": 215.15, "c": 222.19, "d": "2026-03-11"}, {"o": 222.19, "h": 230.18, "l": 220.19, "c": 228.18, "d": "2026-03-12"}, {"o": 228.18, "h": 234.26, "l": 226.18, "c": 232.26, "d": "2026-03-13"}, {"o": 232.26, "h": 239.17, "l": 230.26, "c": 237.17, "d": "2026-03-14"}, {"o": 237.17, "h": 244.86, "l": 235.17, "c": 242.86, "d": "2026-03-15"}, {"o": 242.86, "h": 251.26, "l": 240.86, "c": 249.26, "d": "2026-03-16"}, {"o": 249.26, "h": 255.51, "l": 247.26, "c": 253.51, "d": "2026-03-17"}, {"o": 253.51, "h": 260.35, "l": 251.51, "c": 258.35, "d": "2026-03-18"}, {"o": 258.35, "h": 265.71, "l": 256.35, "c": 263.71, "d": "2026-03-19"}, {"o": 263.71, "h": 271.55, "l": 261.71, "c": 269.55, "d": "2026-03-20"}, {"o": 269.55, "h": 275.0, "l": 267.55, "c": 273.0, "d": "2026-03-21"}, {"o": 273.0, "h": 278.82, "l": 271.0, "c": 276.82, "d": "2026-03-22"}, {"o": 276.82, "h": 282.96, "l": 274.82, "c": 280.96, "d": "2026-03-23"}, {"o": 280.96, "h": 287.37, "l": 278.96, "c": 285.37, "d": "2026-03-24"}, {"o": 285.37, "h": 289.22, "l": 283.37, "c": 287.22, "d": "2026-03-25"}, {"o": 287.22, "h": 291.28, "l": 285.22, "c": 289.28, "d": "2026-03-26"}, {"o": 289.28, "h": 293.52, "l": 287.28, "c": 291.52, "d": "2026-03-27"}, {"o": 291.52, "h": 295.93, "l": 289.52, "c": 293.93, "d": "2026-03-28"}, {"o": 293.93, "h": 295.93, "l": 291.69, "c": 293.69, "d": "2026-04-01"}, {"o": 293.69, "h": 295.69, "l": 291.59, "c": 293.59, "d": "2026-04-02"}, {"o": 293.59, "h": 295.64, "l": 291.59, "c": 293.64, "d": "2026-04-03"}, {"o": 293.64, "h": 295.84, "l": 291.64, "c": 293.84, "d": "2026-04-04"}, {"o": 293.84, "h": 295.84, "l": 289.41, "c": 291.41, "d": "2026-04-05"}, {"o": 291.41, "h": 293.41, "l": 287.17, "c": 289.17, "d": "2026-04-06"}, {"o": 289.17, "h": 291.17, "l": 285.15, "c": 287.15, "d": "2026-04-07"}, {"o": 287.15, "h": 289.15, "l": 283.38, "c": 285.38, "d": "2026-04-08"}, {"o": 285.38, "h": 287.38, "l": 279.1, "c": 281.1, "d": "2026-04-09"}, {"o": 281.1, "h": 283.1, "l": 275.16, "c": 277.16, "d": "2026-04-10"}, {"o": 277.16, "h": 279.16, "l": 271.6, "c": 273.6, "d": "2026-04-11"}, {"o": 273.6, "h": 275.6, "l": 268.48, "c": 270.48, "d": "2026-04-12"}, {"o": 270.48, "h": 272.48, "l": 263.05, "c": 265.05, "d": "2026-04-13"}, {"o": 265.05, "h": 267.05, "l": 258.17, "c": 260.17, "d": "2026-04-14"}, {"o": 260.17, "h": 262.17, "l": 253.9, "c": 255.9, "d": "2026-04-15"}, {"o": 255.9, "h": 257.9, "l": 250.3, "c": 252.3, "d": "2026-04-16"}, {"o": 252.3, "h": 254.3, "l": 244.63, "c": 246.63, "d": "2026-04-17"}, {"o": 246.63, "h": 248.63, "l": 239.76, "c": 241.76, "d": "2026-04-18"}, {"o": 241.76, "h": 243.76, "l": 235.74, "c": 237.74, "d": "2026-04-19"}, {"o": 237.74, "h": 239.74, "l": 232.64, "c": 234.64, "d": "2026-04-20"}, {"o": 234.64, "h": 236.64, "l": 227.7, "c": 229.7, "d": "2026-04-21"}, {"o": 229.7, "h": 231.7, "l": 223.78, "c": 225.78, "d": "2026-04-22"}, {"o": 225.78, "h": 227.78, "l": 220.93, "c": 222.93, "d": "2026-04-23"}, {"o": 222.93, "h": 224.93, "l": 219.2, "c": 221.2, "d": "2026-04-24"}, {"o": 221.2, "h": 223.2, "l": 215.82, "c": 217.82, "d": "2026-04-25"}, {"o": 217.82, "h": 219.82, "l": 213.62, "c": 215.62, "d": "2026-04-26"}, {"o": 215.62, "h": 217.62, "l": 212.63, "c": 214.63, "d": "2026-04-27"}, {"o": 214.63, "h": 216.88, "l": 212.63, "c": 214.88, "d": "2026-04-28"}, {"o": 214.88, "h": 216.88, "l": 211.57, "c": 213.57, "d": "2026-05-01"}, {"o": 213.57, "h": 215.57, "l": 211.52, "c": 213.52, "d": "2026-05-02"}, {"o": 213.52, "h": 216.72, "l": 211.52, "c": 214.72, "d": "2026-05-03"}, {"o": 214.72, "h": 219.17, "l": 212.72, "c": 217.17, "d": "2026-05-04"}, {"o": 217.17, "h": 220.05, "l": 215.17, "c": 218.05, "d": "2026-05-05"}, {"o": 218.05, "h": 222.15, "l": 216.05, "c": 220.15, "d": "2026-05-06"}, {"o": 220.15, "h": 225.44, "l": 218.15, "c": 223.44, "d": "2026-05-07"}, {"o": 223.44, "h": 229.89, "l": 221.44, "c": 227.89, "d": "2026-05-08"}]

_CHART = {"symbol": "AMD", "candles": _CANDLES, "ma": {}, "ema": {},
          "ma_windows": [], "ema_windows": [],
          "markers": {"buy_regions": [], "active_region": None, "pattern_123": None}}


def sobe_servidor(tmp_path):
    """O servidor de teste, como FUNÇÃO e não como fixture — quem precisa dele noutro
    arquivo declara a sua própria fixture chamando isto. Importar fixture de módulo de
    teste funciona, mas faz o parâmetro `base` de cada teste parecer redefinição do
    import (ruff F811), e um aviso repetido que se aprende a ignorar é pior que a
    duplicação de três linhas."""
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "openai",
                     "deep_think_llm": "x", "quick_think_llm": "y"},
        store=HistoryStore(tmp_path))
    httpd = make_server("127.0.0.1", 0, runner=runner)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


@pytest.fixture
def base(tmp_path):
    yield from sobe_servidor(tmp_path)


def _abre(page, base_url, actionable=None, viewport=None):
    """Abre a run no frame do VEREDITO (4h) e deixa /api/chart pronto pro 1h."""
    snap = _snap(actionable if actionable is not None else _ACT_4H)

    def handler(route):
        url = route.request.url
        if "/api/chart" in url:
            tf = (re.search(r"[?&]tf=([^&]+)", url) or [None, "1d"])[1]
            plano = {"1h": _ACT_1H, "1d": _ACT_D}.get(tf, _ACT_4H)
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "timeframe": tf, "timeframes": ["1w", "1d", "4h", "1h", "15m"],
                "actionable": plano, "price_chart": {**_CHART, "timeframe": tf},
                # SEM `live_price` — de propósito: o /api/chart de verdade não o
                # devolve (`runner.timeframe_view`). Uma fixture mais generosa que a
                # rota real escondia justamente o defeito da task 007, em que a
                # cotação sumia da tira ao trocar de frame.
                "degraded": []}))
        elif "/api/status/" in url or re.search(r"/api/run/[^/]+$", url):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(snap))
        else:
            route.continue_()
    page.route(re.compile(r"/api/"), handler)
    page.goto(base_url, wait_until="networkidle")
    page.evaluate("() => watchRun('R-006')")
    page.wait_for_selector("#setupCards:not(.hidden)")
    page.wait_for_timeout(150)


def _troca_frame(page, tf):
    page.click(f'.tf-btn[data-tf="{tf}"]')
    page.wait_for_function(f"() => _tf === '{tf}'")
    page.wait_for_timeout(200)


# ─────────────── 1. o frame que NÃO decidiu se declara exploratório ───────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_frame_do_veredito_nao_e_exploratorio(base):
    """O outro lado da régua primeiro: no frame que DECIDIU não há tarja nenhuma.
    Um aviso que aparece sempre não avisa nada."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        m = page.evaluate("""() => ({
          tf: _tf, veredito: _verdictTf,
          tarja: document.querySelectorAll('#setupCards .sc-explor').length,
          classe: document.getElementById('setupCards').className,
          carimbo: document.getElementById('priceChart').dataset.tf || '',
        })""")
        assert m["tf"] == m["veredito"] == "4h", m
        assert m["tarja"] == 0, ("tarja de exploratório no frame que decidiu", m)
        assert "is-exploratorio" not in m["classe"], m
        assert "exploratório" not in m["carimbo"], m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [DESKTOP, TELEFONE], ids=["desktop", "telefone"])
def test_trocar_de_frame_declara_que_aquilo_nao_e_o_plano_da_decisao(base, viewport):
    """DENTE: o 1h vinha pintado igual ao 4h — mesmo peso, mesma borda — enquanto
    carregava OUTRO trade (stop 207,00 contra 176,83). O aviso está onde a decisão
    se toma (nos cards e NO gráfico), não só no carimbo do topo da página."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre(page, base, viewport=viewport)
        _troca_frame(page, "1h")
        m = page.evaluate("""() => {
          const el = document.getElementById('setupCards');
          const t = el.querySelector('.sc-explor');
          const card = el.querySelector('.setup-card');
          const cs = card ? getComputedStyle(card) : {};
          return {
            tf: _tf, veredito: _verdictTf,
            tarja: t ? t.innerText.replace(/\\s+/g, ' ').trim() : '',
            visivel: t ? t.getBoundingClientRect().height > 0 : false,
            classe: el.className,
            bordaCard: cs.borderTopStyle, bordaEsq: cs.borderLeftStyle,
            carimbo: document.getElementById('priceChart').dataset.tf || '',
            // os NÚMEROS do frame exploratório continuam inteiros na tela
            texto: el.innerText,
          };
        }""")
        assert m["tf"] == "1h" and m["veredito"] == "4h", m
        assert m["visivel"], ("a tarja tem de estar VISÍVEL, não só no DOM", m)
        assert "exploratório" in m["tarja"].lower(), m
        assert "não são o plano da decisão" in m["tarja"].lower(), m
        assert "4h" in m["tarja"], ("tem de dizer QUAL frame decidiu", m)
        assert "is-exploratorio" in m["classe"], m
        assert m["bordaCard"] == "dashed", ("o card muda de tratamento", m)
        assert m["bordaEsq"] == "solid", ("a cor da leitura é identidade e fica", m)
        assert "exploratório" in m["carimbo"].lower(), ("e no GRÁFICO também", m)
        # NADA some pra caber no aviso: os níveis do 1h continuam legíveis
        assert "207,00" in m["texto"] and "210,53" in m["texto"], m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_voltar_pro_frame_do_veredito_tira_a_tarja(base):
    """Ida e volta: o estado acompanha o frame, não fica grudado no primeiro clique."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        _troca_frame(page, "1h")
        assert page.evaluate("() => !!document.querySelector('#setupCards .sc-explor')")
        _troca_frame(page, "4h")
        m = page.evaluate("""() => ({
          tarja: document.querySelectorAll('#setupCards .sc-explor').length,
          classe: document.getElementById('setupCards').className,
          carimbo: document.getElementById('priceChart').dataset.tf || '',
        })""")
        assert m["tarja"] == 0 and "is-exploratorio" not in m["classe"], m
        assert "exploratório" not in m["carimbo"].lower(), m
        browser.close()


# ────────────────────── 2. a cor segue o NÚMERO, sempre ───────────────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_rr_abaixo_de_1_nunca_sai_em_verde(base):
    """DENTE (print do diário): "R:R 0,21:1" em verde, dentro da faixa verde do
    alvo. 0,21 é arriscar ~4,8x o que se pretende ganhar — verde afirmava o
    contrário do número, e verde é "pode ir" na tela inteira."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        _troca_frame(page, "1d")
        m = page.evaluate("""() => {
          const cv = document.getElementById('priceChart');
          return {texto: cv.dataset.rr || '', cor: cv.dataset.rrCor || '',
                  card: document.getElementById('setupCards').innerText};
        }""")
        assert "0,21" in m["texto"], m
        assert not _verde(m["cor"]), ("R:R < 1 saiu em verde", m)
        # e a CONTA vem junto, no chip e no card — o número sozinho não diz o tamanho.
        # Em pt-BR: o resto da tela escreve "0,21:1" e "218,40", e o multiplicador
        # saía "4.8x", duas convenções de decimal na mesma frase.
        assert "4,8x" in m["texto"], m
        assert "risco > retorno" in m["card"], m
        browser.close()



@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_rr_acima_de_1_continua_verde(base):
    """A outra metade da regra: verde não sumiu, ele ficou RESERVADO ao caso em que
    o retorno de fato supera o risco."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        bom = json.loads(json.dumps(_ACT_D))
        bom["risk_reward"] = {"entry": 200.0, "entry_basis": "gatilho", "risk": 10.0,
                              "reward": 25.0, "rr": 2.5, "note": None}
        _abre(page, base, bom)
        m = page.evaluate("""() => {
          const cv = document.getElementById('priceChart');
          return {texto: cv.dataset.rr || '', cor: cv.dataset.rrCor || '',
                  card: document.getElementById('setupCards').innerText};
        }""")
        assert "2,50" in m["texto"], m
        assert _verde(m["cor"]), ("R:R >= 1 tem de ser verde", m)
        assert "risco > retorno" not in m["card"], ("R:R bom não vira alarme", m)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_zona_nao_ativa_agora_sai_do_verde_de_pode_ir(base):
    """DENTE: "recuo à média (MMS20) — não ativa agora" pintado de VERDE, com o
    próprio texto dizendo que não está ativa. Tracejado e opacidade não bastavam:
    são acabamento DENTRO da mesma cor, e a cor é o que se lê primeiro."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        m = page.evaluate("""(planos) => {
          const z = (a) => planZones(a).map(x => ({tag: x.tag, cor: x.color,
                                                   inativa: !!x.inactive}));
          return {fora: z(planos.fora), dentro: z(planos.dentro)};
        }""", {"fora": _ACT_4H,
               "dentro": {**_ACT_4H, "buy_zone": {**_ACT_4H["buy_zone"],
                                                  "active_now": True}}})
        inativa = next(x for x in m["fora"] if "não ativa agora" in x["tag"])
        ativa = next(x for x in m["dentro"] if x["tag"].startswith("recuo à média"))
        assert inativa["inativa"] is True, m
        assert not _verde(inativa["cor"]), ("faixa inativa continua verde", inativa)
        assert _verde(ativa["cor"]), ("a faixa ATIVA é que é verde", ativa)
        assert inativa["cor"] != ativa["cor"], m
        browser.close()


# ─────────────── 3. R:R em todo frame, ou ausente COM o motivo ────────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("tf,esperado", [("4h", "não calculável"), ("1h", "não calculável"),
                                         ("1d", "0,21:1")])
def test_o_rr_aparece_em_TODO_frame(base, tf, esperado):
    """DENTE: o R:R só apareceu no diário. No 1h e no 4h não havia número **nem
    palavra** — indistinguível de um frame sem setup nenhum. Agora, sem número, a
    linha diz que não é calculável e POR QUÊ."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        if tf != "4h":
            _troca_frame(page, tf)
        txt = page.inner_text("#setupCards")
        assert "risco/retorno" in txt, (tf, txt)
        assert esperado in txt, (tf, txt)
        if esperado == "não calculável":
            assert "sem alvo estrutural à frente da entrada" in txt, (
                "ausência sem motivo é o mesmo buraco de antes", tf, txt)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_no_telefone_o_motivo_do_rr_tambem_chega(base):
    """A tela onde o defeito foi visto. O motivo é frase, e frase que não quebra
    some na borda de 390px."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=TELEFONE)
        _abre(page, base, viewport=TELEFONE)
        m = page.evaluate("""() => {
          const el = document.getElementById('setupCards');
          const corta = [...el.querySelectorAll('*')]
            .filter(e => e.scrollWidth > e.clientWidth + 1)
            .map(e => (e.className || '') + ' :: ' + (e.innerText || '').slice(0, 40));
          return {texto: el.innerText, cortados: corta,
                  rola: document.documentElement.scrollWidth >
                        document.documentElement.clientWidth};
        }""")
        assert "não calculável" in m["texto"], m["texto"]
        assert "sem alvo estrutural" in m["texto"], m["texto"]
        assert m["cortados"] == [], ("motivo cortado na borda do telefone", m["cortados"])
        assert not m["rola"], "a página não rola de lado no telefone"
        browser.close()


# ───────────────────── 4. o carimbo da análise diz o que é ────────────────────
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_horario_da_analise_vem_rotulado(base):
    """DENTE: "28/08" no diário, "19:30" no 1h, "17:30" no 4h, sem rótulo nenhum —
    faz sentido (é o último candle de cada frame) e parecia dado inconsistente."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        ler = """() => {
          const u = document.querySelector('#headPrice .hp-ref');
          return {texto: u ? u.innerText.replace(/\\s+/g, ' ').trim() : '',
                  tag: u ? (u.querySelector('.hp-tag') || {}).innerText || '' : '',
                  when: u ? (u.querySelector('.hp-when') || {}).innerText || '' : ''};
        }"""
        no4h = page.evaluate(ler)
        assert "último candle" in no4h["tag"].lower(), no4h
        # o rótulo é caixa alta por CSS — o que se compara é o texto, não a caixa
        assert "4h" in no4h["tag"].lower(), ("o rótulo diz de QUAL frame é o candle", no4h)
        assert "17:30" in no4h["when"], no4h
        _troca_frame(page, "1h")
        no1h = page.evaluate(ler)
        assert "1h" in no1h["tag"].lower(), no1h
        assert "19:30" in no1h["when"], ("a hora acompanha o frame", no1h)
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_sem_regressao_no_desktop_nada_corta_nem_rola_de_lado(base):
    """DA-062: a correção de um print não pode quebrar a tela larga. Vale no frame
    do veredito e no exploratório, que é onde entrou tarja nova."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        for tf in ("4h", "1h"):
            if tf != "4h":
                _troca_frame(page, tf)
            m = page.evaluate("""() => {
              const el = document.getElementById('setupCards');
              return {cortados: [...el.querySelectorAll('*')]
                        .filter(e => e.scrollWidth > e.clientWidth + 1)
                        .map(e => (e.className || '').toString().slice(0, 40)),
                      rola: document.documentElement.scrollWidth >
                            document.documentElement.clientWidth};
            }""")
            assert m["cortados"] == [], (tf, m)
            assert not m["rola"], tf
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("largura", [1500, 1280])
def test_o_carimbo_do_frame_nao_cai_embaixo_da_dica_de_zoom(base, largura):
    """DENTE nascido desta própria task: o carimbo cresceu ("Diário · exploratório")
    e passou a colidir com a dica de zoom, que é uma frase longa numa linha só
    ancorada à direita. Texto por cima de texto — os dois ilegíveis, e um deles é
    dado. A dica cede: quebra em duas linhas na metade direita."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": largura, "height": 1100})
        _abre(page, base)
        _troca_frame(page, "1d")   # o carimbo mais largo: "Diário · exploratório"
        m = page.evaluate("""() => {
          const cv = document.getElementById('priceChart');
          const dica = document.querySelector('.chart-zoom-hint');
          const rc = cv.getBoundingClientRect(), rd = dica.getBoundingClientRect();
          return {carimboFim: Number(cv.dataset.carimboFim || 0),
                  dicaComeca: Math.round(rd.left - rc.left),
                  texto: cv.dataset.tf || '', visivel: rd.height > 0};
        }""")
        assert "exploratório" in m["texto"].lower(), m
        assert m["visivel"], ("a dica não sumiu — ela cedeu ESPAÇO, não existência", m)
        assert m["dicaComeca"] > m["carimboFim"], ("dica de zoom em cima do carimbo", m)


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
@pytest.mark.parametrize("viewport", [DESKTOP, TELEFONE], ids=["desktop", "telefone"])
def test_o_chip_de_rr_cabe_no_grafico_em_vez_de_atravessar_a_regua(base, viewport):
    """DENTE também desta task: com a conta escrita junto, "R:R 0,21:1 · risco 4,8x o
    retorno" atravessava a régua de preço no telefone (plot útil ~250px). Cortar
    perderia letra e diminuir a fonte deixaria ilegível — o texto DEGRADA por medida
    pra forma mais curta que couber, e o número nunca sai."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=viewport)
        _abre(page, base, viewport=viewport)
        _troca_frame(page, "1d")
        m = page.evaluate("""() => {
          const cv = document.getElementById('priceChart');
          const ctx = cv.getContext('2d');
          ctx.font = 'bold 11px ui-monospace, Menlo, monospace';
          const t = cv.dataset.rr || '';
          // mesma geometria do desenho: PAD_L/PAD_R são as constantes do módulo
          return {texto: t, largura: Math.round(ctx.measureText(t).width + 14),
                  plot: Math.round(cv.clientWidth - PAD_L - PAD_R)};
        }""")
        assert "0,21:1" in m["texto"], ("o NÚMERO nunca sai", m)
        assert m["largura"] <= m["plot"], ("o chip atravessa a régua de preço", m)
        if m["plot"] > 400:      # no desktop cabe a frase inteira
            assert "risco 4,8x o retorno" in m["texto"], m
        browser.close()


@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_na_comparacao_ninguem_e_exploratorio(base):
    """Onde a regra NÃO vale, e por quê: no confronto são duas runs, cada uma com o
    SEU veredito no SEU frame. `_verdictTf` ali guarda o da coluna A — carimbar a B
    de "exploratória" afirmaria que ela não decidiu nada, o oposto do que ela é."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=DESKTOP)
        _abre(page, base)
        m = page.evaluate("""() => {
          const antes = ehExploratorio('1h');          // fora do confronto: é sim
          const salvo = _openView;
          _openView = 'compare';
          const durante = ehExploratorio('1h');        // no confronto: nunca
          _openView = salvo;
          return {antes, durante, depois: ehExploratorio('1h')};
        }""")
        assert m["antes"] is True and m["depois"] is True, m
        assert m["durante"] is False, ("o confronto não carimba coluna de exploratória", m)
        browser.close()

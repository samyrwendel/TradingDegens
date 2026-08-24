"""Correlação entre ativos + FORÇA RELATIVA, dos candles diários já cacheados.

Método do Erick (análise 4, 24/08): ele mapeia 30+ ativos por correlação com um
ÂNCORA (a NVDA, pro setor de IA) pra saber quem sofre junto num evento (o
resultado da NVDA) e quem vira refúgio. O conceito que ele extrai daí é a FORÇA
RELATIVA — "o que não cai quando o líder cai é o que eu acumulo".

Isto é cálculo puro sobre o OHLCV diário que a ferramenta JÁ tem em cache: é a
correlação de Pearson dos log-retornos numa janela (30/60/90 dias), NÃO API paga.
:func:`load_ohlcv` já vem cacheado por símbolo (DA-058) e cortado a
``<= curr_date``, então nada aqui enxerga o futuro. Sem candle, declara
indisponível — nunca inventa um número.

Ver ``~/brain/trading-ops/04-nvda-correlacao.md``.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .stockstats_utils import load_ohlcv

logger = logging.getLogger(__name__)

# Âncora padrão por tipo de ativo. Ação: NVDA (líder do ciclo de IA, referência do
# Erick). Cripto: BTC-USD (líder do setor) — NVDA não faz sentido pra cripto.
DEFAULT_STOCK_ANCHOR = "NVDA"
DEFAULT_CRYPTO_ANCHOR = "BTC-USD"

# Janelas (em pregões) da correlação de retornos.
DEFAULT_WINDOWS = (30, 60, 90)
# Mínimo de retornos alinhados pra reportar uma janela — abaixo disso a estimativa
# não tem base e a janela é marcada "histórico insuficiente".
_MIN_OBS = 15

# Recorte de queda do âncora pra força relativa.
_RS_LOOKBACK = 60           # pregões olhados pra achar a maior queda do âncora
_RS_MIN_DRAWDOWN = 0.02     # queda mínima do âncora (2%) pra o recorte "valer"
_RS_TIE = 0.005             # empate técnico: |dif| <= 0,5 p.p. = "caiu junto"


def default_anchor(asset_type: str) -> str:
    return DEFAULT_CRYPTO_ANCHOR if asset_type == "crypto" else DEFAULT_STOCK_ANCHOR


# ---------------------------------------------------------------- bandas Erick --
# Bandas do próprio Erick (nota 04). Correlação ALTA = anda junto = sofre junto
# num evento (risco de contágio 🔴); BAIXA = protegido do evento (refúgio 🟢).
def classify_correlation(r: float) -> tuple[str, str]:
    """(rótulo, emoji-de-estado) pela banda do Erick. r em [-1, 1]."""
    if r >= 0.70:
        return "alta", "🔴"
    if r >= 0.50:
        return "moderada-alta", "🟠"
    if r >= 0.30:
        return "moderada", "🟡"
    return "baixa", "🟢"


# ----------------------------------------------------------------- séries ------
def _closes_by_date(symbol: str, curr_date: str) -> pd.Series:
    """Série de fechamentos indexada por Date (já cortada a <= curr_date)."""
    df = load_ohlcv(symbol, curr_date)
    if df is None or df.empty or "Close" not in df.columns:
        raise ValueError(f"sem candle para {symbol}")
    d = df.copy()
    d["Date"] = pd.to_datetime(d["Date"], errors="coerce")
    d = d.dropna(subset=["Date"]).drop_duplicates(subset=["Date"], keep="last")
    s = d.set_index("Date")["Close"].astype(float).sort_index()
    return s


def _aligned_returns(sym: pd.Series, anc: pd.Series) -> pd.DataFrame:
    """Log-retornos diários alinhados por data (interseção). Colunas s, a."""
    joined = pd.concat([sym.rename("s"), anc.rename("a")], axis=1, join="inner")
    joined = joined.dropna()
    rets = np.log(joined / joined.shift(1)).dropna()
    return rets


# -------------------------------------------------------------- correlação -----
def compute_correlation(
    symbol: str,
    curr_date: str,
    anchor: str | None = None,
    asset_type: str = "stock",
    windows=DEFAULT_WINDOWS,
) -> dict:
    """Correlação de Pearson dos log-retornos de ``symbol`` vs ``anchor``.

    Retorna ``{"anchor", "is_anchor", "windows": {30: {...}}, "available": bool}``.
    Cada janela: ``{"r", "n", "label", "emoji"}`` ou ``{"insufficient": True, "n"}``.
    Não lança em símbolo fino — reporta o que o histórico permite; propaga só a
    falha dura de dados (símbolo inexistente) via :func:`load_ohlcv`.
    """
    anchor = (anchor or default_anchor(asset_type)).upper()
    symbol_u = symbol.upper()
    if symbol_u == anchor:
        return {"anchor": anchor, "is_anchor": True, "windows": {}, "available": True}

    sym = _closes_by_date(symbol, curr_date)
    anc = _closes_by_date(anchor, curr_date)
    rets = _aligned_returns(sym, anc)

    out: dict[int, dict] = {}
    total = len(rets)
    for w in windows:
        tail = rets.tail(w)
        n = len(tail)
        if n < _MIN_OBS:
            out[w] = {"insufficient": True, "n": n}
            continue
        r = tail["s"].corr(tail["a"])
        if r is None or pd.isna(r):
            out[w] = {"insufficient": True, "n": n}
            continue
        label, emoji = classify_correlation(float(r))
        out[w] = {"r": float(r), "n": n, "label": label, "emoji": emoji}

    return {
        "anchor": anchor,
        "is_anchor": False,
        "windows": out,
        "available": total >= _MIN_OBS,
        "overlap": total,
    }


# --------------------------------------------------------- força relativa ------
def compute_relative_strength(
    symbol: str,
    curr_date: str,
    anchor: str | None = None,
    asset_type: str = "stock",
    lookback: int = _RS_LOOKBACK,
    min_drawdown: float = _RS_MIN_DRAWDOWN,
) -> dict:
    """Força relativa: no maior recorte de QUEDA do âncora, quem caiu menos.

    Acha a maior queda pico→vale do âncora nos últimos ``lookback`` pregões
    alinhados e compara o retorno de ``symbol`` no MESMO intervalo de datas.
    Retorna ``{"has_window", ...}``; ``has_window=False`` quando o âncora não teve
    queda relevante na janela (nada a avaliar, nada inventado).
    """
    anchor = (anchor or default_anchor(asset_type)).upper()
    symbol_u = symbol.upper()
    if symbol_u == anchor:
        return {"has_window": False, "is_anchor": True, "anchor": anchor}

    sym = _closes_by_date(symbol, curr_date)
    anc = _closes_by_date(anchor, curr_date)
    joined = pd.concat([sym.rename("s"), anc.rename("a")], axis=1, join="inner").dropna()
    joined = joined.tail(lookback)
    if len(joined) < 5:
        return {"has_window": False, "anchor": anchor, "reason": "histórico insuficiente"}

    a = joined["a"]
    running_max = a.cummax()
    drawdown = a / running_max - 1.0
    trough_date = drawdown.idxmin()                       # vale (queda mais funda)
    peak_date = a.loc[:trough_date].idxmax()              # pico antes do vale
    anc_ret = float(a.loc[trough_date] / a.loc[peak_date] - 1.0)

    if peak_date >= trough_date or anc_ret > -min_drawdown:
        return {
            "has_window": False,
            "anchor": anchor,
            "reason": "sem queda relevante do âncora na janela",
            "lookback": lookback,
        }

    sym_ret = float(joined["s"].loc[trough_date] / joined["s"].loc[peak_date] - 1.0)
    diff = sym_ret - anc_ret  # >0 = caiu menos / subiu = força relativa

    if sym_ret >= 0:
        verdict = "refúgio"
        emoji = "🟢"
    elif diff > _RS_TIE:
        verdict = "caiu menos — força relativa"
        emoji = "🟢"
    elif diff < -_RS_TIE:
        verdict = "caiu mais — sem proteção"
        emoji = "🔴"
    else:
        verdict = "caiu junto"
        emoji = "🟡"

    return {
        "has_window": True,
        "anchor": anchor,
        "peak_date": peak_date.date().isoformat(),
        "trough_date": trough_date.date().isoformat(),
        "anchor_ret": anc_ret,
        "symbol_ret": sym_ret,
        "diff": diff,
        "verdict": verdict,
        "emoji": emoji,
    }


# --------------------------------------------------------------- seção ---------
def _fmt_pct(x: float) -> str:
    return f"{x * 100:+.1f}%".replace(".", ",")


def _fmt_corr(r: float) -> str:
    return f"{r:.2f}".replace(".", ",")


def build_correlation_section(
    symbol: str,
    curr_date: str,
    asset_type: str = "stock",
    anchor: str | None = None,
) -> str:
    """Seção markdown pt-BR: correlação com o âncora + força relativa.

    Fail-soft de dados: se o candle do símbolo ou do âncora não existe, declara
    indisponível — nunca inventa correlação.
    """
    anchor_name = (anchor or default_anchor(asset_type)).upper()
    head = f"## 🔗 Correlação com o âncora ({anchor_name}) + força relativa"

    try:
        corr = compute_correlation(symbol, curr_date, anchor_name, asset_type)
    except Exception as exc:  # noqa: BLE001 — enriquecimento nunca quebra o relatório
        logger.warning("correlation section failed for %s vs %s: %s", symbol, anchor_name, exc)
        return (
            f"{head}\n\n"
            f"Correlação indisponível — sem candle suficiente para {symbol} ou "
            f"{anchor_name} nesta data. Nada inventado."
        )

    if corr.get("is_anchor"):
        return (
            f"{head}\n\n"
            f"**{symbol.upper()} é o próprio âncora do setor.** A correlação de um "
            f"ativo consigo mesmo é trivial (1,00); a leitura de contágio/refúgio se "
            f"faz nos OUTROS ativos contra {anchor_name}."
        )

    lines = [head, ""]

    wins = corr.get("windows", {})
    any_win = any("r" in v for v in wins.values())
    if not any_win:
        lines.append(
            f"Sem histórico alinhado suficiente entre {symbol.upper()} e {anchor_name} "
            f"para estimar a correlação — nada inventado."
        )
    else:
        lines.append(
            "_Correlação de Pearson dos log-retornos diários (janela declarada). "
            "Alta = anda junto, sofre junto num evento; baixa = protegido do evento._"
        )
        lines.append("")
        for w in DEFAULT_WINDOWS:
            v = wins.get(w)
            if not v:
                continue
            if v.get("insufficient"):
                lines.append(f"- **{w}d**: histórico insuficiente ({v['n']} retornos).")
                continue
            lines.append(
                f"- **{w}d**: {v['emoji']} {_fmt_corr(v['r'])} "
                f"(correlação {v['label']}, n={v['n']})"
            )

    # Força relativa — o insumo do "refúgio" do Erick.
    try:
        rs = compute_relative_strength(symbol, curr_date, anchor_name, asset_type)
    except Exception as exc:  # noqa: BLE001
        logger.warning("relative-strength failed for %s vs %s: %s", symbol, anchor_name, exc)
        rs = None

    lines.append("")
    if not rs or not rs.get("has_window"):
        reason = (rs or {}).get("reason", "sem recorte de queda do âncora na janela")
        lines.append(f"**Força relativa:** {reason} — não avaliada agora.")
    else:
        lines.append(
            f"**Força relativa** (recorte de queda do âncora, "
            f"{rs['peak_date']} → {rs['trough_date']}): {rs['emoji']} "
            f"{anchor_name} {_fmt_pct(rs['anchor_ret'])} × {symbol.upper()} "
            f"{_fmt_pct(rs['symbol_ret'])} — {rs['verdict']}."
        )

    return "\n".join(lines)

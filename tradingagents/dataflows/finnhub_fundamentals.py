"""Fundamentals básicos do Finnhub — a FONTE DE CONFERÊNCIA (não a âncora).

O `fundamentals_anchors` é determinístico e date-guarded, mas single-sourced no
yfinance: se a tabela trimestral muda de label ou cai, a âncora morre em silêncio.
Este módulo dá a segunda opinião — FCF TTM, shares e cotação — por endpoints que
não dependem do yfinance.

**Conferência, nunca âncora.** Os endpoints free do Finnhub são AO VIVO: o plano
grátis não tem candle/financeiro histórico (``/stock/candle`` e os
``/stock/financials`` são premium). O FCF TTM derivado aqui é o de HOJE, não o
da data da análise — exatamente o drift que o ``info.freeCashflow`` do yfinance
introduz (INTC 27/08: live 4,87 bi vs tabela date-guarded 2,83 bi). Por isso o
valor deste módulo **nunca substitui** a âncora; ele é comparado contra ela e a
divergência é RENDERIZADA (o leitor vê os dois números e decide com os olhos
abertos), no molde do `same_as_realize`/`overlap_note`.

Derivação do FCF TTM (3 endpoints):
    ``pfcfShareTTM`` = preço ÷ FCF-por-ação → FCF-por-ação = preço ÷ pfcf
    FCF total = FCF-por-ação × ``shareOutstanding`` (profile2, em milhões)

Sem ``FINNHUB_API_KEY`` → ``None`` ("indisponível", nunca inventa). Fail-open:
qualquer falha de endpoint devolve ``None`` — conferência ausente não derruba
a análise.
"""
from __future__ import annotations

import logging

from tradingagents.dataflows.finnhub_earnings import _finnhub_get, get_api_key

logger = logging.getLogger(__name__)


def get_quote(symbol: str) -> float | None:
    """Cotação corrente (``/quote`` → ``c``). Fail-open → ``None``."""
    try:
        q = _finnhub_get("quote", {"symbol": symbol.upper()})
        c = (q or {}).get("c") if isinstance(q, dict) else None
        return float(c) if isinstance(c, (int, float)) and c > 0 else None
    except Exception as exc:  # noqa: BLE001 — conferência ausente nunca derruba a run
        logger.info("finnhub quote indisponível para %s: %s", symbol, exc)
        return None


def get_shares(symbol: str) -> float | None:
    """Shares outstanding em unidades (``/stock/profile2`` traz em milhões)."""
    try:
        p = _finnhub_get("stock/profile2", {"symbol": symbol.upper()})
        sh = (p or {}).get("shareOutstanding") if isinstance(p, dict) else None
        # profile2 devolve MILHÕES — o resto do motor trabalha em unidades absolutas
        return float(sh) * 1_000_000 if isinstance(sh, (int, float)) and sh > 0 else None
    except Exception as exc:  # noqa: BLE001 — idem: fail-open
        logger.info("finnhub shares indisponível para %s: %s", symbol, exc)
        return None


def get_fcf_ttm(symbol: str) -> float | None:
    """FCF TTM CORRENTE (não date-guarded!) em unidades de moeda — ver docstring.

    ``pfcfShareTTM`` (preço ÷ FCF-por-ação TTM) + ``shareOutstanding``. Requer os
    três endpoints coerentes; qualquer buraco (métrica, cotação ou shares) → None.
    """
    if get_api_key() is None:
        return None
    try:
        m = _finnhub_get("stock/metric", {"symbol": symbol.upper(), "metric": "all"})
        metric = (m or {}).get("metric") if isinstance(m, dict) else None
        pfcf = (metric or {}).get("pfcfShareTTM")
        if not isinstance(pfcf, (int, float)) or pfcf <= 0:
            return None
        price = get_quote(symbol)
        shares = get_shares(symbol)
        if price is None or shares is None or price <= 0:
            return None
        fcf_per_share = price / float(pfcf)
        if fcf_per_share <= 0:
            return None
        return fcf_per_share * shares
    except Exception as exc:  # noqa: BLE001 — idem: fail-open
        logger.info("finnhub fcf ttm indisponível para %s: %s", symbol, exc)
        return None

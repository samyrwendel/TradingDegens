"""Cotação LIVE leve pra watchlist lateral — preço atual + variação do dia.

De propósito SEPARADO do pipeline de análise: nunca carrega a série OHLCV
date-guarded nem roda agente. Pergunta ao yfinance só os campos rápidos
(``fast_info``: last_price + previous_close) pra a lateral mostrar um número que
atualiza a cada ~40s. Qualquer falha vira ``None`` (a UI mostra "—"), nunca exceção.

Símbolos são normalizados pela mesma regra do resto (``normalize_symbol``): cripto
``BTC-USD``, forex ``EURUSD=X``, índices/metais via alias — ação passa direto.
"""

from __future__ import annotations

import logging
from typing import Any

from .symbol_utils import normalize_symbol

logger = logging.getLogger(__name__)


def _fast_get(fast_info: Any, *keys: str) -> Any:
    """Lê um campo do ``fast_info`` do yfinance (varia entre versões: às vezes
    mapping com ``.get``/``[]``, às vezes atributo). Tenta cada nome, fail-open."""
    for key in keys:
        try:
            if hasattr(fast_info, "get"):
                val = fast_info.get(key)
                if val is not None:
                    return val
        except Exception:  # noqa: BLE001
            pass
        val = getattr(fast_info, key, None)
        if val is not None:
            return val
    return None


def _to_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        f = float(value)
    except (TypeError, ValueError):
        return None
    # descarta NaN/inf
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def fetch_live_price(symbol: str) -> dict[str, Any] | None:
    """Preço atual de UM símbolo: ``{"price", "change_pct", "currency"}`` ou None.

    Usa ``yfinance.Ticker(sym).fast_info`` (sem baixar histórico, sem pipeline).
    ``change_pct`` é a variação do dia vs. o fechamento anterior (None quando o
    fechamento anterior é desconhecido). Fail-open: qualquer erro devolve None.
    """
    raw = (symbol or "").strip()
    if not raw:
        return None
    canonical = normalize_symbol(raw)
    try:
        import yfinance as yf

        fast_info = yf.Ticker(canonical).fast_info
        last = _to_float(_fast_get(fast_info, "last_price", "lastPrice"))
        if last is None:
            return None
        prev = _to_float(_fast_get(fast_info, "previous_close", "previousClose"))
        currency = _fast_get(fast_info, "currency")
        change_pct = None
        if prev not in (None, 0):
            change_pct = round((last - prev) / prev * 100, 2)
        return {
            "price": last,
            "change_pct": change_pct,
            "currency": currency if isinstance(currency, str) else None,
        }
    except Exception as exc:  # noqa: BLE001 — fonte instável nunca quebra a UI
        logger.debug("preço live falhou para %s (%s): %s", raw, canonical, exc)
        return None

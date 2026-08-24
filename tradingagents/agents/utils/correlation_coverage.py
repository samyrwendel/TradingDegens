"""Garante a seção de correlação + força relativa no relatório do analista.

Espelha os outros guardas de cobertura (multi-timeframe, price-structure): depois
que o analista escreve a prosa, anexa uma seção DETERMINÍSTICA de correlação com o
âncora (NVDA pro setor de IA) e de FORÇA RELATIVA — computada dos mesmos candles
cacheados e date-guarded, sempre presente, jamais um número inventado.

Anexada incondicionalmente: o LLM não calcula correlação de retornos sozinho, não
há o que detectar-e-pular. Fail-open: qualquer erro devolve o relatório intacto.
"""
from __future__ import annotations

import logging

from tradingagents.dataflows.correlation import build_correlation_section

logger = logging.getLogger(__name__)


def ensure_correlation_coverage(
    report: str, symbol: str, curr_date: str, asset_type: str = "stock"
) -> str:
    try:
        section = build_correlation_section(symbol, curr_date, asset_type)
    except Exception as exc:  # noqa: BLE001 — enriquecimento nunca quebra o relatório
        logger.warning("correlation coverage failed for %s: %s", symbol, exc)
        return report
    base = (report or "").rstrip()
    return f"{base}\n\n{section}\n" if base else section + "\n"

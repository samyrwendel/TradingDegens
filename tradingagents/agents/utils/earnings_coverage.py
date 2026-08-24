"""Garante a seção de calendário de earnings no relatório do analista.

Espelha os outros guardas de cobertura: anexa uma seção com a próxima data de
resultado do ativo (e do âncora NVDA — o eixo do evento na leitura do Erick),
vinda da fonte pública cacheada e date-guarded. Fonte caída → "indisponível",
nunca uma data inventada. Cripto não tem earnings: a seção vem vazia e não é
anexada. Fail-open.
"""
from __future__ import annotations

import logging

from tradingagents.dataflows.earnings_calendar import build_earnings_section

logger = logging.getLogger(__name__)


def ensure_earnings_coverage(
    report: str, symbol: str, curr_date: str, asset_type: str = "stock"
) -> str:
    try:
        section = build_earnings_section(symbol, curr_date, asset_type)
    except Exception as exc:  # noqa: BLE001 — enriquecimento nunca quebra o relatório
        logger.warning("earnings coverage failed for %s: %s", symbol, exc)
        return report
    if not section:  # cripto / sem conteúdo
        return report
    base = (report or "").rstrip()
    return f"{base}\n\n{section}\n" if base else section + "\n"

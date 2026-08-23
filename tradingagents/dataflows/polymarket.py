"""Polymarket prediction-market vendor.

Surfaces live, market-implied probabilities for forward-looking events (Fed
decisions, recession, elections, geopolitics, crypto) to the news analyst, as a
complement to news (what happened) and FRED macro data (where things stand):
what the crowd actually prices to happen next.

Uses Polymarket's public Gamma API (https://gamma-api.polymarket.com) — no key,
no auth. Each market's ``outcomePrices`` are the implied probabilities of its
outcomes (a "Yes" at 0.76 means the market prices a 76% chance).
"""
import json
import logging
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"

# Network timeout (seconds), consistent with the other vendors.
REQUEST_TIMEOUT = 30

# Default number of markets to return, ranked by traded volume.
DEFAULT_LIMIT = 6


def _request(path: str, params: dict) -> dict:
    response = requests.get(
        f"{GAMMA_BASE}/{path}", params=params, timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    return response.json()


def _parse_json_list(value) -> list:
    """Gamma encodes ``outcomes``/``outcomePrices`` as JSON-string arrays."""
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


def _is_forward_looking(market: dict, now: datetime) -> bool:
    """Keep only open markets that resolve in the future.

    ``closed`` is the reliable resolved flag (``active`` stays True even for
    settled markets), and a past ``endDate`` means the event already resolved —
    either way it is not a forward-looking signal.
    """
    if market.get("closed"):
        return False
    end_date = market.get("endDate")
    if end_date:
        try:
            if datetime.fromisoformat(end_date.replace("Z", "+00:00")) < now:
                return False
        except ValueError:
            pass
    return bool(_parse_json_list(market.get("outcomePrices"))) and bool(
        _parse_json_list(market.get("outcomes"))
    )


def get_prediction_markets(
    topic: str, limit: int | None = None, display: str | None = None
) -> str:
    """Return live prediction-market probabilities for an event topic.

    Args:
        topic: Event keyword(s) used to SEARCH Polymarket, e.g. "Fed rate cut",
            "recession 2026", "US election". Kept in English because Polymarket's
            markets are titled in English and the keyword search matches on them.
        limit: Max markets to return (ranked by traded volume); ``None`` uses
            DEFAULT_LIMIT.
        display: Optional pt-BR label shown in the report in place of the raw
            English search query, so the output carries no stray English topic
            string. Defaults to ``topic`` when not given.

    Returns:
        A markdown report of the most-traded open markets matching the topic,
        each with its implied probability, traded volume, resolution date, and
        recent (1-week) move.
    """
    if limit is None:
        limit = DEFAULT_LIMIT
    shown = display or topic

    try:
        data = _request("public-search", {"q": topic, "limit_per_type": 20})
    except requests.RequestException as e:
        logger.warning("Polymarket search failed for %r: %s", topic, e)
        return (
            f"O acesso à Polymarket está indisponível no momento (erro de rede: "
            f"{e}). Prossiga sem o sinal de mercado de previsão para '{shown}'."
        )

    now = datetime.now(timezone.utc)
    candidates = [
        m
        for event in data.get("events", [])
        for m in event.get("markets", [])
        if _is_forward_looking(m, now)
    ]
    candidates.sort(key=lambda m: m.get("volumeNum") or 0, reverse=True)

    header = (
        f'## Mercados de previsão Polymarket: "{shown}"\n'
        f"Probabilidades implícitas ao vivo (maior volume negociado = mais "
        f"profundo e confiável). A probabilidade é a odd precificada pela "
        f"multidão para o evento, não uma previsão a tomar como certa.\n\n"
    )

    if not candidates:
        return header + (
            f"Nenhum mercado de previsão aberto casou com '{shown}'. A cobertura "
            f"da Polymarket se concentra em eventos macro, políticos, "
            f"geopolíticos e cripto; um ativo específico pode não ter nenhum."
        )

    lines = []
    for m in candidates[:limit]:
        prices = _parse_json_list(m.get("outcomePrices"))
        outcomes = _parse_json_list(m.get("outcomes"))
        try:
            prob = float(prices[0])
        except (ValueError, IndexError):
            continue
        label = outcomes[0] if outcomes else "Yes"
        label = {"Yes": "Sim", "No": "Não"}.get(label, label)
        volume = m.get("volumeNum") or 0
        end_date = (m.get("endDate") or "")[:10]
        wk = m.get("oneWeekPriceChange")
        wk_str = (
            f", 1 semana {wk * 100:+.1f}pp"
            if isinstance(wk, (int, float)) and wk
            else ""
        )
        lines.append(
            f"- **{m.get('question')}** — {label} {prob:.0%} "
            f"(volume ${volume:,.0f}, resolve em {end_date}{wk_str})"
        )

    return header + "\n".join(lines) + "\n"

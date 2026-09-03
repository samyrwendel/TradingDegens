"""Cotação LIVE leve — preço atual, variação do dia e QUAL preço é esse.

De propósito SEPARADO do pipeline de análise: nunca carrega a série OHLCV
date-guarded nem roda agente. Qualquer falha vira ``None`` (a UI mostra "—"),
nunca exceção.

**A sessão faz parte do dado, não é enfeite.** Fechamento, pré-market e
after-market são preços DIFERENTES, e mostrar qualquer um deles como "agora" é
mentira barata: com o mercado fechado, o número que a tela chamava de atual era o
último fechamento (MSFT 29/08: 513,53 de fechamento contra 513,06 no after). Por
isso a cotação vem do ``info`` do yfinance — que traz ``marketState`` e os preços
de pré/pós além do regular — e não do ``fast_info``, que só tem o regular e não diz
que é o regular. Custo medido: ~0,8s por símbolo, o mesmo do ``fast_info``.

Símbolos são normalizados pela mesma regra do resto (``normalize_symbol``): cripto
``BTC-USD``, forex ``EURUSD=X``, índices/metais via alias — ação passa direto.
"""

from __future__ import annotations

import logging
from typing import Any

from .symbol_utils import normalize_symbol

logger = logging.getLogger(__name__)


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


# Como o ``marketState`` do yfinance vira sessão + rótulo humano. A sessão é o QUE
# o número é; o rótulo é como se diz isso na tela, em pt-BR, sem eufemismo.
_SESSOES = {
    "REGULAR": ("regular", "cotação agora · mercado aberto"),
    "PRE": ("pre", "pré-market"),
    "PREPRE": ("pre", "pré-market"),
    "POST": ("pos", "after-market"),
    "POSTPOST": ("pos", "after-market"),
    "CLOSED": ("fechado", "último fechamento"),
}
_SESSAO_DESCONHECIDA = ("desconhecida", "último preço conhecido")


def _sessao_e_preco(info: dict) -> tuple[str, str, float | None, float | None]:
    """``(sessao, rotulo, preco_a_exibir, preco_regular)`` a partir do ``info``.

    Cripto não tem pregão: é sempre "agora". Numa ação, pré e pós SÓ são exibidos
    quando a fonte devolve o preço daquela sessão — se ela diz "PRE" mas não manda
    ``preMarketPrice``, o que existe é o fechamento anterior, e é isso que se diz.
    """
    estado = str(info.get("marketState") or "").upper()
    regular = _to_float(info.get("regularMarketPrice"))
    if str(info.get("quoteType") or "").upper() == "CRYPTOCURRENCY":
        return "24h", "cotação agora · 24h", regular, regular
    sessao, rotulo = _SESSOES.get(estado, _SESSAO_DESCONHECIDA)
    if sessao == "pre":
        pre = _to_float(info.get("preMarketPrice"))
        if pre is not None:
            return sessao, rotulo, pre, regular
        return "fechado", "último fechamento (pré-market sem negócio ainda)", regular, regular
    if sessao == "pos":
        pos = _to_float(info.get("postMarketPrice"))
        if pos is not None:
            return sessao, rotulo, pos, regular
        return "fechado", "último fechamento (after-market sem negócio ainda)", regular, regular
    return sessao, rotulo, regular, regular


def _instante_manaus(epoch) -> str | None:
    """Epoch (segundos UTC) → instante Manaus **offset-aware** (ISO, ex.:
    ``2026-09-03T15:54:00-04:00``).

    O EIXO da tela é UM só: Manaus (DA-205). A cotação chegava carimbada no fuso da
    BOLSA (NY/UTC) e o front a mostrava crua, ao lado de uma análise carimbada em
    OUTRO fuso — dois relógios no mesmo cabeçalho. Aqui a HORA é sempre Manaus (o
    relógio do usuário); a PROCEDÊNCIA (fuso da bolsa) segue à parte, no campo
    ``fuso``, para quem quiser saber de onde o número veio."""
    if not epoch:
        return None
    try:
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo

        dt = datetime.fromtimestamp(float(epoch), tz=timezone.utc)
        return dt.astimezone(ZoneInfo("America/Manaus")).isoformat(timespec="minutes")
    except Exception:  # noqa: BLE001 — hora ausente nunca derruba a cotação
        return None


def fetch_live_price(symbol: str) -> dict[str, Any] | None:
    """Cotação de UM símbolo, com a SESSÃO declarada. ``None`` em qualquer falha.

    Devolve ``{"price", "change_pct", "currency", "sessao", "rotulo", "as_of",
    "regular_price", "fuso"}``:

    * ``price`` — o número a exibir: o da sessão CORRENTE (pré/pós quando há negócio
      nelas, senão o regular);
    * ``sessao`` ∈ ``{regular, pre, pos, fechado, 24h, desconhecida}`` e ``rotulo``,
      a frase que a tela mostra — juntos são a resposta a "que preço é esse?";
    * ``regular_price`` — o fechamento/último regular, pra a tela poder mostrar os
      dois quando divergem (é a divergência que o leitor precisa ver);
    * ``as_of`` — o instante daquele número, em Manaus e offset-aware (o eixo único
      da tela, DA-205); ``fuso`` guarda à parte a bolsa de PROCEDÊNCIA.

    ``change_pct`` é a variação vs. o fechamento anterior (None quando ausente).
    """
    raw = (symbol or "").strip()
    if not raw:
        return None
    canonical = normalize_symbol(raw)
    try:
        import yfinance as yf

        info = yf.Ticker(canonical).info or {}
        sessao, rotulo, preco, regular = _sessao_e_preco(info)
        if preco is None:
            return None
        prev = _to_float(info.get("regularMarketPreviousClose")) or _to_float(
            info.get("previousClose"))
        currency = info.get("currency")
        tz_nome = info.get("exchangeTimezoneName")
        stamp = {"pre": "preMarketTime", "pos": "postMarketTime"}.get(sessao, "regularMarketTime")
        change_pct = None
        if prev not in (None, 0):
            change_pct = round((preco - prev) / prev * 100, 2)
        return {
            "price": preco,
            "change_pct": change_pct,
            "currency": currency if isinstance(currency, str) else None,
            "sessao": sessao,
            "rotulo": rotulo,
            "as_of": _instante_manaus(info.get(stamp) or info.get("regularMarketTime")),
            "regular_price": regular,
            "fuso": tz_nome if isinstance(tz_nome, str) else None,
        }
    except Exception as exc:  # noqa: BLE001 — fonte instável nunca quebra a UI
        logger.debug("preço live falhou para %s (%s): %s", raw, canonical, exc)
        return None

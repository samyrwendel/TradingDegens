"""Calendário de earnings — próxima data de resultado (fonte pública, keyless).

O eixo da análise do Erick é o EVENTO: "o resultado da NVDA sai quarta 26/08".
Sem saber quando é o evento, não dá pra posicionar antes ("evita aumentar antes
do balanço" — regra dele).

Fonte: ``yfinance`` (``Ticker.get_earnings_dates``), pública e sem chave. Se a
fonte cair/instável, declara INDISPONÍVEL — nunca inventa uma data. Passa pelo
cache (DA-058) e respeita o date_guard: a "próxima" data é a primeira que NÃO é
anterior à data de análise — ou seja, inclui o BALANÇO DO PRÓPRIO DIA. O filtro
antigo (``ts <= base``) descartava justamente o evento do dia corrente, que é o
de MAIOR risco: no dia 27/08 o MRVL divulgava às 16h e a seção imprimia
"indisponível", deixando a regra "não aumentar posição antes do balanço" muda na
única hora em que ela importa. Continua sem look-ahead: a data é agendada e
pública ANTES do evento, e aqui se lê apenas a DATA — nunca o resultado.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from tradingagents.datacache import cache

logger = logging.getLogger(__name__)

_CATEGORY = "earnings_next"

# VERSÃO DA SEMÂNTICA dentro da chave do cache. O fix do L1 mudou o SIGNIFICADO da
# resposta — o balanço do PRÓPRIO dia passou a contar como "próximo" — e a chave
# antiga não sabia disso. Como entrada de data passada é gravada PERMANENTE
# (``permanent = base < hoje``), toda (símbolo, data) já consultada antes do fix
# continuaria devolvendo, pra sempre, a resposta que escondia o balanço do dia —
# em backtest e em reanálise de data passada. Bump aqui = as antigas viram órfãs e
# nunca mais são servidas. Mudou a semântica da resposta? Bump.
_SEMANTICA_KEY = "v2-proprio-dia-conta"
# Quantas linhas puxar do yfinance (cobre alguns trimestres à frente e atrás).
_LIMIT = 16
# Hora (fuso do papel) a partir da qual o release é "após o fechamento".
_AFTER_CLOSE_HOUR = 16


def _to_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (ValueError, TypeError):
        return None


def _fetch_next_earnings(symbol: str, base: date) -> dict | None:
    """Consulta o yfinance e devolve o próximo earnings > ``base``. None se nada."""
    import yfinance as yf

    tk = yf.Ticker(symbol)
    df = tk.get_earnings_dates(limit=_LIMIT)
    if df is None or getattr(df, "empty", True):
        return None

    best = None  # (date, row)
    for idx, row in df.iterrows():
        ts = _to_date(idx)
        if ts is None or ts < base:      # inclui o balanço do PRÓPRIO dia (>= base)
            continue
        if best is None or ts < best[0]:
            best = (ts, idx, row)

    if best is None:
        return None

    d, ts, row = best
    hour = getattr(ts, "hour", None)
    after_close = hour is not None and hour >= _AFTER_CLOSE_HOUR
    est = row.get("EPS Estimate") if hasattr(row, "get") else None
    try:
        est = float(est)
        if est != est:  # NaN
            est = None
    except (TypeError, ValueError):
        est = None

    return {
        "symbol": symbol.upper(),
        "date": d.isoformat(),
        "after_close": bool(after_close),
        "eps_estimate": est,
        # É HOJE? O dia do balanço é o de risco máximo — quem lê precisa ver isso
        # em destaque, não deduzir comparando duas datas.
        "is_today": d == base,
        "days_ahead": (d - base).days,
    }


# Por que a agenda não veio. As duas causas exigem leitura OPOSTA e não podem
# aparecer com a mesma frase: "sem agenda" é informação (a empresa não tem data
# publicada — não há risco de evento conhecido); "fonte fora do ar" é ignorância
# (pode haver balanço amanhã e não sabemos).
STATUS_OK = "ok"
STATUS_SEM_AGENDA = "sem_agenda"
STATUS_FONTE_INDISPONIVEL = "fonte_indisponivel"


def get_next_earnings_status(symbol: str, curr_date: str) -> tuple[dict | None, str]:
    """``(evento, status)`` — o próximo resultado e POR QUE ele falta, se faltar.

    O evento inclui o balanço do PRÓPRIO dia de análise (``is_today``). O status é
    um de :data:`STATUS_OK` / :data:`STATUS_SEM_AGENDA` / :data:`STATUS_FONTE_INDISPONIVEL`,
    para que a seção nunca junte "a empresa não tem data marcada" com "a fonte
    caiu" na mesma frase. Cacheado (DA-058) e date-guarded.
    """
    # Import tardio do guard: date_guard vive na camada de agents e importá-lo no
    # topo criaria um ciclo (agents.__init__ -> erick_analyst -> earnings_coverage
    # -> este módulo). No momento da CHAMADA a camada de agents já está carregada.
    from tradingagents.agents.utils.date_guard import clamp

    guarded = clamp(curr_date)
    base = _to_date(guarded)
    if base is None:
        base = datetime.now().date()

    k = cache.key(_CATEGORY, _SEMANTICA_KEY, symbol.upper(), base.isoformat())
    hit = cache.get(_CATEGORY, k)
    if hit is not None:
        neg = hit.get("kind") == "neg"
        cache.record_hit(_CATEGORY, negative=neg)
        value = hit.get("value")
        if value is not None:
            return value, STATUS_OK
        # o negativo guarda o ERRO quando foi a fonte que caiu; sem erro, foi
        # resposta boa e vazia = a empresa não tem data publicada
        return None, (STATUS_FONTE_INDISPONIVEL if hit.get("error") else STATUS_SEM_AGENDA)

    cache.record_net(_CATEGORY)
    try:
        result = _fetch_next_earnings(symbol, base)
    except Exception as exc:  # noqa: BLE001 — fonte instável degrada a "indisponível"
        logger.warning("earnings source failed for %s: %s", symbol, exc)
        cache.set_neg(_CATEGORY, k, value=None, error={"type": type(exc).__name__, "msg": str(exc)})
        return None, STATUS_FONTE_INDISPONIVEL

    if result is None:
        # Sem próximo evento conhecido: negativo de TTL curto (a agenda pode surgir).
        cache.set_neg(_CATEGORY, k, value=None)
        return None, STATUS_SEM_AGENDA

    # Data passada de análise -> a "próxima data a partir dali" é fato histórico
    # estável (permanente); análise ao vivo expira no fim do dia.
    permanent = base < datetime.now().date()
    cache.set_ok(_CATEGORY, k, result, permanent)
    return result, STATUS_OK


def get_next_earnings(symbol: str, curr_date: str) -> dict | None:
    """Próxima data de resultado de ``symbol`` a partir de ``curr_date`` (inclusive).

    Atalho de :func:`get_next_earnings_status` para quem só quer o evento; ``None``
    quando não há data conhecida — o motivo vem da função com status."""
    return get_next_earnings_status(symbol, curr_date)[0]


def earnings_window_status(
    symbol: str, curr_date: str, window_days: int, asset_type: str = "stock",
) -> dict:
    """Leitura tri-state PRONTA PRA TELA (não markdown): data, dias até lá, e se
    cai dentro de ``window_days`` — reusa o cache de :func:`get_next_earnings_status`
    (DA-058), nenhum fetch novo por chamada.

    Cripto não tem calendário de resultados (mesma regra de
    :func:`build_earnings_section`): ``status`` volta ``None`` sem consultar nada.

    ``in_window`` segue a MESMA disciplina tri-state do resto do módulo:
    ``True``/``False`` quando dá pra saber, ``None`` quando a fonte caiu ou a data
    não tem dias calculáveis — nunca ``False`` por ignorância (ignorância não é
    "sem risco").
    """
    if asset_type == "crypto":
        return {"status": None, "date": None, "days_ahead": None,
                "in_window": None, "window_days": window_days}

    ev, status = get_next_earnings_status(symbol, curr_date)
    out: dict = {"status": status, "date": None, "days_ahead": None,
                 "in_window": None, "window_days": window_days}
    if status == STATUS_SEM_AGENDA:
        out["in_window"] = False
        return out
    if status != STATUS_OK or not isinstance(ev, dict):
        return out
    out["date"] = str(ev.get("date") or "")[:10]
    if ev.get("is_today"):
        out["days_ahead"] = 0
        out["in_window"] = True
        return out
    dias = ev.get("days_ahead")
    out["days_ahead"] = dias
    # ``dias`` ausente é o mesmo caso raro do ``_days_ahead`` do erick_method (data
    # presente sem dias calculáveis): fica ``None`` — não vira "fora da janela".
    out["in_window"] = (dias <= window_days) if isinstance(dias, int) else None
    return out


def _reported_earnings(symbol: str, curr_date: str) -> dict | None:
    """Resultado reportado do âncora (Finnhub), fail-open → None. Seam p/ monkeypatch."""
    try:
        from .finnhub_earnings import get_reported_earnings

        return get_reported_earnings(symbol, curr_date)
    except Exception as exc:  # noqa: BLE001 — enriquecimento nunca quebra a seção
        logger.info("reported earnings unavailable for %s: %s", symbol, exc)
        return None


def _fmt_event(ev: dict) -> str:
    when = " (após o fechamento)" if ev.get("after_close") else ""
    est = ""
    if ev.get("eps_estimate") is not None:
        est = f", EPS estimado {ev['eps_estimate']:.2f}".replace(".", ",")
    return f"{ev['date']}{when}{est}"


def _event_line(name: str, tag: str, ev: dict | None, status: str) -> str:
    if ev is None:
        if status == STATUS_SEM_AGENDA:
            return (f"- **{name}**{tag}: **sem data de resultado publicada** para o "
                    f"período — a fonte respondeu e não há balanço agendado à frente "
                    f"(não é falha de fonte).")
        return (f"- **{name}**{tag}: agenda de resultados **indisponível** — a fonte "
                f"pública não respondeu. Não sabemos se há balanço à frente; nenhuma "
                f"data inventada.")
    if ev.get("is_today"):
        # O dia do balanço é o de risco MÁXIMO — e era exatamente o que sumia antes.
        janela = ("ainda hoje, após o fechamento" if ev.get("after_close")
                  else "hoje, antes/na abertura")
        return (f"- **{name}**{tag}: 🚨 **RESULTADO HOJE** ({_fmt_event(ev)}) — "
                f"{janela}. Risco de evento no máximo: pelo método, não aumentar "
                f"posição antes do balanço.")
    dias = ev.get("days_ahead")
    prox = f" (em {dias} dia{'s' if dias != 1 else ''})" if isinstance(dias, int) else ""
    return f"- **{name}**{tag}: próximo resultado em {_fmt_event(ev)}{prox}."


def build_earnings_section(
    symbol: str,
    curr_date: str,
    asset_type: str = "stock",
    anchor: str | None = None,
) -> str:
    """Seção markdown pt-BR: próximo earnings do ativo (e do âncora NVDA).

    Cripto não tem calendário de resultados — retorna vazio (o chamador não anexa).
    Fonte caída → "indisponível", nunca inventa data.
    """
    if asset_type == "crypto":
        return ""

    head = "## 📅 Calendário de earnings (risco de evento)"
    lines = [head, ""]

    ev, status = get_next_earnings_status(symbol, curr_date)
    lines.append(_event_line(symbol.upper(), "", ev, status))

    # O âncora (NVDA) é o eixo do evento na leitura do Erick — mostra sempre, a não
    # ser que o próprio ativo já seja o âncora.
    from .correlation import default_anchor

    anchor_name = (anchor or default_anchor(asset_type)).upper()
    if symbol.upper() != anchor_name:
        ev_a, status_a = get_next_earnings_status(anchor_name, curr_date)
        lines.append(_event_line(anchor_name, " (âncora)", ev_a, status_a))

    # RESULTADO já reportado do âncora (o CATALISADOR da leitura do Erick) — reportado
    # × estimado + surpresa, via Finnhub. É o dado que define "bateu → liquidação de
    # longs" vs "decepcionou → fraqueza". Ausente (sem chave/fonte) = honesto, sem
    # inventar. Mostra o do próprio ativo quando ele é o âncora.
    reported_symbol = symbol.upper() if symbol.upper() == anchor_name else anchor_name
    reported_tag = "" if symbol.upper() == anchor_name else " (âncora)"
    rep = _reported_earnings(reported_symbol, curr_date)
    if rep is not None:
        from .finnhub_earnings import format_reported_line

        lines.append(f"- **{reported_symbol}**{reported_tag}: {format_reported_line(rep)}.")

    lines.append("")
    lines.append(
        "_Regra do método: evitar aumentar posição antes do balanço; o resultado do "
        "âncora arrasta os correlacionados._"
    )
    return "\n".join(lines)

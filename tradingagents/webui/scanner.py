"""Scanner estrutural 1-2-3 — o olho barato ($0 de LLM) antes da análise cara.

O método decide por ESTRUTURA (1-2-3 + médias), não por sentimento — e a
estrutura já vem computada do :func:`build_actionable_plan_dict`
(determinístico, cacheado DA-058, zero LLM). Este módulo só ENUMERA: varre a
watchlist em 1d+4h+1h (em paralelo) e classifica cada ativo pela distância do
preço ao GATILHO,
pra o Samyr decidir com um clique se vale a análise completa (Padrão/Erick).

Estados (vocabulário único, reutilizado no painel):
* ``em_gatilho``   — preço a ≤ _GATILHO_TOL do gatilho (ponto de entrada AGORA).
                     No painel vira COMPRA (verde) ou VENDA (vermelho) pela direção.
* ``em_movimento`` — padrão acionado e preço além da entrada (no move buscando alvo;
                     o gatilho ficou p/ trás — NÃO é ponto de entrada).
* ``invalidou``    — preço além do ponto 3: a premissa estrutural morreu, não entra.
* ``formando``     — padrão existe, ainda não rompeu (vigiar — distância mostrada).
* ``sem_setup``    — sem padrão detectado (não é erro)
* ``sem_dado``     — fonte degradou; NUNCA se inventa setup (honestidade padrão)

Track record (a observação do Samyr virando número): todo ``em_gatilho`` é
logado append-only em ``scans.jsonl`` com os níveis; ``scan_verdicts``
re-avalia cada log contra o preço de HOJE — bateu TP / bateu SL / andamento —
e devolve a taxa de acerto agregada. Custa $0 porque só lê série cacheada.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tradingagents.dataflows.price_structure import (
    build_actionable_plan_dict,
    build_price_chart,
)

logger = logging.getLogger(__name__)

# Frames do scan: o swing (1d), o posicionamento (4h) e o fino (1h).
SCAN_FRAMES = ("1d", "4h", "1h")

# Distância do preço ao gatilho que caracteriza "em gatilho" (ponto de entrada).
# PROVISÓRIO e declarado: 0,5% absorve o ruído intradiário de um toque iminente.
# A calibrar com o track record do scans.jsonl (mesma disciplina do
# _EARNINGS_WINDOW_NOTE do erick_method).
_GATILHO_TOL = 0.005

# Paralelismo do scan. Limite ESCOLHIDO POR MEDIÇÃO, não por gosto (20 ativos ×
# 3 frames, N=4 por config, 29/08 — mediana):
#
#   workers   cotação fria   cotação quente
#      1          19,9s           5,2s
#      3           9,2s           7,5s
#      4           8,1s           6,5s   ← melhor nos dois, e o mais ESTÁVEL
#      5          10,2s           7,1s
#      6           8,8s           7,0s
#     10           9,4s           6,6s
#
# Acima de 4 não melhora e a variância sobe — é onde o throttle do provedor começa
# a cobrar (o ``yf_retry`` existe por isso), e throttle acumulado é a explicação
# mais provável pro outlier de 75s que a revisão mediu. Abaixo de 4, a rede manda.
# Nota honesta: com TUDO em cache o trabalho vira CPU (detecção de estrutura em
# pandas) e o GIL faz o paralelo custar ~1,3s a mais que o serial — o ganho está no
# caso frio e, sobretudo, no TETO: um ativo lento não segura mais os outros 19.
_SCAN_WORKERS = 4

# TTL do preço LIVE por símbolo. Medido: ``_live_price`` custava ~0,9s POR ativo e
# sozinho respondia por ~18s dos 24,6s de um scan quente — refetch de 20 cotações a
# cada varredura, inclusive quando o usuário só reclicou "Escanear". 30s fica ABAIXO
# do refresh da lateral (~40s), então nenhuma tela fica mais velha do que já ficava.
# Vale também pro NEGATIVO: símbolo que a fonte não resolve custa um timeout por
# janela, não um por scan.
_LIVE_TTL_S = 30.0
_live_cache: dict[str, tuple[float, float | None]] = {}
_live_lock = threading.Lock()

# Ordem de urgência pro sort (menor = mais urgente). Entradas vivas primeiro;
# em_movimento (já passou) e invalidou (morreu) ficam após os opportunities vivos.
_URGENCIA = {"em_gatilho": 0, "formando": 1, "em_movimento": 2, "invalidou": 3, "sem_setup": 4, "sem_dado": 5}


def _fmt_pct(v: float | None) -> str:
    return f"{v * 100:.2f}%" if v is not None else "—"


def _live_price(ticker: str, ttl: float = _LIVE_TTL_S) -> float | None:
    """Preço atual (live) de um símbolo — uma chamada rápida ao fast_info do
    yfinance, fail-open, com cache de janela curta (:data:`_LIVE_TTL_S`).

    O scan de gatilhos mede a distância do PREÇO ATUAL ao gatilho, não do last
    close da série date-guarded (que pode ser de ontem ou estar stale no cache
    diário). Sem live → cai no price do plan (honesto).

    O cache guarda TAMBÉM o negativo: um símbolo que a fonte não resolve paga um
    timeout por janela, não um por varredura.
    """
    agora = time.time()
    with _live_lock:
        hit = _live_cache.get(ticker)
        if hit is not None and agora - hit[0] < ttl:
            return hit[1]
    try:
        from tradingagents.dataflows.live_price import fetch_live_price
        data = fetch_live_price(ticker)
        preco = float(data["price"]) if data and data.get("price") else None
    except Exception:  # noqa: BLE001 — preço live nunca derruba o scan
        preco = None
    with _live_lock:
        _live_cache[ticker] = (agora, preco)
    return preco


def _live_cache_clear() -> None:
    """Zera o cache de cotação (teste, e o "escanear de novo" explícito)."""
    with _live_lock:
        _live_cache.clear()


def _chart_fallback(ticker: str, date: str, frame: str) -> dict:
    """Chart só pro último recurso de preço (ver :func:`_frame_row`). Fail-open."""
    try:
        return build_price_chart(ticker, date, timeframe=frame) or {}
    except Exception as exc:  # noqa: BLE001 — fallback nunca derruba o scan
        logger.info("chart de fallback falhou para %s %s: %s", ticker, frame, exc)
        return {}


def _frame_row(ticker: str, date: str, frame: str,
               live_price: float | None = None) -> dict[str, Any]:
    """Uma linha do scan: plano do frame, classificada."""
    try:
        plan = build_actionable_plan_dict(ticker, date, timeframe=frame)
    except Exception as exc:  # noqa: BLE001 — scan nunca cai por um símbolo
        logger.info("scan fetch falhou para %s %s: %s", ticker, frame, exc)
        return {"frame": frame, "estado": "sem_dado", "motivo": str(exc)}

    plan = plan or {}
    pat = plan.get("pattern") or {}
    # Preço ATUAL tem prioridade: o gatilho é onde se entra AGORA, então a
    # distância é medida do live. Sem live (fonte instável/fora do ar), usa o
    # last close do plan (date-guarded) — declarado, nunca inventado.
    #
    # O chart é o ÚLTIMO recurso e por isso vem PREGUIÇOSO: ele reroda a detecção
    # de estrutura inteira, e buscá-lo sempre dobrava esse trabalho por (ticker,
    # frame) — 3 frames × a watchlist toda — pra um fallback que quase nunca entra.
    price = live_price if live_price is not None else plan.get("price")
    if price is None:
        price = _last_close(_chart_fallback(ticker, date, frame))
    if not pat or pat.get("trigger") is None or price is None:
        setup = plan.get("setup_state")
        if setup in ("sem_dado", "intradiario_indisponivel"):
            return {"frame": frame, "estado": "sem_dado", "motivo": f"fonte: {setup}",
                    "price": price}
        return {"frame": frame, "estado": "sem_setup", "price": price}

    trigger = float(pat["trigger"])
    dist = abs(price / trigger - 1.0) if trigger else None
    state = pat.get("state")
    direction = pat.get("direction")
    # Invalidação: preço além do ponto 3 (onde o padrão deixa de existir). Na
    # compra o setup morre ao PERDER o ponto 3; na venda ao VOLTAR acima dele.
    # (antes o scan não tinha esse estado: mostrava um setup morto como vivo.)
    inval_price = (plan.get("invalidation") or {}).get("price")
    invalidated = False
    if inval_price is not None:
        inval = float(inval_price)
        invalidated = (price > inval) if direction == "venda" else (price < inval)
    # EM GATILHO = preço no ponto de entrada AGORA (≤ tol), independente de o
    # padrão já ter acionado (recém-rompido ainda no ponto ainda entra). Acionado
    # e preço além da entrada → em_movimento (buscando alvo, não é entrada).
    if invalidated:
        estado = "invalidou"
    elif dist is not None and dist <= _GATILHO_TOL:
        estado = "em_gatilho"
    elif state == "acionado":
        estado = "em_movimento"
    else:
        estado = "formando"

    stop = plan.get("stop") or {}
    target = plan.get("target") or {}
    rr = plan.get("risk_reward") or {}
    return {
        "frame": frame,
        "estado": estado,
        "direction": direction,
        "pattern_state": state,
        "trigger": trigger,
        "price": price,
        "dist_pct": dist,
        "dist_txt": _fmt_pct(dist),
        "invalidacao": (plan.get("invalidation") or {}).get("price"),
        "sl": stop.get("price"),
        "tp": target.get("price"),
        "tp_faixa": ([target.get("low"), target.get("high")]
                     if target.get("low") is not None else None),
        "rr": rr.get("rr"),
        "rr_note": rr.get("note"),
    }


def _last_close(chart: dict) -> float | None:
    candles = (chart or {}).get("candles") or []
    return float(candles[-1]["c"]) if candles and candles[-1].get("c") else None


def scan_symbol(ticker: str, date: str, frames: tuple = SCAN_FRAMES) -> dict[str, Any]:
    """O scan de UM ativo nos frames pedidos (fail-open por frame).

    O preço LIVE é buscado UMA vez por símbolo (não por frame) e compartilhado
    entre os frames — a cotação atual é a mesma independente do timeframe, e
    ``fast_info`` é a chamada leve que não carrega série.
    """
    ticker = (ticker or "").strip().upper()
    live = _live_price(ticker)
    rows = [_frame_row(ticker, date, tf, live_price=live) for tf in frames]
    # "Melhor" só ordena a lista (urgência) — não escolhe nem esconde frame.
    # Cada ativo reporta TODOS os frames com seu 1-2-3; a UI mostra os dois lado
    # a lado (1d, 4h e 1h), sem hierarquia entre eles.
    best = min(rows, key=lambda r: (_URGENCIA.get(r.get("estado"), 9),
                                    r.get("dist_pct") if r.get("dist_pct") is not None else 9.9))
    return {"ticker": ticker, "frames": rows, "melhor": best}


def scan_watchlist(tickers: list[str], date: str,
                   frames: tuple = SCAN_FRAMES,
                   workers: int = _SCAN_WORKERS) -> dict[str, Any]:
    """Varre a watchlist toda — ordenada por urgência (em_gatilho primeiro).

    **Em paralelo** (:data:`_SCAN_WORKERS`): é I/O-bound, e o serial fazia 20
    ativos × 3 frames esperarem um de cada vez.

    **Números REAIS** (20 ativos — a watchlist de verdade —, frames 1d+4h+1h,
    medido 29/08, mediana de N=4). O comentário antigo prometia "10 ativos, ~13s
    frio / ~2s cacheado"; a watchlist tem 20 e nenhum dos dois tempos existia:

    ====================  ========  =====================
    cenário               antes     agora (4 workers)
    ====================  ========  =====================
    cotação fria           19,9s     **8,1s**
    cotação quente          5,2s       6,5s
    ====================  ========  =====================

    Sobre a cotação quente ficar 1,3s mais lenta: com tudo em cache o trabalho é
    CPU (pandas sob o GIL), não I/O, e aí thread não ajuda. Vale a troca porque o
    caso que doía era o outro — a revisão mediu 25s a 75s pela HTTP, com a cotação
    fria em TODA chamada (não havia cache de cotação; :func:`_live_price`).

    ``ex.map`` preserva a ordem de entrada; a ordenação por urgência vem depois,
    então o resultado é determinístico apesar do paralelismo.
    """
    if not tickers:
        return {"date": date, "frames": list(frames), "resumo": {}, "ativos": []}
    n = max(1, min(workers, len(tickers)))
    with ThreadPoolExecutor(max_workers=n, thread_name_prefix="scan") as ex:
        out = list(ex.map(lambda t: scan_symbol(t, date, frames), tickers))
    out.sort(key=lambda s: (_URGENCIA.get(s["melhor"].get("estado"), 9),
                            s["melhor"].get("dist_pct") if s["melhor"].get("dist_pct") is not None else 9.9))
    counts: dict[str, int] = {}
    for s in out:
        counts[s["melhor"]["estado"]] = counts.get(s["melhor"]["estado"], 0) + 1
    return {"date": date, "frames": list(frames), "resumo": counts, "ativos": out}


# ------------------------------------------------------ track record do scan ----
class ScanLog:
    """Append-only ``scans.jsonl`` — cada ``em_gatilho`` flagrado, com níveis.

    O teste empírico da observação "1-2-3 dá lucro em alguns dias": sem este
    log, a percepção não vira número. Com ele, ``scan_verdicts`` mede.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, row: dict[str, Any]) -> None:
        """Loga UM gatilho (chamado só quando estado == em_gatilho)."""
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 **{k: row.get(k) for k in ("ticker", "frame", "direction",
                                            "pattern_state", "trigger", "sl", "tp", "rr")}}
        with self._lock, open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self._lock, open(self.path, encoding="utf-8") as fh:
            lines = fh.readlines()
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out


def _dia(v: Any) -> str:
    """Parte-DATA (``YYYY-MM-DD``) de um ts do log ou do rótulo de um candle."""
    return str(v or "")[:10]


def _primeiro_toque(candles: list[dict], desde_dia: str, tp, sl, venda: bool) -> dict | None:
    """O PRIMEIRO toque em TP ou SL na série, varrida em ordem cronológica.

    É isto que torna o track record IMUTÁVEL: um trade que tocou o alvo e voltou
    fica ``bateu_tp`` pra sempre, porque o toque é um fato numa barra do passado —
    não uma comparação com o preço de agora, que muda todo dia. Janela crescendo
    (``date`` avança) nunca desfaz um toque anterior: o primeiro achado manda.

    Janela: barras de dias ESTRITAMENTE POSTERIORES ao dia do log. O log carrega
    hora UTC e o candle carrega o relógio do mercado — sem base comum, contar o
    próprio dia do log poderia creditar um TP que aconteceu ANTES do gatilho ser
    flagrado. Um acerto inflado é o pior erro possível num painel que existe pra
    dizer a taxa de acerto real, então o mesmo-dia fica de fora, declarado.

    TP e SL na MESMA barra: sem tick não dá pra saber a ordem dentro da barra →
    conta ``bateu_sl`` (a leitura pessimista). Também declarado, nunca chutado.
    """
    if tp is None and sl is None:
        return None
    for c in candles:
        dia = _dia(c.get("d"))
        if not dia or dia <= desde_dia:
            continue
        hi, lo = c.get("h"), c.get("l")
        if hi is None or lo is None:
            continue
        bateu_tp = tp is not None and (lo <= tp if venda else hi >= tp)
        bateu_sl = sl is not None and (hi >= sl if venda else lo <= sl)
        if bateu_sl:               # empate na barra resolve pelo SL (pessimista)
            return {"veredito": "bateu_sl", "fechado_em": dia,
                    "empate_na_barra": bool(bateu_tp)}
        if bateu_tp:
            return {"veredito": "bateu_tp", "fechado_em": dia, "empate_na_barra": False}
    return None


def scan_verdicts(log: ScanLog, date: str) -> dict[str, Any]:
    """Re-avalia cada gatilho logado — FECHADO pela série, ABERTO pelo preço de hoje.

    Fechado (``bateu_tp``/``bateu_sl``): decidido pelo primeiro toque em TP ou SL
    nas barras posteriores ao log (:func:`_primeiro_toque`). Uma vez fechado, não
    muda mais — era o defeito antigo, que comparava só o preço de AGORA com os
    níveis e por isso (a) perdia o trade que tocou o alvo e voltou, (b) despromovia
    um ``bateu_tp`` de ontem pra ``andamento`` hoje, e (c) fazia a taxa de acerto
    oscilar a cada chamada.

    Aberto (``andamento_*``): esse sim é marcado a mercado — posição viva vale o
    preço de agora. Só leitura de série cacheada, $0.
    """
    verdicts = []
    for e in log.entries():
        ticker = str(e.get("ticker") or "")
        frame = str(e.get("frame") or "1d")
        trigger, tp, sl = e.get("trigger"), e.get("tp"), e.get("sl")
        try:
            plan = build_actionable_plan_dict(ticker, date, timeframe=frame)
        except Exception:  # noqa: BLE001 — verdict ausente não derruba o resto
            plan = {}
        try:
            candles = (build_price_chart(ticker, date, timeframe=frame) or {}).get("candles") or []
        except Exception:  # noqa: BLE001 — sem série o trade fica ABERTO, não fechado no escuro
            candles = []
        # Preço ATUAL tem prioridade sobre o last close do plan (mesmo motivo do
        # scan: a posição ABERTA é marcada a onde o preço está AGORA).
        live = _live_price(ticker)
        price = live if live is not None else (plan or {}).get("price")
        v = dict(e)
        v["preco_agora"] = price
        venda = e.get("direction") == "venda"

        toque = _primeiro_toque(candles, _dia(e.get("ts")), tp, sl, venda)
        if toque:
            v.update(toque)
            v["fechado"] = True
        elif price is None or trigger is None:
            v["veredito"] = "sem_dado"
            v["fechado"] = False
        else:
            v["fechado"] = False
            if price > trigger:
                v["veredito"] = "andamento_prejuizo" if venda else "andamento_lucro"
            else:
                v["veredito"] = "andamento_lucro" if venda else "andamento_prejuizo"
        verdicts.append(v)

    n = [v for v in verdicts if v.get("veredito") in ("bateu_tp", "bateu_sl")]
    acerto = (sum(1 for v in n if v["veredito"] == "bateu_tp") / len(n)) if n else None
    return {"verdicts": verdicts, "n_fechados": len(n),
            "taxa_acerto": acerto}

"""Scanner estrutural 1-2-3 — o olho barato ($0 de LLM) antes da análise cara.

O método decide por ESTRUTURA (1-2-3 + médias), não por sentimento — e a
estrutura já vem computada do :func:`build_actionable_plan_dict`
(determinístico, cacheado DA-058, zero LLM). Este módulo só ENUMERA: varre a
watchlist em 1d+4h e classifica cada ativo pela distância do preço ao GATILHO,
pra o Samyr decidir com um clique se vale a análise completa (Padrão/Erick).

Estados (vocabulário único, reutilizado no painel):
* ``em_gatilho`` — preço a ≤ _GATILHO_TOL do gatilho OU padrão ``acionado``
* ``perto``      — distância ≤ _PERTO_TOL (vigiar)
* ``formando``   — padrão existe, ainda longe
* ``sem_setup``  — sem padrão detectado (não é erro)
* ``sem_dado``   — fonte degradou; NUNCA se inventa setup (honestidade padrão)

Track record (a observação do Samyr virando número): todo ``em_gatilho`` é
logado append-only em ``scans.jsonl`` com os níveis; ``scan_verdicts``
re-avalia cada log contra o preço de HOJE — bateu TP / bateu SL / andamento —
e devolve a taxa de acerto agregada. Custa $0 porque só lê série cacheada.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tradingagents.dataflows.price_structure import (
    build_actionable_plan_dict,
    build_price_chart,
)

logger = logging.getLogger(__name__)

# Frames do scan (decisão do Samyr 28/08): o swing (1d) e o posicionamento (4h).
SCAN_FRAMES = ("1d", "4h")

# Distância do preço ao gatilho que caracteriza "em gatilho" / "perto".
# PROVISÓRIO e declarado: 0,5% absorve o ruído intradiário de um toque
# iminente; 3% é a janela de vigília. A calibrar com o track record do
# scans.jsonl (mesma disciplina do _EARNINGS_WINDOW_NOTE do erick_method).
_GATILHO_TOL = 0.005
_PERTO_TOL = 0.03

# Ordem de urgência pro sort (menor = mais urgente).
_URGENCIA = {"em_gatilho": 0, "perto": 1, "formando": 2, "sem_setup": 3, "sem_dado": 4}


def _fmt_pct(v: float | None) -> str:
    return f"{v * 100:.2f}%" if v is not None else "—"


def _frame_row(ticker: str, date: str, frame: str) -> dict[str, Any]:
    """Uma linha do scan: plano + chart do frame, classificada."""
    try:
        plan = build_actionable_plan_dict(ticker, date, timeframe=frame)
        chart = build_price_chart(ticker, date, timeframe=frame)
    except Exception as exc:  # noqa: BLE001 — scan nunca cai por um símbolo
        logger.info("scan fetch falhou para %s %s: %s", ticker, frame, exc)
        return {"frame": frame, "estado": "sem_dado", "motivo": str(exc)}

    plan = plan or {}
    pat = plan.get("pattern") or {}
    price = plan.get("price") or _last_close(chart)
    if not pat or pat.get("trigger") is None or price is None:
        setup = plan.get("setup_state")
        if setup in ("sem_dado", "intradiario_indisponivel"):
            return {"frame": frame, "estado": "sem_dado", "motivo": f"fonte: {setup}",
                    "price": price}
        return {"frame": frame, "estado": "sem_setup", "price": price}

    trigger = float(pat["trigger"])
    dist = abs(price / trigger - 1.0) if trigger else None
    state = pat.get("state")
    if state == "acionado" or (dist is not None and dist <= _GATILHO_TOL):
        estado = "em_gatilho"
    elif dist is not None and dist <= _PERTO_TOL:
        estado = "perto"
    else:
        estado = "formando"

    stop = plan.get("stop") or {}
    target = plan.get("target") or {}
    rr = plan.get("risk_reward") or {}
    return {
        "frame": frame,
        "estado": estado,
        "direction": pat.get("direction"),
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
    """O scan de UM ativo nos frames pedidos (fail-open por frame)."""
    ticker = (ticker or "").strip().upper()
    rows = [_frame_row(ticker, date, tf) for tf in frames]
    best = min(rows, key=lambda r: (_URGENCIA.get(r.get("estado"), 9),
                                    r.get("dist_pct") if r.get("dist_pct") is not None else 9.9))
    return {"ticker": ticker, "frames": rows, "melhor": best}


def scan_watchlist(tickers: list[str], date: str,
                   frames: tuple = SCAN_FRAMES) -> dict[str, Any]:
    """Varre a watchlist toda — ordenada por urgência (em_gatilho primeiro)."""
    out = [scan_symbol(t, date, frames) for t in tickers]
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


def scan_verdicts(log: ScanLog, tickers: list[str], date: str) -> dict[str, Any]:
    """Re-avalia cada gatilho logado contra o preço de HOJE (mesma data do scan).

    ``bateu_tp`` / ``bateu_sl`` / ``andamento`` por entrada + taxa de acerto
    agregada — só leitura de série cacheada, $0. Um mesmo gatilho logado há
    dias é o trade que a observação do Samyr diz valer; aqui ele conta.
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
        price = (plan or {}).get("price")
        v = dict(e)
        v["preco_agora"] = price
        venda = e.get("direction") == "venda"
        if price is None or trigger is None:
            v["veredito"] = "sem_dado"
        elif tp is not None and (price <= tp if venda else price >= tp):
            v["veredito"] = "bateu_tp"
        elif sl is not None and (price >= sl if venda else price <= sl):
            v["veredito"] = "bateu_sl"
        elif price > trigger:
            v["veredito"] = "andamento_prejuizo" if venda else "andamento_lucro"
        else:
            v["veredito"] = "andamento_lucro" if venda else "andamento_prejuizo"
        verdicts.append(v)

    n = [v for v in verdicts if v.get("veredito") in ("bateu_tp", "bateu_sl")]
    acerto = (sum(1 for v in n if v["veredito"] == "bateu_tp") / len(n)) if n else None
    return {"verdicts": verdicts, "n_fechados": len(n),
            "taxa_acerto": acerto}

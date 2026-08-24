"""Padrão × Erick comparison + meta-judge (Fase 3 do Modo Erick).

Runs the two readings — the Padrão pipeline and the same pipeline with the Erick
method analyst — and confronts them: a meta-judge names where they AGREE and where
they DIVERGE, because the divergence is the signal ("Padrão vê a tese de fundo,
Erick vê que o timing intradiário ainda não chegou").

The meta-judge here is DETERMINISTIC on purpose: it is anchored in the two real
verdicts + timeframes + the Erick setup state (never invents), keeps the run cost
at exactly two pipelines (no third LLM call), and is fully testable. The narrative
is derived from the actual results, not a vague summary.
"""

from __future__ import annotations

# 5-tier rating -> pt-BR practical meaning (mirror of the frontend VERDICT_PT).
_VERDICT_PT = {
    "buy": "COMPRAR", "overweight": "AUMENTAR", "hold": "MANTER",
    "underweight": "REDUZIR", "sell": "VENDER",
}
# Conservatism order (sell most cautious → buy most aggressive); the meta verdict
# on a divergence is the more conservative side — a split is not a green light.
_ORDER = {"sell": 0, "underweight": 1, "hold": 2, "overweight": 3, "buy": 4}
_TF_PT = {"1w": "semanal", "1d": "diário", "4h": "4h", "1h": "1h", "15m": "15m"}


def _vkey(v: str) -> str:
    return "".join(c for c in (v or "").lower() if c.isalpha())


def _vpt(v: str) -> str:
    return _VERDICT_PT.get(_vkey(v), (v or "—").upper())


def _tf(t: str) -> str:
    return _TF_PT.get((t or "1d"), t or "1d")


def _more_conservative(vp: str, ve: str) -> str:
    op, oe = _ORDER.get(_vkey(vp)), _ORDER.get(_vkey(ve))
    if op is None:
        return ve
    if oe is None:
        return vp
    return vp if op <= oe else ve


def build_column(record: dict, method: str) -> dict:
    """A comparison column from a persisted run record (or a snapshot dict).

    Carries the verdict, its timeframe, the operable plan (trader plan / final
    decision, plus the Erick method read on the Erick side), the cost, and the
    run_id so the UI can open the full analysis. ``_reused`` marks a column served
    from a cached prior run (no re-run).
    """
    r = record.get("result") or {}
    return {
        "method": method,
        "run_id": record.get("run_id"),
        "verdict": r.get("verdict") or record.get("verdict"),
        "timeframe": (
            r.get("verdict_timeframe") or record.get("verdict_timeframe")
            or r.get("timeframe") or "1d"
        ),
        "status": record.get("status"),
        "error": record.get("error"),
        "final_decision": r.get("final_trade_decision", "") or "",
        "trader_plan": r.get("trader_plan", "") or "",
        "erick_report": r.get("erick_report", "") or "",
        "actionable": r.get("actionable") or {},
        "cost": record.get("cost") or {"usd": record.get("cost_usd", 0) or 0},
        "elapsed": record.get("elapsed"),
        "degraded": r.get("degraded") or [],
        "reused": bool(record.get("_reused")),
    }


def deterministic_meta(padrao: dict, erick: dict) -> dict:
    """Confront the two columns: agree/diverge + the decision meaning, anchored in
    the real verdicts, timeframes and Erick setup state."""
    vp, ve = padrao.get("verdict"), erick.get("verdict")
    tfp, tfe = padrao.get("timeframe", "1d"), erick.get("timeframe", "1d")
    have_both = bool(_vkey(vp)) and bool(_vkey(ve))
    agree = have_both and _vkey(vp) == _vkey(ve)
    meta_verdict = vp if agree else _more_conservative(vp, ve)

    # Erick's timing read, if the method exposed a setup state
    setup = (erick.get("actionable") or {}).get("setup_state") or ""
    setup_pt = {
        "ativo": "o gatilho de recuo à média já está ativo",
        "aguardar_pullback": "ainda aguarda o recuo à média",
        "aguardar_rompimento": "ainda aguarda o rompimento",
        "sem_setup": "não vê setup de preço definido",
        "sem_dado": "não teve dado suficiente pro timing",
    }.get(setup, "")

    if not have_both:
        headline = "Comparação parcial — uma das leituras não produziu veredito."
        agreement = "parcial"
    elif agree:
        headline = f"Concordam: ambos {_vpt(vp)}."
        agreement = "concordam"
    else:
        headline = (
            f"Divergem: Padrão {_vpt(vp)} × Erick {_vpt(ve)} — a divergência é o sinal."
        )
        agreement = "divergem"

    if agree:
        concordancia = (
            f"Os dois métodos chegam ao mesmo veredito: **{_vpt(vp)}**. A tese de "
            "fundo (Padrão) e o timing pelo método Erick apontam na mesma direção — "
            "confluência, sinal mais forte."
        )
    else:
        concordancia = (
            "Ambos analisaram o mesmo ativo, na mesma data, com a mesma base de "
            "dados (preço, notícias, derivativos). O que diverge é a leitura — abaixo."
        )

    if agree:
        divergencia = (
            "Sem divergência no veredito. O método Erick apenas detalha o timing e o "
            "peso da entrada" + (f" — {setup_pt}." if setup_pt else ".")
        )
    else:
        linhas = [
            f"- **Padrão** ({_tf(tfp)}): {_vpt(vp)} — leitura de fundo (fundamento, "
            "notícia, tendência dominante).",
            f"- **Erick** ({_tf(tfe)}): {_vpt(ve)} — timing pelo recuo à média"
            + (f", {setup_pt}." if setup_pt else "."),
        ]
        divergencia = "\n".join(linhas)

    if agree:
        significado = (
            "Os dois ângulos — tese de fundo × timing intradiário — concordam. É o "
            "cenário de maior convicção: dá pra agir com o peso que o método Erick "
            "indicar, sem contradição entre horizonte curto e longo."
        )
    elif not have_both:
        significado = (
            "Uma das leituras ficou indisponível; use a que concluiu e considere "
            "reavaliar a outra antes de decidir."
        )
    else:
        cons = _more_conservative(vp, ve)
        if _vkey(cons) == _vkey(ve):
            significado = (
                f"O Padrão vê a tese de fundo (**{_vpt(vp)}**), mas o método Erick — "
                f"no frame {_tf(tfe)} — está mais cauteloso (**{_vpt(ve)}**): o timing "
                "ainda não confirmou. Leitura: esperar o recuo à média antes de agir "
                "com peso; entrar cedo é comprar contra o timing."
            )
        else:
            significado = (
                f"O método Erick vê um gatilho de timing (**{_vpt(ve)}** no frame "
                f"{_tf(tfe)}) que a tese de fundo Padrão (**{_vpt(vp)}**) ainda não "
                "endossa. Leitura: entrada tática de peso reduzido, ciente de que o "
                "horizonte longo não acompanha — realizar antes se a tese não virar."
            )

    return {
        "agreement": agreement,
        "verdict": meta_verdict,
        "verdict_padrao": vp,
        "verdict_erick": ve,
        "timeframe_padrao": tfp,
        "timeframe_erick": tfe,
        "headline": headline,
        "concordancia": concordancia,
        "divergencia": divergencia,
        "significado": significado,
    }

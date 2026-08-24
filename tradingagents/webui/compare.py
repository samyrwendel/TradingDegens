"""Comparação de duas leituras + meta-juiz (Fase 3 do Modo Erick).

Confronta DUAS análises do mesmo ativo — via o atalho "comparar" (roda Padrão +
Erick de uma vez, task 017) ou o fluxo MANUAL (selecionar duas análises já
existentes no histórico e mandar confrontar, task 018). O meta-juiz nomeia
explicitamente as duas (método · timeframe · data), aponta onde CONCORDAM e onde
DIVERGEM, e o que a divergência significa pra decisão — a divergência é o sinal.

Determinístico de propósito: ancorado nos dois vereditos + timeframes + o setup do
método (nunca inventa), sem uma 3ª chamada LLM, e testável.
"""

from __future__ import annotations

# 5-tier rating -> pt-BR practical meaning (mirror of the frontend VERDICT_PT).
_VERDICT_PT = {
    "buy": "COMPRAR", "overweight": "AUMENTAR", "hold": "MANTER",
    "underweight": "REDUZIR", "sell": "VENDER",
}
# Conservatism order (sell most cautious → buy most aggressive).
_ORDER = {"sell": 0, "underweight": 1, "hold": 2, "overweight": 3, "buy": 4}
_TF_PT = {"1w": "semanal", "1d": "diário", "4h": "4h", "1h": "1h", "15m": "15m"}
# Timeframe from widest→narrowest, so we can tell which side is the "timing" frame.
_TF_ORDER = {"1w": 4, "1d": 3, "4h": 2, "1h": 1, "15m": 0}


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


def method_label(method: str) -> str:
    return "Método Erick" if method == "erick" else "Padrão"


def detect_method(record: dict) -> str:
    """Infer a run's method from its stored result (Erick if it wrote an Erick
    report). Compare records report ``"compare"`` and are not single readings."""
    res = record.get("result") or {}
    if res.get("compare"):
        return "compare"
    return "erick" if (res.get("erick_report") or "").strip() else "padrao"


def column_label(col: dict) -> str:
    """Human label naming WHICH reading this is: método · timeframe."""
    return f"{method_label(col.get('method'))} · {_tf(col.get('timeframe', '1d'))}"


def confront_pair_valid(col_a: dict, col_b: dict) -> bool:
    """A confront is legitimate ONLY as Padrão × Erick on the SAME timeframe and
    date — one reading of each method, same frame, same day (Samyr's rule, task
    024). Two of the same method, or mismatched frames/dates, is NOT a confront:
    it is the trivial 'método contra ele mesmo' the meta-judge must never label
    'Concordam'. The caller reroutes an invalid pair through ``start_compare``."""
    if {col_a.get("method"), col_b.get("method")} != {"padrao", "erick"}:
        return False
    if (col_a.get("timeframe") or "1d") != (col_b.get("timeframe") or "1d"):
        return False
    if (col_a.get("date") or "") != (col_b.get("date") or ""):
        return False
    return True


def build_column(record: dict, method: str) -> dict:
    """A comparison column from a persisted run record (or a snapshot dict).

    Carries the verdict, its timeframe, the operable plan, the cost, the date and a
    human ``label`` (método · timeframe) so the meta-judge and UI can name exactly
    which reading each column is. ``_reused`` marks a column served from cache.
    """
    r = record.get("result") or {}
    col = {
        "method": method,
        "run_id": record.get("run_id"),
        "date": record.get("date") or r.get("date") or "",
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
        # Chart of THIS reading (its own timeframe/EMA/1-2-3/bands) so the compare
        # view can show both charts side by side (task 019).
        "price_chart": r.get("price_chart") or {},
        "cost": record.get("cost") or {"usd": record.get("cost_usd", 0) or 0},
        "elapsed": record.get("elapsed"),
        "degraded": r.get("degraded") or [],
        "reused": bool(record.get("_reused")),
    }
    col["label"] = column_label(col)
    return col


def meta_judge(a: dict, b: dict) -> dict:
    """Confront two columns: agree/diverge + the decision meaning, anchored in the
    real verdicts, timeframes, methods and setup — naming exactly which two.

    Works for any pair of the same ticker: Padrão × Erick (thesis vs timing) or two
    timeframes of either method (trend frame vs timing frame).
    """
    va, vb = a.get("verdict"), b.get("verdict")
    tfa, tfb = a.get("timeframe", "1d"), b.get("timeframe", "1d")
    la, lb = column_label(a), column_label(b)
    da, db = a.get("date", ""), b.get("date", "")
    have_both = bool(_vkey(va)) and bool(_vkey(vb))
    # Defesa (task 024): dois do MESMO método não é confronto — é concordância
    # trivial ("6 com meia dúzia"). Nunca emitir "Concordam: ambos X" pra isso;
    # é estado de ERRO. Os chamadores só passam Padrão × Erick, então isto é rede
    # de segurança — se disparar, algo montou o par errado.
    if a.get("method") == b.get("method") and a.get("method") in ("padrao", "erick"):
        return _same_method_error(a, b, la, lb, va, vb, da, db)
    agree = have_both and _vkey(va) == _vkey(vb)
    meta_verdict = va if agree else _more_conservative(va, vb)
    diff_method = a.get("method") != b.get("method")
    diff_tf = _vkey(tfa) != _vkey(tfb)

    setup = (
        (b.get("actionable") or {}).get("setup_state")
        or (a.get("actionable") or {}).get("setup_state") or ""
    )
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
        headline = f"Concordam: ambos {_vpt(va)}."
        agreement = "concordam"
    else:
        headline = f"Divergem: {la} {_vpt(va)} × {lb} {_vpt(vb)} — a divergência é o sinal."
        agreement = "divergem"

    # Quem está a par (naming the exact two readings + dates: criterion 4)
    par = (
        f"Confronto: **{la}**{f' ({da})' if da else ''} × **{lb}**"
        f"{f' ({db})' if db else ''}."
    )

    if agree:
        concordancia = (
            f"{par} Os dois chegam ao mesmo veredito: **{_vpt(va)}**. As leituras "
            "apontam na mesma direção — confluência, sinal mais forte."
        )
    else:
        concordancia = (
            f"{par} Mesma base de dados (preço, notícias, derivativos); o que "
            "diverge é a leitura — abaixo."
        )

    if agree:
        divergencia = (
            "Sem divergência no veredito." + (f" O timing: {setup_pt}." if setup_pt else "")
        )
    else:
        divergencia = (
            f"- **{la}**{f' ({da})' if da else ''}: {_vpt(va)}\n"
            f"- **{lb}**{f' ({db})' if db else ''}: {_vpt(vb)}"
            + (f"\n- Timing (método Erick): {setup_pt}." if setup_pt else "")
        )

    significado = _meaning(
        agree, have_both, va, vb, tfa, tfb, la, lb, diff_method, diff_tf, a, b
    )

    return {
        "agreement": agreement,
        "verdict": meta_verdict,
        "verdict_a": va, "verdict_b": vb,
        "timeframe_a": tfa, "timeframe_b": tfb,
        "label_a": la, "label_b": lb,
        "date_a": da, "date_b": db,
        "headline": headline,
        "concordancia": concordancia,
        "divergencia": divergencia,
        "significado": significado,
    }


def _same_method_error(a, b, la, lb, va, vb, da, db) -> dict:
    """Confront result for an invalid pair (same method against itself, task 024).

    Not a confront — an error state. Never says 'Concordam'; it names the mistake
    and points to the fix (run the missing method so it is a real Padrão × Erick)."""
    return {
        "agreement": "invalido",
        "verdict": None,
        "verdict_a": va, "verdict_b": vb,
        "timeframe_a": a.get("timeframe", "1d"), "timeframe_b": b.get("timeframe", "1d"),
        "label_a": la, "label_b": lb,
        "date_a": da, "date_b": db,
        "headline": "Confronto exige Padrão × Erick — não método contra ele mesmo.",
        "concordancia": "",
        "divergencia": (
            f"Par inválido: **{la}** × **{lb}** — o mesmo método contra ele mesmo."
        ),
        "significado": (
            "Comparar um método com ele mesmo é concordância trivial (6 com meia "
            "dúzia). Rode o método que falta no mesmo timeframe e confronte "
            "Padrão × Erick de verdade — a divergência entre os dois é o sinal."
        ),
    }


def _meaning(agree, have_both, va, vb, tfa, tfb, la, lb, diff_method, diff_tf, a, b) -> str:
    if agree:
        return (
            "Os dois ângulos concordam — é o cenário de maior convicção: dá pra agir "
            "com o peso indicado, sem contradição entre horizonte e timing."
        )
    if not have_both:
        return (
            "Uma das leituras ficou indisponível; use a que concluiu e considere "
            "reavaliar a outra antes de decidir."
        )
    cons = _more_conservative(va, vb)
    if diff_method:
        # Padrão (tese de fundo) × Erick (timing pelo recuo à média)
        erick_is_a = a.get("method") == "erick"
        erick_v = va if erick_is_a else vb
        padrao_v = vb if erick_is_a else va
        erick_l = la if erick_is_a else lb
        if _vkey(cons) == _vkey(erick_v):
            return (
                f"O Padrão vê a tese de fundo (**{_vpt(padrao_v)}**), mas o método "
                f"Erick ({erick_l}) está mais cauteloso (**{_vpt(erick_v)}**): o timing "
                "ainda não confirmou. Leitura: esperar o recuo à média antes de agir "
                "com peso — entrar cedo é comprar contra o timing."
            )
        return (
            f"O método Erick ({erick_l}) vê um gatilho de timing (**{_vpt(erick_v)}**) "
            f"que a tese de fundo Padrão (**{_vpt(padrao_v)}**) ainda não endossa. "
            "Leitura: entrada tática de peso reduzido, realizar antes se a tese não virar."
        )
    if diff_tf:
        # mesmo método, frames diferentes: o mais curto é timing, o mais longo é tendência
        short_is_a = _TF_ORDER.get(tfa, 3) <= _TF_ORDER.get(tfb, 3)
        short_l, short_v = (la, va) if short_is_a else (lb, vb)
        long_l, long_v = (lb, vb) if short_is_a else (la, va)
        return (
            f"Mesmo método, frames diferentes: o frame longo ({long_l}) dá a tendência "
            f"(**{_vpt(long_v)}**) e o curto ({short_l}) o timing (**{_vpt(short_v)}**). "
            "Divergência = tendência e timing desalinhados: siga a tendência do frame "
            "longo e use o curto só pra cronometrar a entrada."
        )
    # mesma configuração (mesmo método e frame) — divergência é dado/ruído
    return (
        "Mesma configuração (método e frame iguais): a divergência vem de dado "
        f"atualizado ou ruído entre as rodadas. O lado mais cauteloso é **{_vpt(cons)}** "
        "— na dúvida, prevaleça o mais conservador e rode de novo pra confirmar."
    )


# Back-compat alias: the auto "comparar" path (task 017) passes (padrao, erick).
deterministic_meta = meta_judge

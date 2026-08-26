"""Pre-publication contradiction checker (spec item 7 — the keystone).

A final, mostly-deterministic pass over the ASSEMBLED report that catches the whole
class of inconsistencies items 1-6 fix, so a regression on any of them surfaces
instead of shipping silently. It reads the run ``result`` dict (module texts +
structured fields) and returns a list of findings; the runner attaches them and the
UI renders a "Checagem de consistência" section. It never blocks — a listed
inconsistency is more useful than a hard stop, and false-positives must not gate a run.

Deterministic where it counts:
* **decisão dupla** — more than one module states a "PROPOSTA FINAL DE TRANSAÇÃO"
  (or one conflicts with the canonical ``final_decision``);
* **1-2-3 incoerente** — the chart trigger disagrees with the report's structure
  section, or ``acionado`` disagrees with price vs the trigger;
* **preço divergente** — the frozen ``as_of_price`` / actionable price / chart close
  disagree beyond tolerance;
* **agregado que não soma** — a cited "FCF ... TTM" disagrees with the deterministic
  anchor value in the same report.
"""
from __future__ import annotations

import re
from typing import Any

# pt-BR action vocabulary the modules emit as a "final proposal".
_ACTIONS = ("COMPRAR", "AUMENTAR", "MANTER", "REDUZIR", "VENDER", "AGUARDAR")

_MODULE_TEXT_KEYS = (
    "market_report", "erick_report", "trader_plan", "investment_plan",
    "research_manager", "risk_decision", "fundamentals_report", "bull", "bear",
    "news_report", "sentiment_report",
)

# Tolerances.
_PRICE_TOL = 0.01        # 1% — structured price agreement
_TTM_TOL = 0.05          # 5% — cited aggregate vs the computed anchor


def _finding(code: str, severity: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


def _pt_number(token: str) -> float | None:
    """Parse a numeric token that may be pt-BR (``160,87`` / ``601.306.000``) or
    en (``160.87``). Returns ``None`` when it is not a clean number."""
    t = token.strip().replace("US$", "").replace("$", "").strip()
    t = t.replace("\u2212", "-")  # unicode minus
    neg = t.startswith("-")
    t = t.lstrip("+-").strip()
    if not re.fullmatch(r"[\d.,]+", t):
        return None
    if "," in t and "." in t:
        # last separator is the decimal one
        dec = "," if t.rfind(",") > t.rfind(".") else "."
        thou = "." if dec == "," else ","
        t = t.replace(thou, "").replace(dec, ".")
    elif "," in t:
        # comma alone: decimal if it looks like "160,87", else thousands
        t = t.replace(",", ".") if re.fullmatch(r"\d{1,3},\d{1,2}", t) else t.replace(",", "")
    else:
        # dots: thousands if grouped in 3s (601.306.000), else decimal
        if re.fullmatch(r"\d{1,3}(\.\d{3})+", t):
            t = t.replace(".", "")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def _money_to_units(value: float, unit: str) -> float:
    u = (unit or "").lower()
    if u.startswith(("t", "tri")) or "trilh" in u:
        return value * 1e12
    if u.startswith(("b", "bi")) or "bilh" in u:
        return value * 1e9
    if u.startswith(("m", "mi")) or "milh" in u:
        return value * 1e6
    if u.startswith(("k", "mil")) and "milh" not in u:
        return value * 1e3
    return value


def _final_proposals(result: dict[str, Any]) -> list[tuple[str, str]]:
    """(module_key, ACTION) for every 'PROPOSTA FINAL DE TRANSAÇÃO: X' in the texts."""
    out: list[tuple[str, str]] = []
    pat = re.compile(r"PROPOSTA FINAL DE TRANSA[ÇC][ÃA]O:\s*\**\s*([A-Za-zÀ-ú]+)", re.IGNORECASE)
    for key in _MODULE_TEXT_KEYS:
        text = result.get(key) or ""
        if not isinstance(text, str):
            continue
        for m in pat.finditer(text):
            action = m.group(1).strip().upper()
            if action in _ACTIONS:
                out.append((key, action))
    return out


def _check_double_decision(result: dict[str, Any]) -> list[dict[str, str]]:
    proposals = _final_proposals(result)
    if not proposals:
        return []
    actions = {a for _, a in proposals}
    where = ", ".join(f"{k}={a}" for k, a in proposals)
    findings = []
    if len(actions) >= 2:
        findings.append(_finding(
            "decisao_dupla", "alta",
            f"Decisões finais conflitantes em módulos diferentes ({where}). "
            "A única decisão é o veredito do risco/portfolio; os módulos deveriam "
            "ser leitura, sem 'PROPOSTA FINAL DE TRANSAÇÃO'.",
        ))
    else:
        findings.append(_finding(
            "proposta_concorrente", "média",
            f"Módulo(s) emitindo 'PROPOSTA FINAL DE TRANSAÇÃO' ({where}) — deveria "
            "ser leitura, não decisão final concorrente.",
        ))
    final = (result.get("final_decision") or "").strip().upper()
    if final:
        conflict = {a for a in actions if a != final and a in _ACTIONS[:5]}
        if conflict:
            findings.append(_finding(
                "modulo_vs_veredito", "alta",
                f"Módulo propõe {', '.join(sorted(conflict))} contra o veredito "
                f"canônico {final}.",
            ))
    return findings


def _section_trigger(market_report: str) -> tuple[float | None, str | None]:
    """Parse the 1-2-3 trigger + direction word from the structure section."""
    if not isinstance(market_report, str):
        return None, None
    m = re.search(
        r"Gatilho\**:\s*(perda|rompimento)\s+de\s+([\d.,]+)", market_report, re.IGNORECASE
    )
    if not m:
        return None, None
    return _pt_number(m.group(2)), m.group(1).lower()


def _check_pattern_123(result: dict[str, Any]) -> list[dict[str, str]]:
    chart = ((result.get("price_chart") or {}).get("markers") or {}).get("pattern_123")
    if not chart:
        return []
    findings: list[dict[str, str]] = []
    trig_chart = chart.get("trigger")
    state = (chart.get("state") or "").lower()
    direction = (chart.get("direction") or "").lower()

    trig_text, _verb = _section_trigger(result.get("market_report") or "")
    if (
        trig_chart is not None
        and trig_text is not None
        and abs(float(trig_chart) - float(trig_text)) > 0.01
    ):
        findings.append(_finding(
            "gatilho_123_divergente", "alta",
            f"Gatilho 1-2-3 do gráfico ({trig_chart}) ≠ do texto do relatório "
            f"({trig_text}). Devem sair do mesmo padrão.",
        ))

    price = _reference_price(result)
    if trig_chart is not None and price is not None and state == "acionado":
        t = float(trig_chart)
        incoherent = (direction == "venda" and price >= t) or (
            direction == "compra" and price <= t
        )
        if incoherent:
            rel = "≥" if direction == "venda" else "≤"
            findings.append(_finding(
                "acionado_incoerente", "alta",
                f"1-2-3 de {direction} marcado 'acionado' mas o preço ({price}) {rel} "
                f"o gatilho ({t}) — o gatilho não foi rompido na direção do padrão.",
            ))
    return findings


def _reference_price(result: dict[str, Any]) -> float | None:
    if result.get("as_of_price") is not None:
        return float(result["as_of_price"])
    price = (result.get("actionable") or {}).get("price")
    return float(price) if price is not None else None


def _chart_last_close(result: dict[str, Any]) -> float | None:
    candles = (result.get("price_chart") or {}).get("candles") or []
    if candles and isinstance(candles[-1], dict):
        c = candles[-1].get("c")
        return float(c) if c is not None else None
    return None


def _check_price_drift(result: dict[str, Any]) -> list[dict[str, str]]:
    prices = {
        "as_of_price": result.get("as_of_price"),
        "actionable": (result.get("actionable") or {}).get("price"),
        "gráfico (último candle)": _chart_last_close(result),
    }
    vals = {k: float(v) for k, v in prices.items() if v is not None}
    if len(vals) < 2:
        return []
    lo, hi = min(vals.values()), max(vals.values())
    if hi <= 0 or (hi - lo) / hi <= _PRICE_TOL:
        return []
    spread = ", ".join(f"{k}={v}" for k, v in vals.items())
    return [_finding(
        "preco_divergente", "média",
        f"Preço de referência divergente entre fontes ({spread}). Deveria haver um "
        "preço as_of único por run.",
    )]


def _check_market_cap_price(result: dict[str, Any]) -> list[dict[str, str]]:
    """A market cap ÷ shares that implies a price far from the reference is the
    classic fundamentals drift (AAOI: 9,56 bi ÷ 80,2 mi ≈ US$ 119 vs referência
    113,15, off a stale live market cap instead of the frozen as_of price)."""
    ref = _reference_price(result)
    text = result.get("fundamentals_report") or ""
    if ref is None or not isinstance(text, str) or ref <= 0:
        return []
    mc = re.search(
        r"capitaliza\w*[^\n]*?US\$\s*([\d.,]+)\s*(trilh\w*|bilh\w*|milh\w*|tri|bi|mi|b|m)",
        text, re.IGNORECASE,
    )
    sh = re.search(r"([\d.,]+)\s*(milh\w*|bilh\w*|mi|bi|m|b)\s*(?:de\s*)?ações", text, re.IGNORECASE)
    if not mc or not sh:
        return []
    market_cap = _money_to_units(_pt_number(mc.group(1)) or 0, mc.group(2))
    shares = _money_to_units(_pt_number(sh.group(1)) or 0, sh.group(2))
    if market_cap <= 0 or shares <= 0:
        return []
    implied = market_cap / shares
    if abs(implied - ref) / ref <= 0.03:
        return []
    return [_finding(
        "preco_market_cap_divergente", "média",
        f"Market cap ({market_cap:,.0f}) ÷ ações ({shares:,.0f}) implica preço "
        f"~{implied:,.2f}, divergente do preço de referência ({ref}). Use o preço "
        "as_of congelado para o market cap.",
    )]


def _anchor_fcf_ttm(result: dict[str, Any]) -> float | None:
    """The deterministic anchor's FCF TTM (rendered as 'FCF TTM (soma de ...): X')."""
    text = result.get("fundamentals_report") or ""
    if not isinstance(text, str):
        return None
    # O agregado agora vem com magnitude ("FCF TTM (soma de ...): US$ 136.68 bilhões"),
    # então captura número + palavra e converte pra unidades absolutas — mesma
    # semântica do que o agente cita, pra os dois compararem na mesma escala (bug 014).
    m = re.search(
        r"FCF TTM[^:\n]*:\s*(-?\s*(?:US\$\s*)?[\d.,]+)\s*(trilh\w*|bilh\w*|milh\w*|tri|bi|mi|b|m|k)?",
        text, re.IGNORECASE,
    )
    if not m:
        return None
    num = _pt_number(m.group(1))
    return _money_to_units(num, m.group(2) or "") if num is not None else None


def _cited_fcf_ttm(result: dict[str, Any]) -> list[tuple[str, float]]:
    """Every 'FCF ... TTM ... value' cited in prose, in absolute currency units."""
    out: list[tuple[str, float]] = []
    pat = re.compile(
        r"FCF[^.\n]{0,40}?(-?\s*(?:US\$\s*)?[\d.,]+)\s*(trilh\w*|bilh\w*|milh\w*|tri|mil|bi|mi|b|m|k)?"
        r"[^.\n]{0,20}?TTM",
        re.IGNORECASE,
    )
    for key in ("bear", "bull", "fundamentals_report", "research_manager", "investment_plan"):
        text = result.get(key) or ""
        if not isinstance(text, str):
            continue
        for m in pat.finditer(text):
            num = _pt_number(m.group(1))
            if num is None:
                continue
            out.append((key, _money_to_units(num, m.group(2) or "")))
    return out


def _check_aggregates(result: dict[str, Any]) -> list[dict[str, str]]:
    anchor = _anchor_fcf_ttm(result)
    cited = _cited_fcf_ttm(result)
    if not cited:
        return []
    findings: list[dict[str, str]] = []
    if anchor is not None and abs(anchor) > 0:
        for key, val in cited:
            if abs(val - anchor) / max(abs(anchor), 1.0) > _TTM_TOL:
                findings.append(_finding(
                    "fcf_ttm_divergente", "alta",
                    f"FCF TTM citado em '{key}' ({val:,.0f}) diverge do agregado "
                    f"determinístico ({anchor:,.0f}, soma dos 4 trimestres).",
                ))
    else:
        # No anchor in this report: flag if two cited TTM values disagree with each other.
        uniq = {round(v, -6) for _, v in cited}
        if len(uniq) >= 2:
            spread = ", ".join(f"{k}={v:,.0f}" for k, v in cited)
            findings.append(_finding(
                "fcf_ttm_inconsistente", "média",
                f"FCF TTM citado com valores diferentes no relatório ({spread}).",
            ))
    return findings


_ERICK_STATES = ("AGIR", "AGUARDAR", "CAIXA")


def _check_erick_state(result: dict[str, Any]) -> list[dict[str, str]]:
    """The Erick module must speak ONE state (item 6b): the deterministic 'Estado' and
    any prose 'Veredito' must agree, never 'Veredito AGUARDAR' vs 'Estado AGIR'."""
    text = result.get("erick_report") or ""
    if not isinstance(text, str) or not text:
        return []
    est = re.search(r"Estado[^\n:]*:\**\s*(AGIR|AGUARDAR|CAIXA)", text, re.IGNORECASE)
    ver = re.search(r"Veredito[^\n:]*:?\**\s*(AGIR|AGUARDAR|CAIXA)", text, re.IGNORECASE)
    if not est or not ver:
        return []
    a, b = est.group(1).upper(), ver.group(1).upper()
    if a != b:
        return [_finding(
            "erick_estado_veredito_divergente", "alta",
            f"Método Erick contradiz a si mesmo: Estado={a} vs Veredito={b}. O módulo "
            "deve emitir UM estado único (AGIR/AGUARDAR/CAIXA).",
        )]
    return []


def _oi_value(text: str, near: str) -> float | None:
    """First open-interest magnitude near an OI cue in ``text`` (absolute units)."""
    if not isinstance(text, str):
        return None
    m = re.search(
        near + r"[^\n]{0,60}?([\d.,]+)\s*(bilh\w*|milh\w*|mi|bi|M|B|k)?",
        text, re.IGNORECASE,
    )
    if not m:
        return None
    num = _pt_number(m.group(1))
    return _money_to_units(num, m.group(2) or "") if num is not None else None


def _check_oi_divergence(result: dict[str, Any]) -> list[dict[str, str]]:
    """Same indicator (open interest) with values from different sources and no note
    (item 6e): the labeled derivatives feed vs an unlabeled prose figure."""
    feed = _oi_value(result.get("derivatives_report") or "", r"open interest[^\n]*?\):")
    if feed is None or feed <= 0:
        return []
    for key in ("news_report", "bull", "bear", "research_manager"):
        prose = _oi_value(result.get(key) or "", r"(?:open interest|contratos em aberto|\bOI\b)")
        if prose is None or prose <= 0:
            continue
        if abs(prose - feed) / max(feed, 1.0) > 0.25:
            return [_finding(
                "oi_divergente", "média",
                f"Open interest com valores divergentes sem rótulo: feed ={feed:,.0f} "
                f"vs '{key}' ={prose:,.0f}. Rotule origem+escopo ou suprima o menos "
                "confiável.",
            )]
    return []


def check_contradictions(result: dict[str, Any] | None) -> list[dict[str, str]]:
    """Run all deterministic checks over an assembled ``result`` → list of findings.

    Fail-open: a bug in any single check must not blow up the run, so each is guarded.
    """
    if not isinstance(result, dict):
        return []
    findings: list[dict[str, str]] = []
    for check in (
        _check_double_decision,
        _check_pattern_123,
        _check_price_drift,
        _check_market_cap_price,
        _check_aggregates,
        _check_erick_state,
        _check_oi_divergence,
    ):
        try:
            findings.extend(check(result))
        except Exception:  # noqa: BLE001 — a checker bug must never break the run
            continue
    return findings


_SEVERITY_ICON = {"alta": "🔴", "média": "🟡", "baixa": "🟢"}


def render_contradictions_section(findings: list[dict[str, str]]) -> str:
    """Render the findings as a pt-BR markdown section for the report."""
    if not findings:
        return (
            "## ✅ Checagem de consistência\n\n"
            "_Nenhuma inconsistência detectada: decisão única, gatilho 1-2-3 coerente, "
            "preço de referência único, agregados batem._"
        )
    lines = ["## ⚠️ Checagem de consistência", ""]
    for f in findings:
        icon = _SEVERITY_ICON.get(f.get("severity", ""), "•")
        lines.append(f"- {icon} **{f.get('code', '')}**: {f.get('message', '')}")
    return "\n".join(lines)


# ── Validação ANTES do juiz (FRENTE 2 / task 016) ────────────────────────────
# O checker acima roda PÓS-publicação (trava final). Aqui a MESMA lógica roda
# UPSTREAM (antes do nó de decisão): monta um bloco "DADOS VERIFICADOS" com os
# números canônicos + as inconsistências detectadas pra injetar no contexto do juiz,
# pra a DECISÃO não se apoiar calada num dado furado. Reusa check_contradictions —
# não duplica os checks.

_ANCHORS_HEADING = "## Âncoras determinísticas"


def _extract_anchors_section(fundamentals_report: str) -> str:
    """Recorta o bloco de âncoras determinísticas do relatório de fundamentos —
    os números canônicos (preço as_of, market cap, FCF/FCO/Capex TTM com magnitude).
    Vazio quando o relatório não trouxe âncoras."""
    if not isinstance(fundamentals_report, str) or _ANCHORS_HEADING not in fundamentals_report:
        return ""
    start = fundamentals_report.index(_ANCHORS_HEADING)
    rest = fundamentals_report[start:]
    # vai até o próximo heading de nível 2 (ou o fim)
    nxt = re.search(r"\n## ", rest[len(_ANCHORS_HEADING):])
    return rest if nxt is None else rest[: len(_ANCHORS_HEADING) + nxt.start()].rstrip()


def build_verified_context(reports: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    """Roda os checks determinísticos ANTES da decisão e devolve
    ``(bloco_markdown, findings)``.

    O bloco "DADOS VERIFICADOS" carrega os números canônicos (âncoras) e a lista de
    inconsistências detectadas, com a instrução de decidir SÓ por eles (valor em
    conflito = o verificado). ``findings`` volta pra marcar o veredito quando sobra
    inconsistência. Fail-open: nunca levanta (o próprio check_contradictions é guardado).
    """
    findings = check_contradictions(reports if isinstance(reports, dict) else {})
    anchors = _extract_anchors_section((reports or {}).get("fundamentals_report", ""))
    out = [
        "## DADOS VERIFICADOS (canônicos — decida SÓ por estes)",
        "Estes são os números DETERMINÍSTICOS da análise. Se um agente citou um valor "
        "que CONFLITA com estes, o valor válido é ESTE, nunca o citado.",
    ]
    if anchors:
        out += ["", anchors]
    if findings:
        out += ["", f"### ⚠️ {len(findings)} inconsistência(s) detectada(s) nos insumos "
                "(use o valor verificado, não o citado):"]
        for f in findings:
            out.append(f"- {f.get('message', '')}")
    return "\n".join(out), findings


def format_verdict_caveat(findings: list[dict[str, str]] | None) -> str:
    """Carimbo curto pro veredito quando os insumos tinham inconsistência na hora da
    decisão (task 016). Vazio quando não há nada a avisar."""
    n = len(findings or [])
    if not n:
        return ""
    return (f"⚠️ Decidido com {n} inconsistência(s) nos insumos — os valores "
            "verificados foram dados ao juiz; tratar com cautela.")

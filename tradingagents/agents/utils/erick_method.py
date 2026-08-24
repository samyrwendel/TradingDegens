"""Leitura determinística do MÉTODO ERICK — o núcleo do analista `erick`.

O analista `erick` (LLM) escreve a moldura (regime, macro, filtros de sentimento
e derivativo). Este módulo garante — determinístico, ancorado em dado de
ferramenta, jamais inventado — a parte que o método EXIGE e que a prosa costuma
deixar solta: **timeframe intradiário, entrada no recuo à média (EMA 8/21),
saída antes da reversão, e o PESO RELATIVO do trade** (posição cheia / meia /
inicial / caixa) — a resposta ao "quantos %" do Samyr sem chutar valor absoluto.

Modelado de 4 fontes independentes (59 transcrições, 18 frames de gráfico,
carteira real 62% em caixa, racional escrito por posição). Ver
`~/brain/trading-ops/modelo-decisorio-erick-sekiama.md`. O eixo é a média móvel
(EMA 8/21 pro timing, 50 pra tendência), no 15m/4h; entrada FRACIONADA no recuo
à média; saída em exaustão ("pega a maior parte e sai antes de reverter"); caixa
é posição ativa; separa tático de estrutural.

Tudo reusa a fundação já pronta (EMA + intradiário + região/1-2-3 do
price_structure, cacheado e date-guarded). Fail-open: qualquer erro devolve o
relatório intacto — enriquecimento nunca derruba a análise.
"""
from __future__ import annotations

import logging

from tradingagents.dataflows.price_structure import (
    build_actionable_plan_dict,
    build_price_chart,
)

logger = logging.getLogger(__name__)

# Frame primário do método por tipo de ativo. Cripto tem candle real de exchange
# (keyless) no intradiário — o método vive no 4h/15m. Ação não tem feed
# intradiário keyless, então cai no diário e a leitura declara isso.
_CRYPTO_FRAME = "4h"
_CRYPTO_FINE_FRAME = "15m"
_STOCK_FRAME = "1d"

_FRAME_LABEL = {
    "15m": "15 minutos (intradiário)",
    "1h": "1 hora (intradiário)",
    "4h": "4 horas (intradiário)",
    "1d": "diário",
    "1w": "semanal",
}

# Toque na média: dentro desta faixa o preço está "na média" (recuo concluído /
# em curso). Fora, esticado (aguardar recuo) ou abaixo (sem gatilho de alta).
_TOUCH_TOL = 0.004      # 0,4%
_EXTENDED = 0.012       # 1,2% acima da EMA21 = esticado


def _fmt(v) -> str:
    return f"{v:,.2f}" if isinstance(v, (int, float)) else "—"


def _last(seq):
    """Último valor não-nulo de uma série (EMA alinhada tem None no começo)."""
    if not seq:
        return None
    for v in reversed(seq):
        if v is not None:
            return v
    return None


def _fmt_zone(zone) -> str | None:
    """Faixa {label, price, low, high} -> texto. Banda quando há ATR, senão ponto."""
    if not zone:
        return None
    label = zone.get("label") or ""
    low, high, price = zone.get("low"), zone.get("high"), zone.get("price")
    if low is not None and high is not None:
        return f"{label}: {_fmt(low)}–{_fmt(high)}"
    if price is not None:
        return f"{label}: {_fmt(price)}"
    return label or None


def _ema_read(chart: dict) -> dict | None:
    """Leitura de EMA 8/21/50 no último candle do frame. None se sem dado."""
    candles = (chart or {}).get("candles") or []
    ema = (chart or {}).get("ema") or {}
    if not candles:
        return None
    close = candles[-1].get("c")
    e8, e21, e50 = _last(ema.get("8")), _last(ema.get("21")), _last(ema.get("50"))
    if close is None or e21 is None:
        return None

    # Tendência pela pilha de médias (o "regime antes do preço" do método).
    if e8 is not None and e50 is not None and e8 > e21 > e50:
        trend = "alta"
    elif e8 is not None and e50 is not None and e8 < e21 < e50:
        trend = "baixa"
    else:
        trend = "transicao"

    dist8 = (close - e8) / e8 if e8 else None
    dist21 = (close - e21) / e21
    near = min(abs(d) for d in (dist8, dist21) if d is not None)
    at_media = near <= _TOUCH_TOL
    extended = trend == "alta" and dist21 > _EXTENDED
    below = close < e21
    return {
        "close": close, "e8": e8, "e21": e21, "e50": e50,
        "dist8": dist8, "dist21": dist21,
        "trend": trend, "at_media": at_media, "extended": extended, "below": below,
    }


_TREND_PT = {"alta": "alta (médias empilhadas)", "baixa": "baixa (médias invertidas)",
             "transicao": "transição (médias entrelaçadas)"}


def _decide(r: dict) -> dict:
    """Do read de EMA para o veredito do método: agir/aguardar, entrada e PESO.

    Peso relativo sempre FRACIONADO (o método nunca entra 100% de uma vez) e
    responde em termos relativos — cheia / meia / inicial / caixa — não em %
    absoluto inventado.
    """
    trend, at_media, extended, below = r["trend"], r["at_media"], r["extended"], r["below"]
    e8, e21 = r["e8"], r["e21"]

    if trend == "alta":
        if at_media:
            return {
                "acao": "AGIR",
                "entrada": f"preço recuou até a média agora (EMA 8 {_fmt(e8)} · EMA 21 {_fmt(e21)}) — é o ponto de entrada no recuo",
                "peso": "meia posição",
                "peso_racional": "monta fracionado no toque da média, com espaço para adicionar se ceder à EMA seguinte — nunca 100% de uma vez",
            }
        if extended:
            return {
                "acao": "AGUARDAR",
                "entrada": f"esticado acima da média — aguardar recuo à EMA 8 ({_fmt(e8)}) ou EMA 21 ({_fmt(e21)})",
                "peso": "caixa",
                "peso_racional": "sem entrada no esticado; posição inicial só no recuo à média",
            }
        return {
            "acao": "AGIR",
            "entrada": f"tendência de alta, preço entre as médias — recuo à EMA 21 ({_fmt(e21)}) é a referência de entrada",
            "peso": "posição inicial",
            "peso_racional": "confirmação parcial: só uma fração inicial, aguardando recuo mais limpo à média",
        }

    if trend == "transicao":
        if at_media and not below:
            return {
                "acao": "AGIR",
                "entrada": f"médias entrelaçadas, preço na EMA 21 ({_fmt(e21)}) — entrada tentativa no recuo",
                "peso": "posição inicial",
                "peso_racional": "sem alinhamento pleno de médias: só inicial, com confirmação antes de somar",
            }
        return {
            "acao": "AGUARDAR",
            "entrada": f"médias sem alinhamento — aguardar recuo à EMA 21 ({_fmt(e21)}) e confirmação acima da EMA 8 ({_fmt(e8)})",
            "peso": "caixa",
            "peso_racional": "sem tendência definida, caixa até o gatilho",
        }

    # baixa
    return {
        "acao": "AGUARDAR",
        "entrada": f"tendência de baixa (preço sob a EMA 21 {_fmt(e21)}) — não comprar contra a tendência sem confirmação",
        "peso": "caixa",
        "peso_racional": "filtro do método: sem entrada contra médias invertidas; caixa é a posição",
    }


def _saida(plan: dict, read: dict) -> str:
    """Saída antes da reversão: topo anterior (realização) quando existe; senão,
    exaustão/liquidação — 'pega a maior parte e sai antes de reverter', sem alvo
    fixo inventado."""
    realize = _fmt_zone((plan or {}).get("realize_zone"))
    if realize:
        return f"realizar na resistência acima ({realize}) — pegar a maior parte e sair antes da reversão"
    return ("sem topo anterior acima (preço em ar de máxima) — realizar por exaustão/"
            "liquidação, não por alvo fixo ('pega a maior parte e sai antes de reverter')")


def _fine_timing(symbol: str, curr_date: str) -> str | None:
    """Linha de timing fino no 15m (só cripto). Fail-open -> None."""
    try:
        plan = build_actionable_plan_dict(symbol, curr_date, _CRYPTO_FINE_FRAME)
    except Exception:  # noqa: BLE001
        return None
    state = (plan or {}).get("setup_state")
    labels = {
        "ativo": "preço já na média (recuo concluído) — janela de entrada aberta",
        "aguardar_pullback": "aguardando recuo à média para o gatilho fino",
        "aguardar_rompimento": "aguardando rompimento do 1-2-3",
        "sem_setup": "sem gatilho fino no momento",
        "sem_dado": None,
        "intradiario_indisponivel": None,
    }
    txt = labels.get(state)
    return f"**Timing fino (15m):** {txt}" if txt else None


def build_erick_method_section(symbol: str, curr_date: str, asset_type: str) -> str:
    """Seção markdown pt-BR do método Erick — determinística e ancorada em dado.

    Cripto lê no 4h (frame de swing do método) + timing fino no 15m; ação cai no
    diário e declara que o intradiário keyless não existe pra ela. Nunca inventa
    nível: sem candle, diz que a leitura intradiária está indisponível.
    """
    is_crypto = asset_type == "crypto"
    frame = _CRYPTO_FRAME if is_crypto else _STOCK_FRAME
    chart = build_price_chart(symbol, curr_date, timeframe=frame)
    read = _ema_read(chart)

    # Cripto com feed intradiário fora do ar: cai no diário, declarando o degradê.
    degraded_note = ""
    if read is None and is_crypto:
        frame = _STOCK_FRAME
        chart = build_price_chart(symbol, curr_date, timeframe=frame)
        read = _ema_read(chart)
        degraded_note = ("\n\n> Fonte intradiária indisponível agora — leitura caiu no "
                         "diário. Nenhuma barra inventada; o método pede o 4h/15m.")

    frame_label = _FRAME_LABEL.get(frame, frame)
    head = "## 🧭 Método Erick — leitura do setup"

    if read is None:
        extra = ("" if is_crypto else
                 " O intradiário keyless (15m/4h) não existe para ação — o método é "
                 "intradiário, então aqui só o diário/semanal serve de referência de fundo.")
        return (
            f"{head}\n\n"
            f"**Timeframe da leitura:** {frame_label}\n\n"
            f"Sem candle suficiente para a leitura de EMA neste frame — nada inventado."
            f"{extra}"
        )

    decision = _decide(read)
    saida = _saida(build_actionable_plan_dict(symbol, curr_date, frame), read)
    trend_pt = _TREND_PT.get(read["trend"], read["trend"])
    caixa = decision["peso"] == "caixa"

    lines = [
        head,
        "",
        f"**Timeframe da leitura:** {frame_label} — o método opera no 15m/4h; "
        "diário/semanal dão a tendência de fundo.",
        "",
        f"**Regime (médias):** {trend_pt} — preço {_fmt(read['close'])}, "
        f"EMA 8 {_fmt(read['e8'])} · EMA 21 {_fmt(read['e21'])} · EMA 50 {_fmt(read['e50'])}.",
        "",
        f"**Estado:** {decision['acao']}",
        f"**Entrada (recuo à média):** {decision['entrada']}.",
        f"**Saída (antes da reversão):** {saida}.",
        f"**Peso relativo do trade:** {decision['peso']} — {decision['peso_racional']}.",
    ]

    fine = _fine_timing(symbol, curr_date) if is_crypto else None
    if fine:
        lines += ["", fine]

    lines += [
        "",
        "**Tático × estrutural:** esta é uma leitura TÁTICA de curto prazo "
        "(intradiário); a tese estrutural de longo prazo é outra decisão, separada.",
    ]
    if caixa:
        lines.append("**Caixa é posição:** ficar de fora aqui é decisão ativa — "
                     "caixa elevado por escolha, aguardando o ponto.")

    return "\n".join(lines) + degraded_note


def ensure_erick_method_coverage(
    report: str, symbol: str, curr_date: str, asset_type: str
) -> str:
    """Anexa a seção determinística do método ao relatório do analista `erick`.

    Espelha os outros guardas de cobertura (multi-timeframe, derivativos,
    price-structure): a prosa do LLM molda; esta seção garante o núcleo
    operável (timeframe, recuo à média, saída, peso). Fail-open: qualquer erro
    devolve o relatório intacto.
    """
    try:
        section = build_erick_method_section(symbol, curr_date, asset_type)
    except Exception as exc:  # noqa: BLE001 — enriquecimento nunca quebra o relatório
        logger.warning("erick-method coverage failed for %s: %s", symbol, exc)
        return report
    base = (report or "").rstrip()
    return f"{base}\n\n{section}\n" if base else section + "\n"

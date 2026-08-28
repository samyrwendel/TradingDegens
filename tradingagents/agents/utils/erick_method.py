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

# O método vive no intradiário — 4h de swing + 15m de timing fino — para QUALQUER
# ativo: cripto lê o candle real da exchange, ação lê o intradiário keyless do
# yfinance (15m/1h nativos, 4h reamostrado por pregão). Quando a fonte não tem
# candle pro símbolo/data (ação fora da janela intradiária do yfinance, ou feed
# fora do ar) a leitura cai no diário e DECLARA o degradê — nunca inventa barra.
_SWING_FRAME = "4h"
_FINE_FRAME = "15m"
_FALLBACK_FRAME = "1d"

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

# Rótulo compacto do frame para a linha do gatilho 1-2-3 (evita parênteses aninhados).
_COMPACT_FRAME = {"15m": "15m", "1h": "1h", "4h": "4h", "1d": "diário", "1w": "semanal"}


def _estado(
    acao: str, trend: str, drop_cls: str | None = None, gate: bool = False
) -> str:
    """The single method state enum (item 6b): AGIR | AGUARDAR | CAIXA, computed ONCE
    so every sub-block renders from it (no 'Veredito AGUARDAR' vs 'Estado AGIR').

    A natureza da queda (``drop_cls``) é a FONTE DE VERDADE quando presente:
    * ``fraqueza`` → CAIXA (estrutura rompida veta, mesmo um AGIR mecânico);
    * ``liquidacao_saudavel`` numa tendência de "baixa" → AGUARDAR (a inversão das
      EMAs curtas é o SINTOMA da liquidação, não um downtrend a ler como CAIXA).

    ``gate`` (porta TIER 2 aberta, já decidida no ``_decide``): o downtrend do frame
    menor é TIMING da tese de alta do frame maior — AGUARDAR, não CAIXA. Espelha a
    mesma condição do ``_decide`` para o Estado nunca contradizer o Peso na tela.

    ``drop_cls=None`` e ``gate=False`` (defaults) reproduzem a mecânica de hoje
    BYTE-A-BYTE:
    * AGIR — there is an entry at the pullback now;
    * CAIXA — downtrend / no setup: cash IS the active position (filtro do método);
    * AGUARDAR — a valid pullback/breakout is still forming.
    """
    if drop_cls == "fraqueza":
        return "CAIXA"
    if acao == "AGIR":
        return "AGIR"
    if trend == "baixa":
        if drop_cls == "liquidacao_saudavel" or gate:
            return "AGUARDAR"
        return "CAIXA"
    return "AGUARDAR"


# ------------------------------------------------ camada de ponderação ---------
# A hierarquia do método (spec ~/brain/trading-ops/erick-camada-de-ponderacao-spec.md).
# Antes, a decisão era GATILHO ÚNICO: a pilha de EMAs do 4h mandava sozinha, nada
# competia, nada sobrepunha e não havia lista de "não ignore isto" — foi assim que o
# INTC virou CAIXA com o calendário de balanço correto na tela, mas fora do _decide.
#
#   TIER 0  separação de frame     — o frame MAIOR decide a TESE, o menor o TIMING
#   TIER 1  veto                   — fraqueza estrutural bloqueia tudo (já existia)
#   TIER 2  sobreposição           — reenquadra a leitura crua de preço (liquidação,
#                                    porta tese>timing, divergência)
#   TIER 3  modificador de tamanho — muda o PESO, nunca a direção (balanço, âncora)
#   TIER 4  desempate              — adiado (ranking cross-asset, gap, sazonalidade)
#
# Regra dura de ausência: todo fator é CONSULTADO em toda decisão; faltando dado, a
# decisão declara AUSENTE e nunca decide como se o fator fosse neutro.
_TESE_FRAMES = ("1w", "1d")   # o mensal (1mo) não existe no price_structure — D4

# Janela de "balanço na janela". PROVISÓRIA e declarada: o corpus do Erick é
# qualitativo ("vai divulgar só em 22 de outubro" × "reduziria agora") e não dá
# número, então a spec marca este limiar como `a calibrar por backtest` (§8). Fica
# explícito aqui E escrito no traço — auditável, não embutido.
_EARNINGS_WINDOW_DAYS = 21
_EARNINGS_WINDOW_NOTE = (
    f"janela provisória de {_EARNINGS_WINDOW_DAYS} dias — a calibrar por backtest"
)

# "Queda desacelerando" ([10:54] "a queda tá bem enfraquecida") — a 3ª condição da
# porta TIER 2. PROVISÓRIO e declarado, mesmo rigor da janela de balanço: compara a
# magnitude da queda das últimas _DECEL_BARS barras com as _DECEL_BARS anteriores.
# Queda que encolhe = desacelerando; que cresce ou vira alta = não.
_DECEL_BARS = 5
_DECEL_NOTE = (
    f"queda das últimas {_DECEL_BARS} barras menor que a das {_DECEL_BARS} anteriores "
    "— a calibrar por backtest"
)

# Escada do peso-de-posição, do menor pro maior (o TIER 3 só anda nela).
_PESO_ORDEM = ("caixa", "posição inicial", "meia posição", "posição cheia")


def _days_ahead(ev: dict | None, curr_date: str) -> int | None:
    """Dias até o balanço. Usa ``days_ahead`` quando o payload tem (L1); senão
    recalcula da data — entradas antigas do cache não têm o campo."""
    if not isinstance(ev, dict):
        return None
    val = ev.get("days_ahead")
    if isinstance(val, int):
        return val
    try:
        from datetime import date

        d = date.fromisoformat(str(ev.get("date"))[:10])
        base = date.fromisoformat(str(curr_date)[:10])
        return (d - base).days
    except (TypeError, ValueError):
        return None


def _earnings_read(symbol: str, curr_date: str) -> dict:
    """TIER 3 — o calendário de balanço CONSULTADO pela decisão.

    Hoje o dado existe e é anexado como PROSA depois da seção
    (``ensure_earnings_coverage``), nunca lido pelo motor — foi exatamente assim que o
    INTC decidiu CAIXA com a data de 22/10 correta na tela.

    O tri-estado da fonte é preservado, porque as duas causas exigem leitura OPOSTA:
    *sem agenda publicada* é INFORMAÇÃO (não há risco de evento conhecido, e o método
    lê isso como positivo pra montar), enquanto *fonte fora do ar* é IGNORÂNCIA (pode
    ter balanço amanhã). Por isso a fonte caída devolve ``na_janela=None`` e JAMAIS
    ``False``: "não medido" não é "sem risco".
    """
    try:
        from tradingagents.dataflows.earnings_calendar import (
            STATUS_OK,
            STATUS_SEM_AGENDA,
            get_next_earnings_status,
        )

        ev, status = get_next_earnings_status(symbol, curr_date)
    except Exception as exc:  # noqa: BLE001 — calendário ausente nunca derruba a run
        logger.info("calendário de balanço indisponível para %s: %s", symbol, exc)
        ev, status = None, "fonte_indisponivel"

    out: dict = {"status": status, "ev": ev, "dias": None, "na_janela": None,
                 "ausente": None, "leitura": None}

    if status == STATUS_SEM_AGENDA:
        out["na_janela"] = False
        out["leitura"] = "sem data de balanço publicada — sem risco de evento conhecido"
        return out
    if status != STATUS_OK or not isinstance(ev, dict):
        out["ausente"] = ("calendário de balanço — fonte indisponível: risco de evento "
                          "NÃO MEDIDO (não é 'sem risco')")
        out["leitura"] = "risco de evento NÃO MEDIDO — fonte do calendário fora do ar"
        return out

    quando = str(ev.get("date") or "")[:10]
    if ev.get("is_today"):
        out["dias"], out["na_janela"] = 0, True
        out["leitura"] = f"balanço HOJE ({quando}) — risco de evento no máximo"
        return out

    dias = _days_ahead(ev, curr_date)
    out["dias"] = dias
    if dias is None:
        out["ausente"] = "calendário de balanço — data presente mas sem dias calculáveis"
        out["leitura"] = "data de balanço sem dias calculáveis"
        return out

    out["na_janela"] = dias <= _EARNINGS_WINDOW_DAYS
    if out["na_janela"]:
        out["leitura"] = (f"balanço em {dias} dia(s) ({quando}) — DENTRO da janela "
                          f"[{_EARNINGS_WINDOW_NOTE}]")
    else:
        out["leitura"] = (f"sem balanço até {quando} ({dias} dias) — fora da janela "
                          f"[{_EARNINGS_WINDOW_NOTE}]")
    return out


def _drop_decelerating(chart: dict) -> dict:
    """Condição 3 da porta TIER 2 — a queda tá DESACELERANDO? ([10:54] "bem enfraquecida")

    Compara a variação agregada das últimas ``_DECEL_BARS`` barras contra as
    ``_DECEL_BARS`` anteriores, no frame do ``chart``: queda recente MENOR em
    magnitude que a anterior = desacelerando (o vendedor está perdendo força).
    ``None`` quando a série é curta — a condição sai NÃO MEDIDA e a porta não
    abre (fail-closed), nunca decidida no escuro.
    """
    candles = (chart or {}).get("candles") or []
    closes = [c.get("c") for c in candles if c.get("c") is not None]
    need = _DECEL_BARS * 2 + 1
    if len(closes) < need:
        return {"decelerando": None, "detail": f"série curta: {len(closes)} closes < {need} necessários"}
    recent = closes[-1] - closes[-1 - _DECEL_BARS]
    prior = closes[-1 - _DECEL_BARS] - closes[-1 - _DECEL_BARS * 2]
    if prior >= 0:  # não havia queda antes — "desacelerar" não se aplica
        return {"decelerando": False,
                "detail": f"sem queda prévia no frame ({prior:+.2f} nas {_DECEL_BARS*2} barras anteriores)"}
    decel = recent > prior
    return {"decelerando": decel,
            "detail": (f"queda recente {recent:+.2f} vs anterior {prior:+.2f} nas últimas "
                       f"{_DECEL_BARS} barras [{_DECEL_NOTE}]")}


def _tese_read(symbol: str, curr_date: str) -> dict:
    """TIER 0 — a TESE, lida no frame MAIOR (semanal, depois diário).

    O frame maior responde "sou comprador deste ativo?"; o menor (4h/15m) responde
    "compro agora?". Fundir os dois é o que fazia um downtrend de 4h virar CAIXA de
    tese. O mensal não existe na fundação (D4) e por isso sai declarado AUSENTE —
    nunca substituído pelo 4h.

    A divergência de RSI sai do MESMO candle já buscado: o TIER 2 é frame-scoped e
    herda a separação do TIER 0 (divergência no 4h ≠ no semanal), então um fetch por
    frame entrega os dois fatos — tese e divergência — sem buscar a série duas vezes.
    """
    out = {
        "regime": None, "frame": None, "leituras": {}, "divergencias": {},
        "ausentes": ["mensal (1mo) — frame não existe na fundação"],
    }
    for tf in _TESE_FRAMES:
        try:
            ch = build_price_chart(symbol, curr_date, timeframe=tf)
            r = _ema_read(ch)
        except Exception as exc:  # noqa: BLE001 — tese ausente nunca derruba a run
            logger.info("tese frame %s indisponível para %s: %s", tf, symbol, exc)
            ch, r = None, None
        if r is None:
            out["ausentes"].append(f"{tf} — sem candle para a leitura de tese")
            continue
        out["leituras"][tf] = r["trend"]
        div = _rsi_divergence(ch)
        out["divergencias"][tf] = div
        if not div["measured"]:
            out["ausentes"].append(f"divergência de RSI no {tf} NÃO MEDIDA — {div['detail']}")
        if out["regime"] is None:          # o MAIOR disponível manda (semanal > diário)
            out["regime"], out["frame"] = r["trend"], tf
    return out


def _factors(
    symbol: str, curr_date: str, chart: dict, drop: dict | None
) -> dict:
    """A CAMADA DE PONDERAÇÃO consultada por TODA decisão (spec §3).

    Cada fator é lido UMA vez aqui — tese (TIER 0), calendário de balanço (TIER 3),
    divergência do frame de swing (TIER 2) e âncora do setor. A âncora é REUSADA do
    ``drop`` (``classify_drop_nature`` já calcula ``evidence.anchor`` — mesmo fetch,
    fonte única), nunca re-buscada. Faltando dado, o fator declara AUSENTE na lista
    consolidada — a decisão nunca o trata como neutro.

    ``chart`` é o chart do frame de swing JÁ buscado por ``build_erick_method_section``
    — divergência e desaceleração saem dele, sem fetch novo.
    """
    tese = _tese_read(symbol, curr_date)
    earnings = _earnings_read(symbol, curr_date)
    divergencia = _rsi_divergence(chart)
    decel = _drop_decelerating(chart)

    ausentes = list(tese["ausentes"])
    if not divergencia["measured"]:
        ausentes.append(f"divergência de RSI no frame de swing NÃO MEDIDA — {divergencia['detail']}")
    if decel["decelerando"] is None:
        ausentes.append(f"queda desacelerando NÃO MEDIDA — {decel['detail']}")
    if earnings["ausente"]:
        ausentes.append(earnings["ausente"])

    ancora = ((drop or {}).get("evidence") or {}).get("anchor") or {}
    if not ancora:
        ausentes.append("âncora do setor — não calculada nesta run")

    return {
        "tese": tese,
        "earnings": earnings,
        "divergencia": divergencia,
        "decel": decel,
        "ancora": {
            "nome": ancora.get("name"),
            "em_alta": bool(ancora.get("trend") == "alta"),
            "bateu_balanco": bool(ancora.get("beat_recent")),
        } if ancora else None,
        "ausentes": ausentes,
    }


def _rsi_series(closes: list, period: int = 14) -> list:
    """RSI de Wilder sobre os fechamentos do frame. ``None`` nas barras sem janela
    cheia — nada é extrapolado para preencher o começo da série."""
    out: list = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / period, losses / period
    def rsi(ag_, al_):
        if al_ == 0:
            return 100.0
        rs = ag_ / al_
        return 100.0 - (100.0 / (1.0 + rs))
    out[period] = rsi(ag, al)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0.0)) / period
        al = (al * (period - 1) + max(-d, 0.0)) / period
        out[i] = rsi(ag, al)
    return out


_DIV_SWING_K = 3   # look-around do swing de divergência (mesma ideia do price_structure)


def _swing_points(vals: list, k: int = _DIV_SWING_K) -> tuple[list, list]:
    """Índices de máximos e mínimos locais confirmados (k barras de cada lado)."""
    highs, lows = [], []
    for i in range(k, len(vals) - k):
        win = vals[i - k:i + k + 1]
        if any(v is None for v in win):
            continue
        if vals[i] == max(win) and win.count(vals[i]) == 1:
            highs.append(i)
        if vals[i] == min(win) and win.count(vals[i]) == 1:
            lows.append(i)
    return highs, lows


def _rsi_divergence(chart: dict) -> dict:
    """TIER 2 — divergência de RSI no frame do ``chart`` (indicador nº2 do método).

    * *bearish* — preço faz topo MAIS ALTO e o RSI faz topo MAIS BAIXO: a alta perdeu
      força (topo local), mesmo com o preço subindo.
    * *bullish* — preço faz fundo MAIS BAIXO e o RSI faz fundo MAIS ALTO: fundo se
      formando, mesmo com o preço caindo.

    Sem os dois swings ou sem RSI cheio devolve ``measured=False`` — a decisão diz
    "divergência NÃO MEDIDA" e JAMAIS afirma "sem divergência" (o protocolo de
    ausência da spec §3). Poder de direção fica DESLIGADO por ora: a spec marca a
    precedência divergência × earnings como `a validar` (§8), então aqui ela entra no
    traço, bloqueia a porta do TIER 2 e limita o tamanho — nunca inverte sozinha um
    veredito que a mecânica de hoje já dá."""
    candles = (chart or {}).get("candles") or []
    closes = [c.get("c") for c in candles]
    if len(closes) < 40 or any(c is None for c in closes[-40:]):
        return {"measured": False, "kind": None, "detail": "série curta demais para RSI"}
    rsi = _rsi_series(closes)
    highs, lows = _swing_points(closes)
    def two(idxs):
        return idxs[-2:] if len(idxs) >= 2 else None
    hh, ll = two(highs), two(lows)
    if hh:
        a, b = hh
        if rsi[a] is not None and rsi[b] is not None and closes[b] > closes[a] and rsi[b] < rsi[a]:
            return {"measured": True, "kind": "bearish",
                    "detail": f"topo do preço subiu ({_fmt(closes[a])}→{_fmt(closes[b])}) "
                              f"e o do RSI caiu ({rsi[a]:.0f}→{rsi[b]:.0f})"}
    if ll:
        a, b = ll
        if rsi[a] is not None and rsi[b] is not None and closes[b] < closes[a] and rsi[b] > rsi[a]:
            return {"measured": True, "kind": "bullish",
                    "detail": f"fundo do preço caiu ({_fmt(closes[a])}→{_fmt(closes[b])}) "
                              f"e o do RSI subiu ({rsi[a]:.0f}→{rsi[b]:.0f})"}
    if hh or ll:
        return {"measured": True, "kind": None, "detail": "preço e RSI apontam para o mesmo lado"}
    return {"measured": False, "kind": None, "detail": "sem dois swings confirmados na janela"}


def _liq_entry_ref(drop: dict | None) -> str:
    """Referência de ENTRADA numa liquidação: a média DIÁRIA que sobe — a MESMA que
    classificou o recuo (``active_rising_label`` da evidência) — NUNCA a EMA 4h
    invertida. Numa liquidação as EMAs curtas do 4h invertem por construção: são
    RESISTÊNCIA, o SINTOMA da queda, não o eixo da compra. Fail-open → texto genérico
    da média diária (jamais um nível de EMA 4h inventado)."""
    asset = ((drop or {}).get("evidence") or {}).get("asset") or {}
    label = asset.get("active_rising_label")
    val = None
    if label and "200" in str(label):
        val = asset.get("ma200")
    elif label and "50" in str(label):
        val = asset.get("ma50")
    if label and val is not None:
        return f"{label} diária ({_fmt(val)})"
    if label:
        return f"{label} diária"
    return "média diária que sobe"


def _gate_abre(r: dict, drop_cls: str | None, factors: dict | None) -> bool:
    """A PORTA TIER 2 do INTC (spec §2): downtrend de frame menor deixa de ser CAIXA
    de tese quando as CINCO condições citadas estão presentes.

    1. frame menor em ``baixa``                    — o próprio ``r`` (leitura de EMA)
    2. TESE do frame maior em alta                 — TIER 0 (``_tese_read``)
    3. queda desacelerando                         — ``_drop_decelerating``
    4. sem balanço na janela                       — ``_earnings_read``
    5. âncora do setor em alta                     — reusada do ``drop_nature``

    FAIL-CLOSED: qualquer condição ausente/None/não medida → porta FECHADA e o motor
    cai no CAIXA seguro de hoje. A fraqueza estrutural (TIER 1) é consultada antes e
    veta tudo — a porta nunca abre sobre estrutura rompida. Divergência bearish no
    frame da tese também fecha a porta (a alta do frame maior perdeu força).
    """
    if factors is None:
        return False
    if drop_cls == "fraqueza":
        return False
    if r.get("trend") != "baixa":
        return False

    tese = factors.get("tese") or {}
    decel = factors.get("decel") or {}
    earnings = factors.get("earnings") or {}
    ancora = factors.get("ancora") or {}

    if tese.get("regime") != "alta":
        return False
    if decel.get("decelerando") is not True:
        return False
    if earnings.get("na_janela") is not False:      # None (fonte caída) NÃO abre
        return False
    if not ancora.get("em_alta"):
        return False

    # Guarda: divergência bearish no frame da TESE tampa a alta do frame maior.
    frame_tese = tese.get("frame")
    div_tese = (tese.get("divergencias") or {}).get(frame_tese) or {}
    return not (div_tese.get("measured") and div_tese.get("kind") == "bearish")


def _decide(
    r: dict, drop: dict | None = None, fine_veto: bool = False,
    factors: dict | None = None,
) -> dict:
    """Do read de EMA para o veredito do método: agir/aguardar, entrada e PESO.

    ``drop`` é o dict inteiro da natureza da queda (fonte única): ``_decide`` extrai a
    ``classification`` e, na liquidação, a REFERÊNCIA de entrada (a média diária que
    sobe) via :func:`_liq_entry_ref`. A natureza manda ANTES da pilha de médias:
    ``fraqueza`` zera a entrada (caixa) e ``liquidacao_saudavel`` numa baixa/transição
    lê o recuo à média DIÁRIA que sobe como comprável (posição inicial no toque,
    aguardar o toque senão). ``fine_veto`` (1-2-3 de venda no 15m acionado) trava a
    promoção da liquidação além de AGUARDAR — o recuo virou ruptura. ``drop=None`` e
    ``fine_veto=False`` (defaults) reproduzem a mecânica de hoje BYTE-A-BYTE.

    ``factors`` é a camada de ponderação (:func:`_factors`). A PORTA TIER 2 liga aqui:
    com o frame menor em baixa, tese do frame maior em alta e as demais condições
    citadas presentes, o downtrend vira TIMING (AGUARDAR/posição inicial) em vez de
    rejeição (CAIXA) — o fix do INTC. ``factors=None`` (default) mantém a mecânica de
    hoje BYTE-A-BYTE: sem a camada, nada muda.

    Peso relativo sempre FRACIONADO (o método nunca entra 100% de uma vez) e
    responde em termos relativos — cheia / meia / inicial / caixa — não em %
    absoluto inventado.
    """
    trend, at_media, extended, below = r["trend"], r["at_media"], r["extended"], r["below"]
    e8, e21 = r["e8"], r["e21"]
    drop_cls = (drop or {}).get("classification")

    # Natureza da queda ANTES das médias (fonte única). Nunca rebaixa uma leitura de
    # alta já válida — só reenquadra a baixa/transição da liquidação e veta a fraqueza.
    if drop_cls == "fraqueza":
        return {
            "acao": "AGUARDAR",
            "entrada": f"estrutura rompida (fraqueza) — sem entrada; caixa até um novo setup à média (EMA 21 {_fmt(e21)})",
            "peso": "caixa",
            "peso_racional": "filtro do método: estrutura rompida, sem entrada contra o gráfico quebrado — caixa é a posição",
        }
    if drop_cls == "liquidacao_saudavel" and trend in ("baixa", "transicao"):
        ref = _liq_entry_ref(drop)
        if fine_veto:
            return {
                "acao": "AGUARDAR",
                "entrada": f"recuo comprável na {ref}, mas o 15m tem 1-2-3 de venda ACIONADO — aguardar o gatilho virar",
                "peso": "caixa",
                "peso_racional": "1-2-3 de venda no 15m — o recuo virou ruptura; "
                                 "a liquidação não promove a entrada além de aguardar",
            }
        if at_media:
            return {
                "acao": "AGIR",
                "entrada": f"queda é liquidação de longs — segue comprador no recuo à {ref}; a EMA 4h "
                           f"invertida (EMA 21 {_fmt(e21)}) é o sintoma da liquidação, não o eixo da entrada",
                "peso": "posição inicial",
                "peso_racional": "recuo comprável numa liquidação saudável: fração inicial na média diária "
                                 "que sobe, com espaço para somar — nunca 100% de uma vez",
            }
        return {
            "acao": "AGUARDAR",
            "entrada": f"liquidação de longs — aguardar o toque na {ref} antes de montar (recuo comprável, ainda sem toque)",
            "peso": "caixa",
            "peso_racional": "recuo comprável, mas o preço ainda não tocou a média — caixa até o ponto de entrada",
        }

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

    # baixa — TIER 0 separa TESE de TIMING antes de rejeitar: com a tese do frame
    # maior em alta e as 5 condições da porta TIER 2 presentes, o downtrend de 4h é
    # TIMING (espera o ponto), não CAIXA-tese (rejeição). Faltando qualquer condição,
    # a porta não abre e vale o filtro de hoje: caixa contra médias invertidas.
    if _gate_abre(r, drop_cls, factors):
        tese = factors["tese"]
        earnings = factors["earnings"]
        ancora = factors.get("ancora") or {}
        motivos = [
            f"tese de alta no frame maior ({_FRAME_LABEL.get(tese['frame'], tese['frame'])})",
            f"queda desacelerando [{_DECEL_NOTE.split(' — ')[0]}]",
            earnings.get("leitura") or "sem balanço na janela",
        ]
        if ancora.get("nome"):
            motivos.append(f"âncora {ancora['nome']} em alta")
        return {
            "acao": "AGUARDAR",
            "entrada": (f"downtrend do frame menor rebaixado a TIMING — tese de alta no "
                        f"frame maior sustenta a montagem: {' · '.join(motivos)}; "
                        f"entrada no recuo à EMA 21 ({_fmt(e21)}) com a pilha virando"),
            "peso": "posição inicial",
            "peso_racional": ("porta TIER 2 (5 condições citadas): o 4h em baixa não veta a "
                              "tese do frame maior — começar a montar, fração inicial"),
        }

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


def _drop_nature(symbol: str, curr_date: str, asset_type: str) -> dict | None:
    """Classifica a natureza da queda ANTES da decisão (fonte única do módulo).
    Fail-open → None. Seam isolado para monkeypatch nos testes."""
    try:
        from tradingagents.agents.utils.drop_nature import classify_drop_nature_safe

        return classify_drop_nature_safe(symbol, curr_date, asset_type)
    except Exception as exc:  # noqa: BLE001 — enriquecimento nunca quebra o relatório
        logger.info("drop-nature classify failed for %s: %s", symbol, exc)
        return None


def _render_drop_nature(res: dict | None, estado: str | None) -> str | None:
    """Texto da 'natureza da queda' a partir da classificação JÁ FEITA (DEPOIS que o
    Estado já derivou dela). Sem re-leitura. Fail-open → None. Seam de monkeypatch."""
    try:
        from tradingagents.agents.utils.drop_nature import render_drop_nature_line

        return render_drop_nature_line(res, estado)
    except Exception as exc:  # noqa: BLE001 — enriquecimento nunca quebra o relatório
        logger.info("drop-nature render failed: %s", exc)
        return None


def _fine_plan(symbol: str, curr_date: str) -> dict | None:
    """Plano 15m computado UMA vez — o seam de dado que alimenta TANTO o veto (o 1-2-3
    de venda) QUANTO o render do timing fino. Fail-open → None quando não há candle."""
    try:
        return build_actionable_plan_dict(symbol, curr_date, _FINE_FRAME)
    except Exception:  # noqa: BLE001
        return None


def _fine_sell_triggered(plan: dict | None) -> bool:
    """True quando o plano 15m tem um 1-2-3 de VENDA acionado — o sinal mais rápido de
    que o recuo virou ruptura (mitigação 1 do caveat). Fail-open → False: na dúvida NÃO
    veta (não inventa um rompimento que a fonte não confirma)."""
    pat = (plan or {}).get("pattern") or {}
    return pat.get("direction") == "venda" and pat.get("state") == "acionado"


_PAT_DIR_PT = {"compra": "de compra", "venda": "de venda"}
_PAT_STATE_PT = {
    "acionado": "acionado",
    "rompeu_retracou": "rompeu e retraçou (não confirmado)",
    "formando": "em formação",
}


def _pattern_line(plan: dict | None, frame_label: str) -> str | None:
    """Gatilho 1-2-3 do frame (o outro pilar do método além do recuo à EMA 8/21),
    a partir do plano já computado. ``None`` quando não há padrão detectado."""
    pat = (plan or {}).get("pattern")
    if not pat:
        return None
    direction = _PAT_DIR_PT.get(pat.get("direction"), pat.get("direction") or "")
    state = _PAT_STATE_PT.get(pat.get("state"), pat.get("state") or "")
    trigger = pat.get("trigger")
    verb = "perda de" if pat.get("direction") == "venda" else "rompimento de"
    trig_txt = f"{verb} {_fmt(trigger)}" if trigger is not None else "gatilho sem nível"
    return f"**Gatilho 1-2-3 {direction} ({frame_label}):** {trig_txt} — {state}."


def _levels_line(plan: dict | None) -> str | None:
    """Onde INVALIDA, stop, alvo e R:R do 1-2-3 — numa linha, a partir do plano já
    computado. É exatamente o que o método prega: o stop não é percentual, é a
    perda de estrutura (com folga de ATR declarada). ``None`` sem padrão; cada
    item sem base sai como "sem nível", nunca um número inventado."""
    plan = plan or {}
    if not plan.get("pattern"):
        return None
    inval, stop = plan.get("invalidation"), plan.get("stop")
    target, rr = plan.get("target"), plan.get("risk_reward")
    bits = [
        f"invalida em {_fmt(inval['price'])} ({inval.get('label') or 'estrutura'})"
        if inval and inval.get("price") is not None else "invalidação sem nível",
        f"stop {_fmt(stop['price'])} ({stop.get('basis') or 'estrutura'})"
        if stop and stop.get("price") is not None else "stop sem nível",
        f"alvo {_fmt(target['price'])} ({target.get('label') or ''})".strip()
        if target and target.get("price") is not None else "alvo sem nível",
    ]
    if rr and rr.get("rr") is not None:
        bits.append(f"R:R {rr['rr']:.2f}:1 (entrada {_fmt(rr.get('entry'))} — {rr.get('entry_basis')})")
    elif rr and rr.get("note"):
        bits.append(f"R:R não calculável — {rr['note'].rstrip('.')}")
    else:
        bits.append("R:R sem base")
    return "**Stop / alvo do 1-2-3:** " + " · ".join(bits) + "."


def _fine_timing(plan: dict | None) -> str | None:
    """Linha de timing fino no 15m a partir do plano JÁ computado (:func:`_fine_plan`)
    — render PURO, sem rebuild (o mesmo plano alimenta o veto): estado do setup +
    gatilho 1-2-3 do 15m, o timing do método. Fail-open → None quando não há candle."""
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
    if not txt:
        return None
    line = f"**Timing fino (15m):** {txt}"
    pat_line = _pattern_line(plan, "15m")
    if pat_line:
        line += "\n" + pat_line
    return line


def _estado_note(
    drop_cls: str | None, estado: str, fine_veto: bool = False, gate: bool = False,
    gate_frame: str | None = None,
) -> str:
    """Nota curta que explica a DERIVAÇÃO do Estado — QUAL fator comandou e o que ele
    sobrepôs — pra ler de cima pra baixo dar a mesma conclusão do enum. A porta TIER 2
    aberta nomeia a tese do frame maior como o fator que rebaixou o downtrend do frame
    menor a timing."""
    if drop_cls == "fraqueza":
        return ("Deriva da natureza da queda (fraqueza: estrutura rompida) — "
                "não da pilha curta de médias.")
    if drop_cls == "liquidacao_saudavel" and estado in ("AGIR", "AGUARDAR"):
        note = ("Deriva da natureza da queda (liquidação saudável: recuo comprável) — "
                "não da pilha curta de médias.")
        if fine_veto:
            note += (" Veto do 15m ativo: 1-2-3 de venda acionado trava a promoção "
                     "além de AGUARDAR.")
        return note
    if gate and estado == "AGUARDAR":
        tf = _FRAME_LABEL.get(gate_frame or "", gate_frame or "")
        return (f"Deriva da TESE de alta do frame maior ({tf}) sobrepondo o downtrend "
                "do frame menor (rebaixado a TIMING) — não da pilha curta de médias.")
    return ""


def build_erick_method_section(
    symbol: str, curr_date: str, asset_type: str, drop: dict | None = None
) -> str:
    """Seção markdown pt-BR do método Erick — determinística e ancorada em dado.

    O método lê no 4h (frame de swing) + timing fino no 15m para QUALQUER ativo —
    cripto no candle da exchange, ação no intradiário keyless do yfinance. Se a
    fonte não tem candle pro símbolo/data, cai no diário e declara o degradê. Nunca
    inventa nível: sem candle, diz que a leitura intradiária está indisponível.

    ``drop`` é a classificação da natureza da queda JÁ FEITA pelo analista (fonte
    única, compartilhada com o juiz). ``None`` → classifica aqui uma vez.
    """
    frame = _SWING_FRAME
    chart = build_price_chart(symbol, curr_date, timeframe=frame)
    read = _ema_read(chart)

    # Fonte intradiária sem candle pro símbolo/data (ação fora da janela do yfinance,
    # ou feed cripto fora do ar): cai no diário, declarando o degradê.
    degraded_note = ""
    if read is None:
        frame = _FALLBACK_FRAME
        chart = build_price_chart(symbol, curr_date, timeframe=frame)
        read = _ema_read(chart)
        degraded_note = ("\n\n> Fonte intradiária indisponível agora — leitura caiu no "
                         "diário. Nenhuma barra inventada; o método pede o 4h/15m.")

    frame_label = _FRAME_LABEL.get(frame, frame)
    head = "## 🧭 Método Erick — leitura do setup"

    if read is None:
        return (
            f"{head}\n\n"
            f"**Timeframe da leitura:** {frame_label}\n\n"
            f"Sem candle suficiente para a leitura de EMA neste frame — nada inventado."
        )

    # Natureza da queda classificada ANTES da decisão (fonte única). Se o analista
    # não passou a classificação, classifica aqui — uma vez.
    if drop is None:
        drop = _drop_nature(symbol, curr_date, asset_type)
    drop_cls = (drop or {}).get("classification")

    # Plano 15m computado UMA vez — alimenta o veto (1-2-3 de venda) E o timing fino.
    fine_plan = _fine_plan(symbol, curr_date)
    # Mitigação 1: 1-2-3 de venda no 15m veta a promoção da liquidação além de AGUARDAR
    # (o recuo virou ruptura). Fail-open → não veta. Threaded em _decide (não pós-fato).
    fine_veto = drop_cls == "liquidacao_saudavel" and _fine_sell_triggered(fine_plan)
    # A camada de ponderação, consultada UMA vez e entregue aos DOIS pontos que
    # decidem (_decide E _estado) — nunca calculada de novo em cada um.
    factors = _factors(symbol, curr_date, chart, drop)
    gate = _gate_abre(read, drop_cls, factors)
    decision = _decide(read, drop, fine_veto, factors)
    # ONE canonical state (item 6b), agora derivado da natureza da queda E da porta
    # TIER 2 — o gate espelha a decisão pra Estado e Peso nunca se contradizerem.
    decision["estado"] = _estado(decision["acao"], read["trend"], drop_cls, gate)

    # Mitigação 2: LOG da taxa de flip — quando a liquidação promove o Estado que a
    # mecânica leria como CAIXA (medir num backtest se o gate filtra algo). A porta
    # TIER 2 vira o MESMO medidor: quantas vezes a tese do frame maior salvou um CAIXA.
    mech = _decide(read, None)
    mech_estado = _estado(mech["acao"], read["trend"], None)
    if drop_cls == "liquidacao_saudavel" and mech_estado == "CAIXA" and decision["estado"] != "CAIXA":
        logger.info(
            "erick-drop-flip %s %s: estado %s->%s por liquidacao_saudavel (veto15m=%s)",
            symbol, curr_date, mech_estado, decision["estado"], fine_veto,
        )
    if gate and mech_estado == "CAIXA":
        logger.info(
            "erick-gate-flip %s %s: estado CAIXA->%s por porta TIER 2 (tese %s/%s)",
            symbol, curr_date, decision["estado"],
            factors["tese"]["regime"], factors["tese"]["frame"],
        )

    swing_plan = build_actionable_plan_dict(symbol, curr_date, frame)
    saida = _saida(swing_plan, read)
    trend_pt = _TREND_PT.get(read["trend"], read["trend"])
    caixa = decision["estado"] == "CAIXA"
    # Texto da natureza da queda a partir da classificação já feita (sem re-leitura):
    # o Estado já veio dela, o texto só explica de onde. Vem ANTES do Estado abaixo.
    drop_line = _render_drop_nature(drop, decision["estado"])
    estado_note = _estado_note(drop_cls, decision["estado"], fine_veto, gate,
                               (factors.get("tese") or {}).get("frame"))
    estado_txt = ("**Estado (Método Erick):** "
                  f"{decision['estado']} — estado único do método neste run; a "
                  "leitura abaixo deriva dele (sem veredito paralelo).")
    if estado_note:
        estado_txt += " " + estado_note

    lines = [
        head,
        "",
        f"**Timeframe da leitura:** {frame_label} — o método opera no 15m/4h; "
        "diário/semanal dão a tendência de fundo.",
        "",
        f"**Regime (médias):** {trend_pt} — preço {_fmt(read['close'])}, "
        f"EMA 8 {_fmt(read['e8'])} · EMA 21 {_fmt(read['e21'])} · EMA 50 {_fmt(read['e50'])}.",
    ]
    # 🩸 Natureza da queda ANTES do Estado: ler de cima pra baixo dá a MESMA conclusão
    # que o enum (a classificação é a fonte de onde o Estado veio, não uma re-leitura
    # anexada no fim que o contradiz).
    if drop_line:
        lines += ["", drop_line]
    # A porta TIER 2 aberta é FATO DECISIVO — vem logo antes do Estado, como a
    # natureza da queda: quem lê de cima pra baixo chega no enum já sabendo por quê.
    if gate:
        tese = factors["tese"]
        lines += ["",
                  f"**🚪 Porta TIER 2 aberta:** tese de alta no frame maior "
                  f"({_FRAME_LABEL.get(tese['frame'], tese['frame'])}) sobrepondo o "
                  f"downtrend do {frame_label} — rebaixado a TIMING, não veto de tese."]
    lines += [
        "",
        estado_txt,
        f"**Entrada (recuo à média):** {decision['entrada']}.",
        f"**Saída (antes da reversão):** {saida}.",
        f"**Peso relativo do trade:** {decision['peso']} — {decision['peso_racional']}.",
    ]

    # Gatilho 1-2-3 do frame de swing (o outro pilar do método além do recuo à EMA
    # 8/21), agora DENTRO da leitura do método — não só na seção de mercado.
    swing_pat = _pattern_line(swing_plan, _COMPACT_FRAME.get(frame, frame))
    if swing_pat:
        lines.append(swing_pat)
        # O padrão sem os níveis é meia informação: quem lê precisa saber onde o
        # setup morre, onde fica o stop e quanto se arrisca para ganhar quanto.
        lvl = _levels_line(swing_plan)
        if lvl:
            lines.append(lvl)

    fine = _fine_timing(fine_plan)
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

    # Consulta obrigatória (spec §3): tudo que a decisão NÃO conseguiu medir sai
    # declarado — o leitor sabe o que a decisão não ouviu, em vez de presumir neutro.
    if factors["ausentes"]:
        lines += ["",
                  "**Não medido nesta run** (declarado ausente — não tratado como neutro):"]
        lines += [f"- {a}" for a in factors["ausentes"]]

    return "\n".join(lines) + degraded_note


def ensure_erick_method_coverage(
    report: str, symbol: str, curr_date: str, asset_type: str, drop: dict | None = None
) -> str:
    """Anexa a seção determinística do método ao relatório do analista `erick`.

    Espelha os outros guardas de cobertura (multi-timeframe, derivativos,
    price-structure): a prosa do LLM molda; esta seção garante o núcleo
    operável (timeframe, recuo à média, saída, peso). ``drop`` propaga a
    classificação já feita (fonte única). Fail-open: qualquer erro devolve o
    relatório intacto.
    """
    try:
        section = build_erick_method_section(symbol, curr_date, asset_type, drop=drop)
    except Exception as exc:  # noqa: BLE001 — enriquecimento nunca quebra o relatório
        logger.warning("erick-method coverage failed for %s: %s", symbol, exc)
        return report
    base = (report or "").rstrip()
    return f"{base}\n\n{section}\n" if base else section + "\n"

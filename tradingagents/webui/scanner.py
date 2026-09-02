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
    build_storm_plan_dict,
)
from tradingagents.webui import timeutil

logger = logging.getLogger(__name__)

# Frames do scan (DA-138): o contexto (1w), o swing (1d), o posicionamento (4h) e o
# fino (1h). Decisão do Samyr: *"a cada quinze minutos vamos ter muito ruído, vamos
# mudar pra a cada uma hora, [varrer] uma hora, quatro horas, diário e semanal"*.
#
# O 15m fica de fora por RUÍDO, não por limitação de capacidade — registre-se assim,
# porque a diferença importa no dia em que alguém quiser reabrir a decisão: varrer o
# 15m é tecnicamente possível (o loader intradiário o atende), e o que o desaconselha
# é o que ele faria com a leitura, não o custo.
#
# O SEMANAL É QUASE DE GRAÇA, e isto foi MEDIDO: ele é REAMOSTRADO da mesma série
# diária que o frame `1d` já carrega (`_serie_do_frame` → `load_ohlcv` →
# `_resample_weekly`), e `load_ohlcv` guarda por símbolo em disco. Custo por ativo:
# uma leitura de disco a mais e a reamostragem — 3,0 → 4,0 chamadas ao carregador,
# com o tempo de passada INALTERADO (6,5s → 6,4s em 5 ativos, processos frios).
# Rede: zero a mais.
#
# A CADÊNCIA continua saindo daqui sozinha: `agenda.cadencia_minutos` usa o candle
# mais rápido do conjunto, que segue sendo o 1h = 60 minutos.
SCAN_FRAMES = ("1w", "1d", "4h", "1h")

# Distância do preço ao gatilho que caracteriza "em gatilho" (ponto de entrada).
# PROVISÓRIO e declarado: 0,5% absorve o ruído intradiário de um toque iminente.
# A calibrar com o track record do scans.jsonl (mesma disciplina do
# _EARNINGS_WINDOW_NOTE do erick_method).
_GATILHO_TOL = 0.005

# Abaixo deste R:R, num setup JÁ ACIONADO, o que sobra do movimento não paga o
# risco de entrar agora — a tela diz isso com palavra ("alvo praticamente
# alcançado") em vez de exibir "0.00", que se lê como "setup sem retorno".
# ARBITRÁRIO e declarado, como o _GATILHO_TOL: a calibrar com o track record.
_RR_RESIDUAL = 0.05

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
# `concluido` fica com `invalidou`: os dois são trades que não se opera mais. A
# diferença entre eles (ganhou × morreu) está no DESFECHO, não na urgência.
_URGENCIA = {"em_gatilho": 0, "formando": 1, "em_movimento": 2, "invalidou": 3,
             "concluido": 3, "sem_setup": 4, "sem_dado": 5}


def _resto(r: dict) -> float:
    """Quanto AINDA sobra do movimento, em %, pro desempate dentro de em_movimento.

    Um setup acionado que andou 20% do caminho e outro que andou 95% são coisas
    diferentes pra quem vai entrar agora, e a lista os empilhava lado a lado pela
    distância do gatilho — que num acionado mede o quanto ele já FUGIU, ou seja,
    ordenava ao contrário do interesse.

    Sem faixa arbitrária: é a própria medida que ordena. Sem percurso (padrão não
    acionado, ou sem alvo pra medir contra) devolve 100 — nada foi consumido, que é
    a leitura certa pro que ainda não começou.
    """
    v = r.get("sobra_pct")
    return float(v) if v is not None else 100.0


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


# CICLO -> ESTADO DA LINHA, uma tradução só para os DOIS métodos (DA-129).
#
# A régua do desfecho já era compartilhada desde a DA-126, mas só o 1-2-3 tinha
# aprendido a LER o resultado dela: o Storm calculava o ciclo e jogava fora aqui,
# publicando "vetado" para um trade que já tinha terminado. Uma linha de scan que
# consulta o filtro do Éden sobre um padrão ENCERRADO está perguntando "vale a pena
# entrar?" sobre um trade que não existe mais.
#
# Só os estados que o CICLO decide moram aqui. Gatilho, movimento e formação
# dependem da distância do preço, que é de cada método — mas eles só são
# alcançados quando o ciclo não terminou.
_CICLO_ESTADO = {
    "concluido_alvo": "concluido",
    "concluido_stop": "concluido",
    "invalidado_sem_acionar": "invalidou",
    "invalidado_operando": "invalidou",
}


def _estado_do_ciclo(pat: dict[str, Any] | None) -> str | None:
    """O estado da linha quando o CICLO já decide sozinho; ``None`` se ainda é vivo.

    Ponto único: enquanto os dois métodos passarem por aqui, não há como um deles
    publicar "vetado" ou "em movimento" sobre um padrão que virou história.
    """
    ciclo = (pat or {}).get("ciclo")
    return _CICLO_ESTADO.get(ciclo) if ciclo else None


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
        # Sem 1-2-3 não quer dizer sem STORM: são setups diferentes, e o Storm pode
        # estar formado onde este não está (é metade da razão de ele existir aqui).
        return {"frame": frame, "estado": "sem_setup", "price": price,
                "storm": _storm_row(ticker, date, frame, price)}

    trigger = float(pat["trigger"])
    dist = abs(price / trigger - 1.0) if trigger else None
    state = pat.get("state")
    direction = pat.get("direction")
    # Invalidação: preço além do ponto 3 (onde o padrão deixa de existir). Na
    # compra o setup morre ao PERDER o ponto 3; na venda ao VOLTAR acima dele.
    # (antes o scan não tinha esse estado: mostrava um setup morto como vivo.)
    # A MORTE VEM DO DETECTOR, não de uma segunda conta aqui. O padrão carrega
    # `invalidado` medido na barra que FECHOU além do ponto 3 (task 013): um setup que
    # morreu e voltou continua morto, e a conta local — que só olhava o ÚLTIMO preço —
    # o ressuscitava. Duas definições de "invalidado" fariam a lista dizer "em
    # movimento" sobre o mesmo padrão que a análise desenha como fantasma.
    # O fallback local fica pro plano ANTIGO (cache sem o campo), não como regra.
    inval_price = (plan.get("invalidation") or {}).get("price")
    if "invalidado" in pat:
        invalidated = bool(pat.get("invalidado"))
    elif inval_price is not None:
        inval = float(inval_price)
        invalidated = (price > inval) if direction == "venda" else (price < inval)
    else:
        invalidated = False
    # EM GATILHO = preço no ponto de entrada AGORA (≤ tol), independente de o
    # padrão já ter acionado (recém-rompido ainda no ponto ainda entra). Acionado
    # e preço além da entrada → em_movimento (buscando alvo, não é entrada).
    # ENCERRADO manda em tudo (DA-125): um trade que já chegou ao alvo ou ao stop
    # terminou, e nada posterior o reabre. Antes, o LINK-USD saía "invalidou" oito
    # horas DEPOIS de ter atingido o alvo — veredito invertido em relação ao
    # dinheiro. O desfecho vem do plano, que é onde padrão e níveis coexistem.
    if (_do_ciclo := _estado_do_ciclo(pat)) is not None:
        estado = _do_ciclo
    elif invalidated:
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
    # ALVO INCOERENTE NÃO SE PUBLICA. ``_risk_reward`` já detecta o alvo atrás da
    # entrada (ou o stop do lado errado) e devolve ``rr=None`` com o motivo escrito;
    # o scan publicava o ``tp`` assim mesmo, e a tela mostrava "TP 512,76" ao lado de
    # "gatilho 512,76 · R:R não calculável" — número sem sentido, e o MOTIVO,
    # que a tela de análise mostra, era descartado aqui. Pior: esse tp ia pro
    # ScanLog e virava ACERTO FABRICADO — alvo igual ao gatilho é "bateu_tp" no
    # instante em que aciona. Com ``tp=None`` o track record só pode fechar pelo SL
    # (``_primeiro_toque`` já trata), que é a leitura honesta.
    tp_incoerente = bool(rr.get("note")) and rr.get("rr") is None
    # RETORNO RESIDUAL: num setup JÁ ACIONADO a entrada de referência é o PREÇO
    # ATUAL, não o gatilho (``_entry_ref``) — então o R:R mede o que AINDA sobra do
    # trade, não o que ele valia quando nasceu. Aritmeticamente certo e enganoso na
    # tela: MSFT 1h em 29/08 saía "gatilho 497,14 · TP 513,73 · R:R 0.00" com o
    # preço em 513,67, e 0.00 lê-se como "setup sem retorno" quando a verdade é "o
    # trade já andou, sobrou 0,06 pra 28,70 de risco" (do gatilho teria dado 1,36).
    # Mesma família do alvo degenerado: número correto, leitura errada. O flag deixa
    # a TELA dizer isso com palavra em vez de repetir o número cru.
    #
    # 0,05 é o limiar: abaixo disso o que resta do movimento não paga o risco de
    # entrar agora em nenhuma leitura razoável. ARBITRÁRIO e declarado, como o
    # _GATILHO_TOL — a calibrar com o track record.
    rr_val = rr.get("rr")
    rr_residual = bool(
        state == "acionado" and rr_val is not None and 0 <= float(rr_val) < _RR_RESIDUAL
    )
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
        # O DESFECHO viaja na linha (DA-125): sem ele a lista diz "concluído" e não
        # diz se ganhou ou perdeu — que é a única coisa que importa num encerrado.
        "desfecho": pat.get("desfecho"),
        "sl": stop.get("price"),
        "tp": None if tp_incoerente else target.get("price"),
        "tp_faixa": (None if tp_incoerente else
                     ([target.get("low"), target.get("high")]
                      if target.get("low") is not None else None)),
        "rr": rr_val,
        "rr_note": rr.get("note"),
        # A BASE da entrada viaja junto do R:R: sem ela a tela não tem como dizer
        # se o número foi medido do gatilho ou do preço de agora — e são leituras
        # diferentes do mesmo setup.
        "rr_entry": rr.get("entry"),
        "rr_basis": rr.get("entry_basis"),
        "rr_risco": rr.get("risk"),
        "rr_retorno": rr.get("reward"),
        "rr_residual": rr_residual,
        # PERCURSO — quanto do caminho gatilho→alvo o preço já andou, e o R:R que o
        # setup oferecia NO GATILHO. É o que separa "o método dá trade ruim" de
        # "cheguei tarde": um R:R 0,09 num setup que andou 91% do caminho não é um
        # setup ruim, é um setup ESGOTADO, e a lista precisa mostrar a diferença.
        # Vazios quando o padrão ainda não acionou (ali a entrada É o gatilho).
        "andado_pct": rr.get("andado_pct"),
        "sobra_pct": rr.get("sobra_pct"),
        "rr_gatilho": (rr.get("no_gatilho") or {}).get("rr"),
        "rr_motivo": rr.get("motivo"),
        # O STORM na MESMA linha, em célula própria — o objetivo declarado é comparar
        # os dois setups no mesmo ativo de relance. Nunca no mesmo campo: misturar os
        # dois numa coluna só faria a taxa de acerto descrever trade nenhum.
        "storm": _storm_row(ticker, date, frame, price),
    }


def _storm_row(ticker: str, date: str, frame: str, price: float | None) -> dict[str, Any]:
    """A leitura do STORM naquele frame, compacta pra caber numa linha do scan.

    Setup DIFERENTE do 1-2-3 desta lista (DA-081): outro detector, outro ponto 2,
    outro stop, outro alvo — e um filtro (o Éden) com poder de VETO. Por isso ele
    ocupa a SUA célula, com o seu estado, e nunca se mistura ao 1-2-3 na mesma
    coluna: acerto de um setup com R:R de outro não descreve trade nenhum (task 008).

    Das DUAS entradas do padrão (ponto 2 e ponto 3), a linha carrega a mais PRÓXIMA
    do preço — é a que decide agora. A outra continua inteira na análise; aqui o
    espaço é de uma célula, e escolher a mais próxima é a escolha que responde
    "isto está para acontecer?". O ``title`` da célula leva as duas.
    """
    try:
        plano = build_storm_plan_dict(ticker, date, timeframe=frame) or {}
    except Exception as exc:  # noqa: BLE001 — o Storm nunca derruba o scan do 1-2-3
        logger.info("storm no scan falhou para %s %s: %s", ticker, frame, exc)
        return {"estado": "sem_dado"}
    pat = plano.get("pattern") or {}
    leituras = plano.get("leituras") or []
    if not pat or not leituras:
        return {"estado": "sem_setup",
                "eden": (plano.get("eden") or {}).get("direcao"),
                # O NOME do estado viaja pronto: a célula do scan não reescreve rótulo
                # (foi assim que a tela ganhou três jeitos de dizer timeframe).
                "eden_rotulo": (plano.get("eden") or {}).get("rotulo_curto"),
                "eden_ok": bool((plano.get("eden") or {}).get("alinhado")),
                "opera": False, "motivo": plano.get("motivo")}
    direction = pat.get("direction")
    stop = (plano.get("stop") or {}).get("price")

    def _dist(le):
        t = le.get("trigger")
        return abs(price / float(t) - 1.0) if (price and t) else 9.9
    escolhida = min(leituras, key=_dist)
    dist = _dist(escolhida) if price else None
    estado = escolhida.get("state")
    # O CICLO vem ANTES DE TUDO (DA-129) — inclusive do veto. Um padrão que chegou
    # ao alvo ou ao stop é HISTÓRIA: o Éden responde "vale a pena entrar agora?", e
    # não há entrada nenhuma a autorizar num trade que já terminou. Sem esta linha o
    # Storm publicava "vetado" sobre o próprio desfecho que ele mesmo calcula.
    if (_do_ciclo := _estado_do_ciclo(pat)) is not None:
        linha_estado = _do_ciclo
    # VETO do Éden vem ANTES do estado do gatilho: um padrão acionado que a regra
    # proíbe não é "em movimento", é um trade que não se faz.
    elif not plano.get("opera"):
        linha_estado = "vetado"
    elif plano.get("qualidade") == "neutra":
        # OPERA, mas na região que o Stormer chama de perigosa. Estado próprio: uma
        # lista que mostra "em gatilho" igual pros dois esconde justamente o aviso.
        linha_estado = "zona_neutra"
    elif dist is not None and dist <= _GATILHO_TOL:
        linha_estado = "em_gatilho"
    elif estado == "acionado":
        linha_estado = "em_movimento"
    else:
        linha_estado = "formando"
    rr = escolhida.get("risk_reward") or {}
    return {
        "estado": linha_estado,
        # DESFECHO e CICLO viajam na linha do Storm como já viajavam na do 1-2-3
        # (DA-129): "concluído" sem dizer se ganhou ou perdeu não informa nada, e
        # era exatamente o que faltava pro método mais usado dos dois.
        "desfecho": pat.get("desfecho"),
        "ciclo": pat.get("ciclo"),
        "direction": direction,
        "entrada": escolhida.get("entrada"),
        "ordem": escolhida.get("ordem"),
        "pattern_state": estado,
        "trigger": escolhida.get("trigger"),
        "dist_pct": dist,
        "dist_txt": _fmt_pct(dist),
        "sl": stop,
        "tp": (escolhida.get("target") or {}).get("price"),
        "rr": rr.get("rr"),
        "rr_note": rr.get("note"),
        "qualidade": plano.get("qualidade"),
        "opera": bool(plano.get("opera")),
        "veto": plano.get("veto"),
        "eden": (plano.get("eden") or {}).get("direcao"),
        "eden_rotulo": (plano.get("eden") or {}).get("rotulo_curto"),
        "eden_ok": bool((plano.get("eden") or {}).get("alinhado")),
        # ZONA NEUTRA viaja na linha (task 016): ela OPERA, então `opera` sozinho não
        # a distingue de um Éden alinhado — e o aviso do Stormer ("operar aqui é
        # muito mais perigoso") sumiria numa lista onde os dois saem iguais.
        "zona_neutra": bool((plano.get("eden") or {}).get("zona_neutra")),
        # As DUAS leituras viajam pro title da célula: a linha mostra a mais
        # próxima, mas esconder a outra seria decidir pelo leitor.
        "leituras": [{"entrada": L.get("entrada"), "ordem": L.get("ordem"),
                      "trigger": L.get("trigger"),
                      "tp": (L.get("target") or {}).get("price"),
                      "rr": (L.get("risk_reward") or {}).get("rr")}
                     for L in leituras],
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
    best = min(rows, key=lambda r: (_URGENCIA.get(r.get("estado"), 9), -_resto(r),
                                    r.get("dist_pct") if r.get("dist_pct") is not None else 9.9))
    return {"ticker": ticker, "frames": rows, "melhor": best}


# A ESCADA COMPLETA de UM ativo (task 20260831-012): os cinco frames que o método
# de fato usa — o maior manda na TESE, o menor no TIMING. É a MESMA leitura do
# scan (mesmo detector, mesmo vocabulário de estado, mesmo par 1-2-3 × Storm),
# só que num ativo só e na escada inteira, pra a análise já nascer comparável em
# vez de o usuário trocar de chip cinco vezes e guardar na cabeça.
ESCADA_FRAMES = ("1w", "1d", "4h", "1h", "15m")

# Paralelismo da escada de UM ativo. É I/O-bound (uma série por frame), e cinco
# frames em série somam a latência de todos. MEDIDO (31/08, símbolo frio, mediana):
#
#   ============  =========  ===============
#   cenário       em série   em paralelo (5)
#   ============  =========  ===============
#   frio           ~5,6s      **~2,2–3,1s**
#   quente         ~0,5s        ~0,5s
#   ============  =========  ===============
#
# Cinco workers = um por frame: não há fila, e o teto vira o frame mais lento em
# vez da soma. Diferente do ``_SCAN_WORKERS`` (que paraleliza ATIVOS e por isso
# tem throttle do provedor a considerar): aqui são 5 requisições de um símbolo só.
_ESCADA_WORKERS = 5


def scan_symbol_frames(ticker: str, date: str,
                       frames: tuple = ESCADA_FRAMES,
                       workers: int = _ESCADA_WORKERS) -> dict[str, Any]:
    """A escada de UM ativo — os frames pedidos, **em paralelo**, na ordem pedida.

    Irmã de :func:`scan_symbol`, com duas diferenças que só fazem sentido no caso
    de um ativo só:

    * os frames rodam **concorrentes** (o gargalo é rede, não CPU) — em
      :func:`scan_watchlist` isso seria aninhar pool dentro de pool e multiplicar
      o throttle, então lá o paralelismo continua sendo por ATIVO;
    * não há ``melhor``: a escada não elege frame nenhum. Quem tem o veredito é a
      análise que a chamou, e o resto é EXPLORATÓRIO — deixar o scanner apontar um
      "melhor" aqui criaria um segundo veredito na mesma tela.

    O preço LIVE é buscado UMA vez e compartilhado (a cotação é do ativo, não do
    frame). Fail-open por frame, como o scan: um frame sem candle volta
    ``sem_dado`` com o motivo, nunca um nível inventado.

    ``ex.map`` preserva a ordem de entrada, então a escada sai sempre do frame
    maior pro menor — determinística apesar do paralelismo.
    """
    ticker = (ticker or "").strip().upper()
    frames = tuple(frames or ())
    if not ticker or not frames:
        return {"ticker": ticker, "frames": []}
    live = _live_price(ticker)
    n = max(1, min(workers, len(frames)))
    with ThreadPoolExecutor(max_workers=n, thread_name_prefix="escada") as ex:
        rows = list(ex.map(lambda tf: _frame_row(ticker, date, tf, live_price=live), frames))
    return {"ticker": ticker, "frames": rows}


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
    gerado_em = timeutil.stamp()
    if not tickers:
        return {"date": date, "frames": list(frames), "resumo": {}, "ativos": [],
                "gerado_em": gerado_em}
    n = max(1, min(workers, len(tickers)))
    with ThreadPoolExecutor(max_workers=n, thread_name_prefix="scan") as ex:
        out = list(ex.map(lambda t: scan_symbol(t, date, frames), tickers))
    ativos, counts = ordenar_e_resumir(out)
    # ``gerado_em`` é o carimbo da varredura, em Manaus e offset-aware: sem ele a
    # tela só sabe a hora em que o JSON *chegou* nela, e um resultado servido do
    # disco (ou do memo) se passaria por recém-saído. Marcado ANTES do trabalho —
    # é a hora do dado que se leu, não a de quando o último ativo terminou.
    return {"date": date, "frames": list(frames), "resumo": counts, "ativos": ativos,
            "gerado_em": gerado_em}


def ordenar_e_resumir(ativos: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Ordem de urgência + contagem por estado — a apresentação da lista, num lugar só.

    Sai de dentro do :func:`scan_watchlist` porque o **último conhecido** (o snapshot
    em disco) mistura ativos de passadas diferentes e precisa reordenar e recontar o
    conjunto MESCLADO. Duas cópias divergiriam, e a divergência apareceria como a
    lista da abertura ordenada de um jeito e a da varredura de outro — o mesmo
    portfólio parecendo dois.

    Dentro do mesmo estado, quem tem MAIS movimento pela frente vem antes: um
    acionado que sobrou 80% do caminho ainda é aproveitável; um que sobrou 5% é um
    trade que já aconteceu, e mostrar os dois com o mesmo peso é o que faz o leitor
    concluir que o método só dá R:R ruim.
    """
    out = sorted(ativos, key=lambda s: (
        _URGENCIA.get((s.get("melhor") or {}).get("estado"), 9),
        -_resto(s.get("melhor") or {}),
        (s.get("melhor") or {}).get("dist_pct")
        if (s.get("melhor") or {}).get("dist_pct") is not None else 9.9))
    counts: dict[str, int] = {}
    for s in out:
        estado = (s.get("melhor") or {}).get("estado")
        if estado:
            counts[estado] = counts.get(estado, 0) + 1
    return out, counts


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
        """Loga UM gatilho (chamado só quando estado == em_gatilho).

        ``setup`` diz DE QUAL setup veio o gatilho — ``123`` (o desta lista) ou
        ``storm`` (com a entrada usada em ``entrada``). Sem isso a taxa de acerto
        mistura dois métodos com stops, alvos e R:R construídos por regras
        diferentes, e o número resultante não descreve nenhum dos dois: é a mesma
        lição da task 008 (acerto de um grupo com R:R de outro não descreve trade).
        Linha antiga sem ``setup`` é do 1-2-3 — o ledger é append-only e não se
        reescreve; quem lê é que assume o default (:func:`_setup_da_entrada`).
        """
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "setup": row.get("setup") or "123",
                 **{k: row.get(k) for k in ("ticker", "frame", "direction",
                                            "pattern_state", "trigger", "sl", "tp", "rr")}}
        if row.get("entrada"):
            entry["entrada"] = row.get("entrada")
        self._append(entry)

    def record_close(self, chave: str, veredito: str, fechado_em: str,
                     empate_na_barra: bool = False) -> None:
        """Grava o FECHAMENTO de um gatilho — a linha que torna o veredito eterno.

        Recalcular o fechamento a cada leitura só é imutável enquanto a série ainda
        alcança o dia do log, e ela NÃO alcança pra sempre: ``build_price_chart``
        devolve as últimas N barras, e num frame de 1h a janela cobre poucas semanas.
        Quando a barra do toque saísse dela, um ``bateu_tp`` voltava calado a
        ``andamento`` e a taxa de acerto mudava — exatamente o defeito que o fechamento
        pela série dizia ter matado. Um toque é um FATO datado: vira linha no ledger."""
        self._append({"tipo": "fechamento", "ref": chave, "veredito": veredito,
                      "fechado_em": fechado_em, "empate_na_barra": bool(empate_na_barra),
                      "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")})

    def record_pass(self, alvos: int, lidos: int, sem_dado: int,
                    gatilhos: int, origem: str = "agenda",
                    sessao: str | None = None) -> None:
        """Grava que uma PASSADA aconteceu — a linha que separa "não houve gatilho" de
        "ninguém olhou".

        Sem ela o ledger só sabe dizer o que ACONTECEU, e um período sem linhas fica
        ambíguo: pode ter sido mercado parado ou serviço fora do ar. Metade do valor de
        um track record está em saber que se olhou e não havia nada.

        ``sem_dado`` é a parte que protege o número: fonte degradada não vira gatilho
        inventado nem "não aconteceu" falso — vira contagem declarada nesta linha.

        É inerte pro motor de vereditos: :meth:`entries` só devolve linha SEM ``tipo``, e
        :meth:`fechamentos` só lê ``tipo == "fechamento"``.
        """
        self._append({"tipo": "passada", "origem": origem, "sessao": sessao,
                      "alvos": int(alvos), "lidos": int(lidos),
                      "sem_dado": int(sem_dado), "gatilhos": int(gatilhos),
                      "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")})

    def passadas(self) -> list[dict[str, Any]]:
        """As passadas registradas, da mais antiga pra mais nova."""
        return [x for x in self._linhas() if x.get("tipo") == "passada"]

    def _append(self, obj: dict[str, Any]) -> None:
        with self._lock, open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, default=str) + "\n")

    def _linhas(self) -> list[dict[str, Any]]:
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

    def entries(self) -> list[dict[str, Any]]:
        """Só os GATILHOS. Linha sem ``tipo`` é gatilho (formato anterior aos
        fechamentos — o ledger é append-only, nada foi reescrito)."""
        return [x for x in self._linhas() if not x.get("tipo")]

    def fechamentos(self) -> dict[str, dict[str, Any]]:
        """``chave do gatilho -> fechamento``. O PRIMEIRO fechamento de uma chave
        manda: regravar não sobrescreve fato (append-only também na leitura)."""
        out: dict[str, dict[str, Any]] = {}
        for x in self._linhas():
            if x.get("tipo") == "fechamento" and x.get("ref") and x["ref"] not in out:
                out[x["ref"]] = x
        return out


def _dia(v: Any) -> str:
    """Parte-DATA (``YYYY-MM-DD``) de um ts do log ou do rótulo de um candle."""
    return str(v or "")[:10]


def _chave(e: dict[str, Any]) -> str:
    """Identidade de um gatilho logado — o que amarra o fechamento à entrada.

    O ``setup`` NÃO entra na chave: as chaves antigas (todas do 1-2-3, gravadas
    antes da task 023) já existem no ledger amarradas aos seus fechamentos, e mudar
    a forma da chave desamarraria todas de uma vez. O que distingue os setups é o
    campo ``setup`` da entrada, não a identidade dela.
    """
    return "|".join(str(e.get(k)) for k in ("ts", "ticker", "frame", "trigger"))


# Setups que o track record sabe separar. ``123`` é o desta lista; ``storm`` é o
# 1-2-3 do Stormer (DA-081) — outro detector, outro stop, outro alvo.
SETUPS_DO_LEDGER = ("123", "storm")

# Gate de amostra — DO TRACK RECORD, importado por ``execucao.confiabilidade`` (não
# uma segunda constante lá: dois limiares com valores diferentes seria a lista
# dizendo "n insuficiente" e o card dizendo "operável" sobre a MESMA amostra —
# tanto pro acerto quanto pro PnL de paper, DA-154). Convenção estatística
# declarada, `a calibrar`: com N<5 o intervalo de uma proporção cobre quase [0,1]
# e não informa; em N=10, p=0,5, o Wilson 95% ainda é ~±30pp; só perto de 20-30
# ele aperta pra algo acionável. Não vêm do corpus — vêm de "não exibir número
# que o intervalo desmente".
_N_MINIMO = 5
_N_OPERAVEL = 20

# PAPER TRADING NO TRACK RECORD (DA-154). A proposta do Samyr: "como se fosse
# paper" — POSIÇÃO FIXA em dólares por operação, não RISCO fixo. Com banca=100 a
# quantidade sai de banca/entrada e o resultado é (saída−entrada)/entrada × banca
# — a perda por trade VARIA com a distância do stop (ela não é ±100 sempre). É a
# escolha certa pra COMPARAR setups (mesma régua pros dois), mas tem de ser dita
# na tela — quem lê "paper" acha risco fixo por padrão. Configurável (o parâmetro
# ``banca`` de :func:`scan_verdicts`), 100 é só o chão.
_BANCA_PADRAO = 100.0
_PREMISSA_PAPER = ("paper: posição FIXA em dólares por operação (não risco fixo — "
                   "a perda varia com a distância do stop) · sem custos · sem slippage")


def _setup_da_entrada(e: dict[str, Any]) -> str:
    """De qual setup veio um gatilho logado. Entrada sem carimbo é do 1-2-3: quando
    o campo nasceu (task 023) o ledger só tinha gatilhos dele, e append-only quer
    dizer que a linha velha não é reescrita — o default é que a lê."""
    s = str(e.get("setup") or "123")
    return s if s in SETUPS_DO_LEDGER else "123"


# Barras por DIA de calendário, por frame — só pra DIMENSIONAR o pedido de série
# (pedir a mais é inofensivo: a fonte devolve o que tem; pedir a menos é que
# apagava um fechamento). Usa a taxa de CRIPTO (24h/dia), que é o teto: uma ação
# tem menos barras por dia, então a janela sobra.
_BARRAS_POR_DIA = {"1w": 1, "1d": 1, "4h": 6, "1h": 24, "15m": 96}
_BARS_MIN = 260      # o default de build_price_chart — nunca pedir menos
_BARS_MAX = 5000     # teto de sanidade: nenhuma leitura de painel puxa mais


def _bars_para_cobrir(desde_dia: str, ate_dia: str, frame: str) -> int:
    """Quantas barras pedir pra a série alcançar o dia do log (:data:`_BARS_MIN` no
    mínimo). Sem data utilizável, o mínimo — a cobertura é conferida depois."""
    try:
        d0 = datetime.strptime(desde_dia, "%Y-%m-%d")
        d1 = datetime.strptime(_dia(ate_dia) or desde_dia, "%Y-%m-%d")
    except (ValueError, TypeError):
        return _BARS_MIN
    dias = max(0, (d1 - d0).days) + 2      # +2 de folga (fuso do log vs do mercado)
    n = dias * _BARRAS_POR_DIA.get(frame, 1)
    return max(_BARS_MIN, min(_BARS_MAX, n))


def _serie_cobre(candles: list[dict], desde_dia: str) -> bool:
    """A série alcança o dia do log? Só então a ausência de toque significa mesmo
    "não tocou" — série que começa DEPOIS do gatilho não viu o que aconteceu, e
    tratá-la como ``andamento`` é afirmar o que não se sabe."""
    if not candles or not desde_dia:
        return False
    primeiro = _dia(candles[0].get("d"))
    return bool(primeiro) and primeiro <= desde_dia


def _tp_publicavel(e: dict[str, Any]) -> float | None:
    """O alvo LOGADO, se ele for coerente com a direção do trade — senão ``None``.

    O log é append-only e carrega entradas gravadas antes do fix do alvo degenerado
    (ex.: ZEC-USD 4h de 29/08, ``tp == trigger == 834,82``, ``rr: null``). Um alvo
    que não está à frente da entrada é ``bateu_tp`` no instante em que aciona —
    ACERTO FABRICADO. Não se reescreve o ledger; ignora-se o alvo na LEITURA, e o
    trade só pode fechar pelo SL, que é a leitura honesta.

    DOIS sinais têm que apontar juntos: ``rr`` ausente (o próprio plano recusou a
    conta) E o alvo do lado errado do gatilho. Só o segundo não bastaria — num setup
    JÁ ACIONADO a entrada é o preço, não o gatilho, e um alvo entre os dois é
    legítimo (e vem com o ``rr`` calculado). Rejeitar aquele seria trocar um acerto
    fabricado por uma PERDA fabricada."""
    tp, trigger = e.get("tp"), e.get("trigger")
    if tp is None:
        return None
    if trigger is None or e.get("rr") is not None:
        return float(tp)          # o plano calculou o retorno: alvo coerente
    venda = e.get("direction") == "venda"
    ok = (float(tp) < float(trigger)) if venda else (float(tp) > float(trigger))
    return float(tp) if ok else None


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
    # Sem dia de referência não há janela: ``dia <= ""`` nunca é verdade, então TODA
    # barra entraria — inclusive as ANTERIORES ao gatilho, fabricando um toque que
    # aconteceu antes de o setup existir. Sem carimbo, não se conta nada.
    if not desde_dia:
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


def scan_verdicts(log: ScanLog, date: str, banca: float = _BANCA_PADRAO) -> dict[str, Any]:
    """Re-avalia cada gatilho logado — FECHADO pelo ledger, ABERTO pelo preço de hoje.

    **Fechado é fato gravado, não conta refeita.** Quando a série mostra o primeiro
    toque em TP ou SL (:func:`_primeiro_toque`), o veredito é APENDADO no próprio
    ``scans.jsonl`` (:meth:`ScanLog.record_close`) e, da próxima leitura em diante,
    vem de lá. Recalcular a cada chamada só parecia imutável: ``build_price_chart``
    devolve as ÚLTIMAS N barras, então a janela desliza — num frame de 1h ela cobre
    poucas semanas, e o dia em que a barra do toque saísse dela um ``bateu_tp``
    voltaria calado pra ``andamento``. O pedido de série passou a ser dimensionado
    pelo intervalo log→data (:func:`_bars_para_cobrir`), mas isso só ADIA o teto (a
    fonte não guarda intradiário eterno); o que resolve é o fato persistido.

    **Estados abertos.** ``andamento_*`` é marcado a mercado — posição viva vale o
    preço de agora. ``sem_serie_cobrindo`` é o estado NOVO e distinto: a série não
    alcança o dia do log, então não se sabe se tocou; antes isso caía calado em
    ``andamento``, que afirma o que não se sabe. Entrada sem ``ts`` não é avaliada
    (sem janela, toda barra contaria — inclusive as anteriores ao gatilho).

    **Expectativa, não só acerto.** Taxa de acerto sozinha engana quando o alvo é
    perto e o stop é longe: com R:R 0,13 é preciso acertar 88,5% só pra empatar. O
    painel passa a devolver a expectativa em múltiplos de risco (R) e o acerto de
    equilíbrio ao lado — ver :func:`_expectativa`.

    **PnL de paper** (DA-154): expectativa em R é abstrata e não soma. Com ``banca``
    dólares de posição fixa por operação, o resultado vira dinheiro — comparável
    entre setups, somável numa curva de equity. Ver :func:`_pnl_paper_resumo`.

    Só leitura de série cacheada, $0 de LLM.
    """
    fechados_log = log.fechamentos()
    verdicts = []
    novos_fechamentos = []
    for e in log.entries():
        ticker = str(e.get("ticker") or "")
        frame = str(e.get("frame") or "1d")
        trigger, sl = e.get("trigger"), e.get("sl")
        tp = _tp_publicavel(e)
        chave = _chave(e)
        v = dict(e)
        v["tp_ignorado"] = e.get("tp") is not None and tp is None
        venda = e.get("direction") == "venda"
        desde = _dia(e.get("ts"))

        # 1) Fechamento JÁ GRAVADO: fato, não se recalcula (nem se busca série).
        gravado = fechados_log.get(chave)
        if gravado:
            v.update({"veredito": gravado.get("veredito"),
                      "fechado_em": gravado.get("fechado_em"),
                      "empate_na_barra": bool(gravado.get("empate_na_barra")),
                      "fechado": True, "fonte_veredito": "ledger"})
            v["preco_agora"] = _live_price(ticker)
            verdicts.append(v)
            continue

        # 2) Sem carimbo de tempo não há janela — não se afirma nada.
        if not desde:
            v.update({"veredito": "sem_dado", "fechado": False,
                      "motivo": "entrada sem carimbo de tempo — sem janela pra medir",
                      "preco_agora": _live_price(ticker)})
            verdicts.append(v)
            continue

        try:
            plan = build_actionable_plan_dict(ticker, date, timeframe=frame)
        except Exception:  # noqa: BLE001 — verdict ausente não derruba o resto
            plan = {}
        try:
            # Janela dimensionada pelo intervalo log→data (o default de 260 barras
            # não cobre um 1h de algumas semanas atrás).
            candles = (build_price_chart(
                ticker, date, bars=_bars_para_cobrir(desde, date, frame),
                timeframe=frame) or {}).get("candles") or []
        except Exception:  # noqa: BLE001 — sem série não se fecha no escuro
            candles = []
        live = _live_price(ticker)
        price = live if live is not None else (plan or {}).get("price")
        v["preco_agora"] = price

        toque = _primeiro_toque(candles, desde, tp, sl, venda)
        if toque:
            v.update(toque)
            v["fechado"] = True
            v["fonte_veredito"] = "serie"
            novos_fechamentos.append((chave, toque))
        elif not _serie_cobre(candles, desde):
            # A série não alcança o gatilho: pode ter tocado sem ninguém ver.
            v.update({"veredito": "sem_serie_cobrindo", "fechado": False,
                      "motivo": "a série disponível não alcança o dia do gatilho"})
        elif price is None or trigger is None:
            v.update({"veredito": "sem_dado", "fechado": False})
        else:
            v["fechado"] = False
            if price > trigger:
                v["veredito"] = "andamento_prejuizo" if venda else "andamento_lucro"
            else:
                v["veredito"] = "andamento_lucro" if venda else "andamento_prejuizo"
        verdicts.append(v)

    # Grava os fechamentos NOVOS depois de varrer (uma escrita por fato, e a
    # releitura seguinte já os encontra no ledger).
    for chave, toque in novos_fechamentos:
        log.record_close(chave, toque["veredito"], toque["fechado_em"],
                         toque.get("empate_na_barra", False))

    for v in verdicts:
        v["setup"] = _setup_da_entrada(v)
    n = [v for v in verdicts if v.get("veredito") in ("bateu_tp", "bateu_sl")]
    acerto = (sum(1 for v in n if v["veredito"] == "bateu_tp") / len(n)) if n else None
    out = {"verdicts": verdicts, "n_fechados": len(n), "taxa_acerto": acerto}
    out.update(_expectativa(n))
    # POR SETUP, além do agregado. Dois setups com stops, alvos e R:R construídos por
    # regras diferentes somados num número só não descrevem nenhum dos dois — é a
    # mesma lição da task 008. O agregado fica (é a leitura do painel inteiro), mas
    # agora ao lado da decomposição, e cada uma com a SUA base declarada.
    out["por_setup"] = {}
    for nome in SETUPS_DO_LEDGER:
        do_setup = [v for v in verdicts if v.get("setup") == nome]
        fech = [v for v in do_setup if v.get("veredito") in ("bateu_tp", "bateu_sl")]
        bloco = {
            "n": len(do_setup),
            "n_fechados": len(fech),
            "taxa_acerto": (sum(1 for v in fech if v["veredito"] == "bateu_tp") / len(fech))
                           if fech else None,
        }
        bloco.update(_expectativa(fech))
        out["por_setup"][nome] = bloco

    # PAPER TRADING (DA-154): mesma decomposição do acerto/expectativa acima —
    # agregado, por setup e por frame (o R:R varia muito entre 1h e semanal, e
    # somar os dois esconderia isso). Banca declarada e sempre presente, mesmo
    # com n=0 — é o que permite a tela dizer "amostra insuficiente" em vez de
    # calar a seção inteira.
    banca_efetiva = float(banca) if banca and banca > 0 else _BANCA_PADRAO
    out["paper"] = {
        "banca_por_trade": banca_efetiva,
        "premissa": _PREMISSA_PAPER,
        "agregado": _pnl_paper_resumo(n, banca_efetiva),
        "por_setup": {nome: _pnl_paper_resumo(
            [v for v in n if v.get("setup") == nome], banca_efetiva)
            for nome in SETUPS_DO_LEDGER},
        "por_frame": {frame: _pnl_paper_resumo(vs, banca_efetiva)
                      for frame, vs in _agrupa_por_frame(n).items()},
    }
    return out


def _expectativa(fechados: list[dict[str, Any]]) -> dict[str, Any]:
    """Expectativa em múltiplos de RISCO (R) — a métrica que responde à pergunta.

    Taxa de acerto sozinha é a armadilha clássica: alvo perto e stop longe produzem
    acerto ALTO com expectativa NEGATIVA. Distribuição real do scan de 29/08 (n=33):
    mediana de R:R **0,13** — com ela é preciso acertar **88,5%** só pra empatar.

    ``E[R] = p·RR − (1−p)``, com ``RR`` = R:R MÉDIO planejado no momento do gatilho
    (o que se podia saber ao entrar). ``p`` é medido no MESMO subconjunto que tem R:R
    conhecido, não na amostra toda: misturar a taxa de acerto de um grupo com o R:R
    de outro daria um número que não descreve trade nenhum. Por isso ``n_com_rr`` e
    ``acerto_com_rr`` vão junto — a expectativa é declarada com a sua base.

    Acerto de equilíbrio = ``1/(1+RR)``. Tudo ``None`` quando não há fechado com R:R
    conhecido: número inventado é pior que a ausência dele."""
    com_rr = [v for v in fechados if v.get("rr") is not None]
    if not com_rr:
        return {"rr_medio": None, "expectativa_r": None, "acerto_equilibrio": None,
                "n_com_rr": 0, "acerto_com_rr": None}
    rr_medio = sum(float(v["rr"]) for v in com_rr) / len(com_rr)
    p = sum(1 for v in com_rr if v["veredito"] == "bateu_tp") / len(com_rr)
    return {
        "rr_medio": round(rr_medio, 2),
        "expectativa_r": round(p * rr_medio - (1.0 - p), 3),
        "acerto_equilibrio": round(1.0 / (1.0 + rr_medio), 4) if rr_medio > 0 else None,
        "n_com_rr": len(com_rr),
        "acerto_com_rr": round(p, 4),
    }


def _agrupa_por_frame(fechados: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Fechados agrupados por frame — só os frames com o que mostrar aparecem
    (frame sem nenhum fechado some, em vez de virar um bloco n=0 sem sentido)."""
    out: dict[str, list[dict[str, Any]]] = {}
    for v in fechados:
        out.setdefault(str(v.get("frame") or "1d"), []).append(v)
    return out


def _pnl_paper_trade(v: dict[str, Any], banca: float) -> dict[str, Any] | None:
    """PnL de UM trade fechado, POSIÇÃO FIXA (não risco fixo — ver :data:`_PREMISSA_PAPER`).

    Quantidade = ``banca / entrada``; resultado = variação percentual do preço ×
    banca. A ENTRADA é ``entrada`` quando o log a carimbou (Storm, que referencia
    o ponto de entrada real) ou o ``trigger`` (1-2-3 — a entrada É o gatilho). A
    SAÍDA é o nível que o veredito diz que foi tocado: o alvo publicável
    (:func:`_tp_publicavel`, nunca um alvo degenerado) em ``bateu_tp``, o stop em
    ``bateu_sl``. ``None`` pra qualquer coisa que não seja um fechado de verdade
    ou que não tenha preço suficiente pra calcular — número inventado é pior que
    a ausência dele.

    ``pnl_risco_fixo_usd`` é a LEITURA ALTERNATIVA que a mesma banca responde de
    graça: "se eu arriscasse ``banca`` por trade" — ganha ``rr·banca`` no alvo,
    perde ``banca`` no stop. As duas convivem porque respondem perguntas
    diferentes (comparar setups × dimensionar risco), e nenhuma é `a` resposta.
    """
    veredito = v.get("veredito")
    if veredito not in ("bateu_tp", "bateu_sl"):
        return None
    entrada = v.get("entrada") if v.get("entrada") is not None else v.get("trigger")
    saida = _tp_publicavel(v) if veredito == "bateu_tp" else v.get("sl")
    if entrada is None or saida is None:
        return None
    try:
        entrada, saida = float(entrada), float(saida)
    except (TypeError, ValueError):
        return None
    if entrada <= 0:
        return None
    variacao = (saida - entrada) / entrada
    if v.get("direction") == "venda":
        variacao = -variacao
    rr = v.get("rr")
    pnl_risco_fixo_usd = None
    if rr is not None:
        try:
            pnl_risco_fixo_usd = round(banca * (float(rr) if veredito == "bateu_tp" else -1.0), 2)
        except (TypeError, ValueError):
            pnl_risco_fixo_usd = None
    return {
        "ts": v.get("fechado_em") or _dia(v.get("ts")),
        "ticker": v.get("ticker"), "setup": v.get("setup"), "frame": v.get("frame"),
        "veredito": veredito,
        "pnl_pct": round(variacao * 100, 2),
        "pnl_usd": round(banca * variacao, 2),
        "pnl_risco_fixo_usd": pnl_risco_fixo_usd,
    }


def _pnl_paper_resumo(fechados: list[dict[str, Any]], banca: float) -> dict[str, Any]:
    """O resumo de PnL de paper de um grupo (agregado, de um setup, ou de um frame).

    O ``nivel`` usa o MESMO gate de :data:`_N_MINIMO`/:data:`_N_OPERAVEL` do
    índice de confiabilidade — dois limiares divergentes diriam "acerto operável,
    PnL insuficiente" sobre a mesma amostra. A CURVA DE EQUITY é cronológica (pelo
    dia do FECHAMENTO — ``fechado_em``, não o dia do gatilho): é a pergunta "isso
    daria dinheiro, no tempo?", e um trade fechado antes de outro que abriu depois
    tem de vir primeiro na curva."""
    trades = [t for t in (_pnl_paper_trade(v, banca) for v in fechados) if t is not None]
    trades.sort(key=lambda t: t["ts"] or "")
    n = len(trades)
    nivel = "insuficiente" if n < _N_MINIMO else ("preliminar" if n < _N_OPERAVEL else "operavel")
    base = {"n": n, "nivel": nivel, "banca_por_trade": banca,
            "pnl_total_usd": None, "pnl_total_pct": None, "pnl_medio_usd": None,
            "melhor_trade": None, "pior_trade": None, "curva_equity": []}
    if not trades:
        return base
    equity = 0.0
    curva = []
    for t in trades:
        equity = round(equity + t["pnl_usd"], 2)
        curva.append({"ts": t["ts"], "ticker": t["ticker"], "pnl_usd": t["pnl_usd"],
                      "equity_usd": equity})
    total = round(sum(t["pnl_usd"] for t in trades), 2)
    melhor = max(trades, key=lambda t: t["pnl_usd"])
    pior = min(trades, key=lambda t: t["pnl_usd"])
    base.update({
        "pnl_total_usd": total,
        # % sobre o CAPITAL EMPREGADO (banca × n trades, sem reaproveitar equity
        # entre eles — cada operação abre com a MESMA banca fixa, não composta).
        "pnl_total_pct": round(total / (banca * n) * 100, 2) if banca > 0 else None,
        "pnl_medio_usd": round(total / n, 2),
        "melhor_trade": {"ticker": melhor["ticker"], "pnl_usd": melhor["pnl_usd"],
                         "pnl_pct": melhor["pnl_pct"]},
        "pior_trade": {"ticker": pior["ticker"], "pnl_usd": pior["pnl_usd"],
                       "pnl_pct": pior["pnl_pct"]},
        "curva_equity": curva,
    })
    return base

"""AGENDA DO SCAN — uma passada por CANDLE FECHADO, não a máxima que a API aguenta.

O pedido do Samyr: *"a melhor frequência possível pra gerar histórico e dados sobre
acertos"*. O track record existe desde a task 023, mas só crescia quando ALGUÉM abria a
tela — em três dias o ledger juntou 20 gatilhos e 4 fechamentos, que é amostra de menos
pra passar do ``n<5`` do gate de confiabilidade (:func:`execucao.confiabilidade`).

MEDIDO ANTES DE ESCOLHER (30/08, watchlist real de 20 ativos × frames 1d+4h+1h):

===================================  ==========================================
uma passada FRIA                     20,7s · 20 leituras de diário + 40 de
                                     intradiário + 20 cotações
uma passada QUENTE (dentro do TTL)   3,6s · **zero** chamadas de rede
===================================  ==========================================

**A frequência NÃO é decidida pelo limite da fonte.** Na cadência horária isto dá ~80
leituras por hora (~1,3/min) — duas ordens de grandeza abaixo de qualquer limite
plausível do provedor. Não fomos empurrar a fonte até o "não" de propósito: o único IP
disponível é o de PRODUÇÃO, e um bloqueio derrubaria o produto no ar pra descobrir um
número que não decide nada aqui. O que decide é a informação.

**POR QUE UMA POR CANDLE FECHADO.** O padrão e o gatilho saem da série date-guarded: só
mudam quando um candle FECHA. Varrer o 1h a cada 5 minutos devolve o MESMO padrão com um
candle ainda em formação. Duas consequências, e a segunda é a que manda:

1. o ledger não incha — a de-duplicação é por ``(setup, ticker, frame, gatilho)``, e o
   gatilho é um nível do padrão: repetir a passada não cria linha nova;
2. o que a passada extra pega são TOQUES INTRABARRA — o preço entrando na faixa de 0,5%
   do gatilho e saindo antes do fechamento. Isso é informação de verdade, e é
   justamente a que **não se quer** no track record: ele mede o que a pessoa
   operando ESTA TELA teria visto, e ela olha a tela em cadência humana, não a cada
   30 segundos. Ledger cheio de roçadas que ninguém pegaria mede outro método.

Então a cadência é a do candle mais rápido que o scan lê (:data:`scanner.SCAN_FRAMES` →
1h), com um atraso curto pra o candle já estar fechado na fonte.

**AÇÃO TEM PREGÃO, CRIPTO É 24/7.** Fora do pregão a ação repete o mesmo candle: gasto
sem informação. A sessão sai do ``marketState`` que o :mod:`live_price` já lê — não de
um calendário nosso, que envelheceria em feriado. Uma cotação de referência por passada
responde pelas ações daquela passada (declarado: se entrar ação de outra bolsa na
watchlist, ela segue o pregão da referência — nunca em silêncio, é este parágrafo).

**AUSÊNCIA É REGISTRADA.** Cada passada grava uma linha ``tipo: "passada"`` no ledger com
quantos ativos foram lidos e quantos degradaram. Sem ela não dá pra distinguir "não
houve gatilho" de "ninguém olhou" — e essa diferença é metade do valor de um track
record. A linha é inerte pro motor de vereditos, que só lê gatilhos e fechamentos.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from tradingagents.webui.scanner import SCAN_FRAMES

logger = logging.getLogger(__name__)

# Duração do candle de cada frame, em minutos. É daqui que sai a cadência: a passada
# acontece quando o candle mais rápido que o scan lê FECHA.
MINUTOS_DO_FRAME = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440, "1w": 10080}

# Atraso depois do fechamento. A barra recém-fechada leva alguns segundos pra aparecer
# consolidada na fonte; varrer no segundo exato do fechamento lê a barra anterior e
# desperdiça a passada. 60s é curto o bastante pra não atrasar a leitura e longo o
# bastante pra a barra existir.
ATRASO_POS_FECHAMENTO_S = 60

# Sessões em que uma AÇÃO vale a pena: durante o pregão e nas pontas (pré e after),
# onde o preço ainda anda. "fechado" é a noite — ali a ação repete o mesmo candle.
# O after-market cobre o fechamento do candle DIÁRIO, que é o mais importante do dia.
SESSOES_ATIVAS = ("regular", "pre", "pos", "24h")


def cadencia_minutos(frames: tuple[str, ...] = SCAN_FRAMES) -> int:
    """Minutos entre passadas = o candle MAIS RÁPIDO que o scan lê.

    Uma passada calcula todos os frames de uma vez, então não há cadência por frame a
    escolher: o que define o ritmo é o menor candle da lista. Frame desconhecido é
    ignorado em vez de virar cadência de 1 minuto por acidente de digitação.
    """
    conhecidos = [MINUTOS_DO_FRAME[f] for f in frames if f in MINUTOS_DO_FRAME]
    return min(conhecidos) if conhecidos else MINUTOS_DO_FRAME["1h"]


def proxima_passada(agora: datetime, frames: tuple[str, ...] = SCAN_FRAMES,
                    atraso_s: int = ATRASO_POS_FECHAMENTO_S) -> datetime:
    """O próximo instante de varredura: fechamento do candle + atraso.

    Ancorada no RELÓGIO (minuto 0 da hora), não no momento em que o serviço subiu: um
    restart às 14h37 não pode passar a varrer aos 37 de cada hora, senão a passada deixa
    de coincidir com o fechamento do candle e volta a ler barra em formação.
    """
    passo = cadencia_minutos(frames)
    base = agora.replace(second=0, microsecond=0)
    minutos_do_dia = base.hour * 60 + base.minute
    proximo_bloco = (minutos_do_dia // passo + 1) * passo
    dia = base.replace(hour=0, minute=0)
    alvo = dia + timedelta(minutes=proximo_bloco, seconds=atraso_s)
    # O instante corrente pode já estar DEPOIS do fechamento + atraso do bloco atual
    # (serviço subiu no meio da janela): nesse caso o alvo é o do bloco corrente.
    atual = dia + timedelta(minutes=(minutos_do_dia // passo) * passo, seconds=atraso_s)
    return atual if atual > agora else alvo


def alvos_da_passada(watchlist: list[dict[str, Any]], sessao: str) -> list[str]:
    """Quais tickers varrer AGORA: cripto sempre; ação só com o mercado ativo.

    ``asset_type`` ausente conta como AÇÃO — é o default do resto do sistema, e o erro
    seguro aqui é varrer de menos (perde-se cobertura, não se envenena o ledger).
    Sessão desconhecida varre tudo: não saber não é motivo pra deixar de olhar.
    """
    ativos = sessao in SESSOES_ATIVAS or sessao == "desconhecida"
    out = []
    for w in watchlist:
        t = w.get("ticker")
        if not t:
            continue
        cripto = (w.get("asset_type") or "").lower() == "crypto"
        if cripto or ativos:
            out.append(t)
    return out


def sessao_de_mercado(watchlist: list[dict[str, Any]],
                      cotacao: Callable[[str], dict | None]) -> tuple[str, str | None]:
    """``(sessao, ticker_de_referência)`` das AÇÕES desta passada.

    UMA cotação por passada, não vinte: o objetivo é saber se a bolsa está de portas
    abertas, e isso é o mesmo para toda a watchlist de ações. Sem ação na lista, ou sem
    cotação, devolve ``desconhecida`` — e ``alvos_da_passada`` varre tudo, porque não
    saber não pode virar "não olhar".
    """
    ref = next((w.get("ticker") for w in watchlist
                if w.get("ticker") and (w.get("asset_type") or "stock").lower() != "crypto"),
               None)
    if not ref:
        return "24h", None
    try:
        d = cotacao(ref) or {}
    except Exception:  # noqa: BLE001 — a agenda nunca cai por causa de uma cotação
        return "desconhecida", ref
    return str(d.get("sessao") or "desconhecida"), ref


class AgendaScan:
    """O laço que executa as passadas — um thread, dormindo até o próximo fechamento.

    Dentro do PROCESSO do servidor de propósito: o ledger tem um dono só (o
    ``ScanLog`` do runner, com o seu lock) e a de-duplicação lê o arquivo inteiro antes
    de gravar. Um processo separado escrevendo no mesmo ``scans.jsonl`` reabriria a
    corrida que a de-duplicação existe pra fechar.
    """

    def __init__(self, passada: Callable[[], None],
                 frames: tuple[str, ...] = SCAN_FRAMES,
                 atraso_s: int = ATRASO_POS_FECHAMENTO_S,
                 relogio: Callable[[], datetime] | None = None):
        self._passada = passada
        self._frames = frames
        self._atraso_s = atraso_s
        self._relogio = relogio or (lambda: datetime.now(timezone.utc))
        self._parar = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._laco, name="agenda-scan", daemon=True)
        self._thread.start()
        logger.info("agenda do scan ligada: uma passada a cada %d min (+%ds após o "
                    "fechamento)", cadencia_minutos(self._frames), self._atraso_s)

    def stop(self) -> None:
        self._parar.set()

    def _laco(self) -> None:
        while not self._parar.is_set():
            agora = self._relogio()
            alvo = proxima_passada(agora, self._frames, self._atraso_s)
            espera = max(1.0, (alvo - agora).total_seconds())
            if self._parar.wait(espera):
                return
            try:
                self._passada()
            except Exception:  # noqa: BLE001 — uma passada ruim não mata a agenda
                logger.warning("passada agendada do scan falhou", exc_info=True)

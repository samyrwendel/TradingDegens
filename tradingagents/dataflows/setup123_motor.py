"""Setup123 do TradingDegens = o MOTOR do Nest Egg (DA-024). Importado, nunca copiado.

A única fonte de Setup123 da frota é ``nest_egg.motor.setup123`` (regra v2 da DAS-345:
vela 1 de queda, vela 2a de queda que perde a mínima da 1, até 2 velas 2b, vela 3 que
FECHA acima do gatilho; furar a mínima da 2a invalida; stop da posição DAS-347:
D = mín. 2a − 1×ATR14, S = entrada − 8%). Este módulo só faz a ponte: busca as MESMAS
velas que o painel do Nest Egg usa (``nest_egg.motor.sr.candles`` — preço BRUTO, sem
ajuste de dividendo, igual à tela da corretora), corta pela data pedida, separa a vela
em formação e chama o motor do mesmo jeito que o relay (``relay_server`` /setup123).

O motor só decide em D e S (DAS-326): qualquer outro frame devolve ``None``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from tradingagents.dataflows.symbol_utils import crypto_base

logger = logging.getLogger(__name__)

FRAMES = ("1d", "1w")
_DUR_CRIPTO = {"1d": 86400, "1w": 7 * 86400}


def _motor():
    """Import TARDIO: o Nest Egg é instalado editável no venv (``uv pip install --no-deps -e
    /opt/projects/nest-egg``) e um ``uv sync`` o remove — sem ele, o 123 falha POR CHAMADA
    (o plano degrada e loga), em vez de derrubar o servidor inteiro no import."""
    from nest_egg.motor import setup123 as motor, sr as motor_sr  # noqa: PLC0415
    return motor, motor_sr


def _ticker_motor(symbol: str) -> tuple[str, bool]:
    """``BTC-USD`` (TD) → ``BTC/USDT`` (motor: Binance, igual ao Nest Egg); ação fica igual."""
    base = crypto_base(symbol)
    return (f"{base}/USDT", True) if base else (symbol.upper(), False)


def _velas_brutas(symbol: str, curr_date: str, frame: str) -> list[list[float]]:
    """``[[t, o, h, lo, c, v], ...]`` da fonte do motor. Seam: a suíte troca por série sintética."""
    tk, _ = _ticker_motor(symbol)
    return _motor()[1].candles(tk, frame)["velas"]


def _corta(velas: list, curr_date: str, frame: str, cripto: bool) -> tuple[list, list]:
    """``(todas, fechadas)`` até ``curr_date``. Data passada: só o que já tinha FECHADO
    naquele dia (date-guard). Hoje: a régua de fechamento do próprio motor."""
    fim = datetime.strptime(str(curr_date)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    hoje = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    if fim < hoje:
        # 1d: barra do dia ≤ data; 1w: a semana (carimbo = segunda) fecha na sexta.
        # ponytail: cripto semanal fecha domingo — 2 dias de folga a mais numa data passada.
        folga = timedelta(days=4 if frame == "1w" else 0)
        corte = [v for v in velas
                 if datetime.fromtimestamp(v[0], tz=timezone.utc) + folga <= fim]
        return corte, corte
    if cripto:
        agora = datetime.now(timezone.utc).timestamp()
        return velas, velas[:-1] if velas and velas[-1][0] + _DUR_CRIPTO[frame] > agora else velas
    return velas, _motor()[0].velas_fechadas(velas, frame)


def _data(velas: list, i: int | None, frame: str) -> str | None:
    """Data da vela ``i`` na convenção do TD: o dia (1d) ou o DOMINGO que fecha a semana
    (1w, rótulo ``W-SUN`` do gráfico do TD — o motor carimba a segunda)."""
    if i is None or i >= len(velas):
        return None
    d = datetime.fromtimestamp(velas[i][0], tz=timezone.utc)
    return (d + timedelta(days=6 if frame == "1w" else 0)).strftime("%Y-%m-%d")


def leitura(symbol: str, curr_date: str, frame: str) -> dict[str, Any] | None:
    """O 123 do motor na ponta (``estado_atual``) com o stop da posição — o mesmo
    conjunto que o relay do Nest Egg publica. ``None``: frame fora de D/S, série curta
    ou nenhum setup vivo/recente. Exceção de fonte sobe (o chamador degrada)."""
    if frame not in FRAMES:
        return None
    motor = _motor()[0]
    _, cripto = _ticker_motor(symbol)
    todas, velas = _corta(_velas_brutas(symbol, curr_date, frame), curr_date, frame, cripto)
    if len(velas) < 2:
        return None
    o, h, lo, c = ([v[k] for v in velas] for k in (1, 2, 3, 4))
    t = len(velas) - 1
    lo_aberta = motor.minima_aberta(todas, velas)
    e = motor.estado_atual(o, h, lo, c, t, lo_aberta, frame=frame)
    if not e:
        return None
    estado = "ARMADO" if motor.armado_agora(e, todas, velas, frame, cripto=cripto) else e["estado"]
    stop = motor.stop_posicao(e, o, h, lo, c, frame)
    entrada = e["entrada"] if e["entrada"] is not None else e["gatilho"]
    return {
        "estado": estado,
        "fase": e.get("fase"),
        "gatilho": round(e["gatilho"], 2),
        "invalidacao": round(e["stop"], 2),          # mínima da vela 2a (DAS-345)
        "stop_posicao": round(stop, 2) if stop is not None else None,   # DAS-347
        "entrada": round(entrada, 2),
        "confirmou": e["i3"] is not None,
        "vela1": {"date": _data(velas, e["i1"], frame), "price": round(lo[e["i1"]], 2)},
        "vela2": {"date": _data(velas, e["i2"], frame), "price": round(lo[e["i2"]], 2)},
        "vela2b": [_data(velas, i, frame) for i in e["i2b"]],
        "vela3": ({"date": _data(velas, e["i3"], frame), "price": round(c[e["i3"]], 2)}
                  if e["i3"] is not None else None),
        "fim_em": _data(velas, e["fim"], frame) if e.get("fim") is not None and e["fim"] <= t else (
            _data(todas, e["fim"], frame) if e.get("fim") is not None else None),
        "frame": frame,
        "cripto": cripto,
        "fonte": "motor Nest Egg (nest_egg.motor.setup123)",
    }

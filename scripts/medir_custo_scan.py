"""Mede o custo REAL de uma varredura da watchlist — tempo e requisições."""
import json
import sys
import time

sys.path.insert(0, "/home/clawd/tradingagents")

from tradingagents.dataflows import live_price as lp, price_structure as ps
from tradingagents.webui import scanner as sc, timeutil

_WATCH = "/home/clawd/.tradingagents/logs/webui/watchlist.json"
with open(_WATCH, encoding="utf-8") as _fh:
    TICKERS = [t["ticker"] for t in json.load(_fh)["tickers"]]
HOJE = timeutil.today()

contas = {"load_ohlcv": 0, "load_intraday": 0, "live": 0}
_lo, _li, _lv = ps.load_ohlcv, ps.load_intraday_ohlcv, lp.fetch_live_price


def load_ohlcv(*a, **k):
    contas["load_ohlcv"] += 1
    return _lo(*a, **k)


def load_intraday(*a, **k):
    contas["load_intraday"] += 1
    return _li(*a, **k)


def live(*a, **k):
    contas["live"] += 1
    return _lv(*a, **k)


ps.load_ohlcv = load_ohlcv
ps.load_intraday_ohlcv = load_intraday
lp.fetch_live_price = live

print(f"watchlist: {len(TICKERS)} ativos · frames {sc.SCAN_FRAMES}")
for rodada in ("FRIA (cache do processo vazio)", "QUENTE (logo em seguida)"):
    contas.update(dict.fromkeys(contas, 0))
    t = time.time()
    r = sc.scan_watchlist(TICKERS, HOJE)
    dt = time.time() - t
    print(f"\n{rodada}")
    print(f"  tempo: {dt:.1f}s")
    print(f"  chamadas ao loader diário:     {contas['load_ohlcv']}")
    print(f"  chamadas ao loader intradiário:{contas['load_intraday']}")
    print(f"  cotações live:                 {contas['live']}")
    print(f"  resumo dos estados: {r['resumo']}")

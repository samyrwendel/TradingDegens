"""Backtest do TradingAgents no BE: veredito em datas passadas x retorno posterior."""
import json, os, sys, time, traceback
from datetime import datetime
import yfinance as yf
from langchain_core.callbacks import UsageMetadataCallbackHandler
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

TICKER = "BE"
DATES = ["2025-09-15","2025-12-15","2026-03-16","2026-04-15","2026-06-15"]
OUT = "/tmp/be-backtest/results-3rodadas.json"
CAP = 0.80
os.makedirs("/tmp/be-backtest", exist_ok=True)

PRICE = {"gpt-4o-mini-2024-07-18": (0.15/1e6, 0.60/1e6),
         "gpt-4.1-mini-2025-04-14": (0.40/1e6, 1.60/1e6)}
# gpt-5.6-sol: preço não publicado aqui — estimativa conservadora p/ não subestimar
def cost_of(u):
    c = 0.0
    for m, d in (u or {}).items():
        pin, pout = PRICE.get(m, (2.0/1e6, 8.0/1e6))
        c += d.get("input_tokens", 0)*pin + d.get("output_tokens", 0)*pout
    return c

hist = yf.Ticker(TICKER).history(period="2y", interval="1d")
hist.index = hist.index.tz_localize(None)
def px_on(d):
    s = hist.loc[:d, "Close"]
    return float(s.iloc[-1]) if len(s) else None
def fwd_ret(d, days):
    import pandas as pd
    after = hist.loc[d:, "Close"]
    if len(after) < 2: return None
    base = float(after.iloc[0])
    idx = min(days, len(after)-1)
    return (float(after.iloc[idx])/base - 1)*100

MEM_DIR = "/tmp/be-backtest/mem3"
os.makedirs(MEM_DIR, exist_ok=True)
cfg = dict(DEFAULT_CONFIG)
# gpt-5.6-sol como juiz (deep) — não aceita temperature != 1, então deixamos o default
cfg.update({"deep_think_llm":"gpt-5.6-sol","quick_think_llm":"gpt-4o-mini",
            "max_debate_rounds":3,"max_risk_discuss_rounds":2})
cfg.pop("temperature", None)
results, total = [], 0.0
if os.path.exists(OUT):
    prev = json.load(open(OUT)); results = prev.get("results", []); total = prev.get("total_cost", 0.0)
done = {r["date"] for r in results}

for d in DATES:
    if d in done:
        print(f"[skip] {d}", flush=True); continue
    if total > CAP:
        print(f"[STOP] cap ${CAP} atingido", flush=True); break
    print(f"[run] {d} cum=${total:.4f}", flush=True)
    cb = UsageMetadataCallbackHandler()
    # ISOLAMENTO: cada data roda com memória própria e VAZIA — sem lição de outra data,
    # sem vazamento de futuro. Mede o modelo puro, sem o viés que ele cultivou.
    mem_path = os.path.join(MEM_DIR, f"mem_{d}.md")
    if os.path.exists(mem_path):
        os.remove(mem_path)
    cfg["memory_log_path"] = mem_path
    try:
        g = TradingAgentsGraph(config=cfg, debug=False)
        state, decision = g.propagate(TICKER, d, callbacks=[cb])
        c = cost_of(cb.usage_metadata); total += c
        results.append({"date": d, "signal": decision if isinstance(decision,str) else str(decision),
                        "final": state.get("final_trade_decision",""),
                        "price": px_on(d), "ret_30d": fwd_ret(d,21), "ret_60d": fwd_ret(d,42),
                        "cost": round(c,4)})
    except TypeError:
        cfg["memory_log_path"] = mem_path
        g = TradingAgentsGraph(config=cfg, debug=False)
        state, decision = g.propagate(TICKER, d)
        results.append({"date": d, "signal": str(decision), "final": state.get("final_trade_decision",""),
                        "price": px_on(d), "ret_30d": fwd_ret(d,21), "ret_60d": fwd_ret(d,42), "cost": None})
    except Exception as e:
        print(f"[erro] {d}: {e}", flush=True)
        results.append({"date": d, "signal": "ERRO", "error": str(e)[:200],
                        "price": px_on(d), "ret_30d": fwd_ret(d,21), "ret_60d": fwd_ret(d,42)})
    json.dump({"ticker":TICKER,"results":results,"total_cost":round(total,4)}, open(OUT,"w"), indent=1)
    print(f"[ok] {d} -> {results[-1].get('signal')} | ret30 {results[-1].get('ret_30d')}", flush=True)
print(f"[FIM] {len(results)} datas | ${total:.4f}", flush=True)

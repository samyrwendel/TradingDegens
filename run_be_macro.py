"""BE com macro ligado — mesma config de ontem, só o FRED muda."""
import json, os
from datetime import datetime
from langchain_core.callbacks import UsageMetadataCallbackHandler
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

PRICE = {"gpt-4o-mini-2024-07-18": (0.15/1e6, 0.60/1e6),
         "gpt-4.1-mini-2025-04-14": (0.40/1e6, 1.60/1e6)}
def cost_of(u):
    return sum(d.get("input_tokens",0)*PRICE.get(m,(2.0/1e6,8.0/1e6))[0] +
               d.get("output_tokens",0)*PRICE.get(m,(2.0/1e6,8.0/1e6))[1] for m,d in (u or {}).items())

cfg = dict(DEFAULT_CONFIG)
cfg.update({"deep_think_llm":"gpt-4.1-mini","quick_think_llm":"gpt-4o-mini",
            "max_debate_rounds":1,"max_risk_discuss_rounds":1,"temperature":0.0,
            "memory_log_path":"/tmp/be-macro/mem.md"})
os.makedirs("/tmp/be-macro", exist_ok=True)
cb = UsageMetadataCallbackHandler()
g = TradingAgentsGraph(config=cfg, debug=False)
state, decision = g.propagate("BE", "2026-08-23")
out = {"signal": str(decision), "final": state.get("final_trade_decision",""),
       "plan": state.get("investment_plan",""), "news": state.get("news_report",""),
       "cost": round(cost_of(cb.usage_metadata),4)}
json.dump(out, open("/tmp/be-macro/result.json","w"), indent=1)
print("SIGNAL:", out["signal"], "| custo $%.4f" % out["cost"], flush=True)

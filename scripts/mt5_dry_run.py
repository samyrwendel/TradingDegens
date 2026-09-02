"""DRY RUN persistido pro MT5 via Meridian — o modo PADRÃO da escada de execução.
ESTUDO-MERIDIAN-INTEGRACAO.md §14-§16, task 20260901-040.

A ESCADA é DRY RUN → DEMO → (REAL não existe — bloqueado pelo Meridian).
Este script é o DRY RUN: percorre o caminho INTEIRO que um sinal em_gatilho
faria até virar ordem no Meridian — símbolo mapeado, lote pela banca fixa,
SL/TP, magic number e comment por ESTRATÉGIA (270101/Setup123,
270102/Storm123 — DA-039) — e REGISTRA o payload completo + o veredito de
validação num log append-only, em vez de enviar qualquer coisa.

NÃO conecta em corretora, NÃO abre o MT5, NÃO chama o Meridian. Só lê o scan
já salvo do TradingDegens (`/api/scan/salvo`, público, $0) e escreve em disco,
neste servidor. É a evolução de `mt5_sinais_dry_run.py` (task 036): aquele só
imprimia; este REGISTRA — o que o pedido chama de "prestar contas".

VALIDAÇÃO É PARCIAL, E ISSO É DECLARADO NO PRÓPRIO REGISTRO. Rodando só neste
servidor Debian, sem conexão com o terminal MT5 (que vive no samyr-srv), dá
pra conferir a ARITMÉTICA (o símbolo tem mapeamento? o lote calculado é
positivo? a direção é válida?) mas NÃO dá pra conferir contra a corretora de
verdade (o símbolo existe mesmo? o lote respeita mínimo/passo? a distância de
stop é aceita? o mercado está aberto agora?) — isso exige o agente do
Meridian, que já faz `mt5.order_check()` antes de qualquer `order_send()`
(`xm_mt5_bridge.py:671-674`). A PROPOSTA de como pedir essa validação real ao
agente, sem nunca enviar a ordem, está na §15 do estudo — não implementada
aqui, porque mexe no repositório do Meridian, não neste.
"""
import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_URL = "http://127.0.0.1:8781/api/scan/salvo"
_BANCA_USD = 100.0
_LOG = Path(os.path.expanduser("~/.tradingagents/logs/webui/mt5_dry_run.jsonl"))

# Confiança atualizada pela pesquisa específica do XM (ESTUDO-MERIDIAN-
# INTEGRACAO.md §3) — LINK subiu de "media" pra "alta" em relação à task 036.
_MAPA_MT5 = {
    "AAPL":    {"simbolo": "AAPL",     "confianca": "alta",  "unidade_lote": 1},
    "MSFT":    {"simbolo": "MSFT",     "confianca": "alta",  "unidade_lote": 1},
    "NVDA":    {"simbolo": "NVDA",     "confianca": "alta",  "unidade_lote": 1},
    "AMD":     {"simbolo": "AMD",      "confianca": "alta",  "unidade_lote": 1},
    "INTC":    {"simbolo": "INTC",     "confianca": "alta",  "unidade_lote": 1},
    "IBM":     {"simbolo": "IBM",      "confianca": "alta",  "unidade_lote": 1},
    "GOOGL":   {"simbolo": "GOOGL",    "confianca": "alta",  "unidade_lote": 1},
    "AVGO":    {"simbolo": "AVGO",     "confianca": "media", "unidade_lote": 1},
    "CRWD":    {"simbolo": "CRWD",     "confianca": "media", "unidade_lote": 1},
    "TSM":     {"simbolo": "TSM",      "confianca": "media", "unidade_lote": 1},
    "MRVL":    {"simbolo": "MRVL",     "confianca": "baixa", "unidade_lote": 1},
    "SNDK":    {"simbolo": "SNDK",     "confianca": "baixa", "unidade_lote": 1},
    "MP":      {"simbolo": "MP",       "confianca": "baixa", "unidade_lote": 1},
    "AAOI":    {"simbolo": "AAOI",     "confianca": "baixa", "unidade_lote": 1},
    "BE":      {"simbolo": "BE",       "confianca": "baixa", "unidade_lote": 1},
    "EOSE":    {"simbolo": "EOSE",     "confianca": "baixa", "unidade_lote": 1},
    "SPCX":    {"simbolo": "?",        "confianca": "desconhecida", "unidade_lote": None},
    "BTC-USD": {"simbolo": "BTCUSD",   "confianca": "alta",  "unidade_lote": 1},
    "LINK-USD": {"simbolo": "LINKUSD", "confianca": "alta",  "unidade_lote": 1},
    "ZEC-USD": {"simbolo": "?",        "confianca": "baixa", "unidade_lote": None},
}

# Magic/comment POR ESTRATÉGIA (DA-039 corrige o magic único da task 038) — a
# faixa 270101-270110 é do TradingDegens, longe do 260811 do Pyr-Cycle de
# propósito (não colidir nem confundir de relance no histórico do MT5).
_ESTRATEGIA = {
    "123": {"strategy_id": "tradingdegens-setup123", "magic": 270101,
            "comment": "Meridian:tradingdegens-setup123"},
    "storm": {"strategy_id": "tradingdegens-storm123", "magic": 270102,
              "comment": "Meridian:tradingdegens-storm123"},
}


def _sinais_em_gatilho(snapshot):
    """(ticker, frame, dono, linha) de cada estado 'em_gatilho' no snapshot —
    dono é '123' (linha do topo) ou 'storm' (sub-objeto)."""
    for ativo in snapshot.get("ativos") or []:
        ticker = ativo.get("ticker")
        for f in ativo.get("frames") or []:
            if f.get("estado") == "em_gatilho":
                yield ticker, f.get("frame"), "123", f
            storm = f.get("storm") or {}
            if storm.get("estado") == "em_gatilho":
                yield ticker, f.get("frame"), "storm", storm


def _payload(ticker, frame, dono, linha):
    """O payload COMPLETO que iria à fila do Meridian, mais o veredito de
    validação — PARCIAL, porque este servidor não tem conexão com o MT5."""
    mapa = _MAPA_MT5.get(ticker, {"simbolo": "?", "confianca": "desconhecida", "unidade_lote": None})
    estrategia = _ESTRATEGIA[dono]
    trigger = linha.get("trigger")
    sl, tp = linha.get("sl"), linha.get("tp")
    direction = linha.get("direction")

    checado = {
        "simbolo_tem_mapeamento": mapa["simbolo"] != "?",
        "direcao_valida": direction in ("compra", "venda"),
        "niveis_presentes": all(v is not None for v in (trigger, sl, tp)),
    }
    lote = None
    if checado["simbolo_tem_mapeamento"] and checado["niveis_presentes"] and trigger:
        lote = round(_BANCA_USD / (trigger * mapa["unidade_lote"]), 4)
    checado["lote_calculado_e_positivo"] = bool(lote and lote > 0)

    nao_checado = [
        "o símbolo existe de verdade na corretora conectada",
        "lote respeita mínimo/máximo/passo do símbolo",
        "distância de SL/TP respeita o mínimo de stops (trade_stops_level)",
        "mercado aberto / símbolo não suspenso agora",
        "conta demo online e Algo Trading ligado",
    ]
    # False quando a ARITMÉTICA já reprova (não precisa da corretora pra saber);
    # None quando a aritmética passa mas falta a corretora pra confirmar — NUNCA
    # True: aprovação é o que só o `order_check()` do agente pode afirmar.
    aceitaria = False if not all(checado.values()) else None
    motivo = ("validação PARCIAL — este servidor não tem conexão com o agente MT5 "
              "(roda só no samyr-srv); os itens acima não puderam ser conferidos. "
              "A aritmética própria do TradingDegens foi conferida (abaixo).") \
        if all(checado.values()) else \
        "REJEITARIA sem chegar na corretora — " + ", ".join(
            k for k, v in checado.items() if not v)

    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "modo": "dry_run",
        "ticker": ticker, "frame": frame,
        "strategy_id": estrategia["strategy_id"],
        "mt5_simbolo": mapa["simbolo"] if checado["simbolo_tem_mapeamento"] else None,
        "confianca_mapeamento": mapa["confianca"],
        "direcao": direction,
        "entrada_gatilho": trigger, "sl_preco": sl, "tp_preco": tp,
        # PONTOS exigem o `point` do símbolo (casas decimais do preço na
        # corretora) — não temos isso sem o terminal conectado. Publicamos a
        # DISTÂNCIA EM PREÇO, que é o que o TradingDegens sabe de verdade; a
        # conversão pra pontos é passo do agente (ESTUDO §2), não daqui.
        "sl_distancia_preco": round(abs(trigger - sl), 6) if trigger and sl else None,
        "tp_distancia_preco": round(abs(tp - trigger), 6) if trigger and tp else None,
        "banca_alvo_usd": _BANCA_USD,
        "lote_calculado": lote,
        "magic": estrategia["magic"], "comment": estrategia["comment"],
        "validacao": {
            "aceitaria": aceitaria,
            "checado_aqui": checado,
            "nao_checado_precisa_do_agente_mt5": nao_checado,
            "motivo": motivo,
        },
    }


def _ja_registrado_hoje(chaves_existentes, chave):
    return chave in chaves_existentes


def _chave(registro):
    dia = registro["ts"][:10]
    return f"{registro['ticker']}|{registro['frame']}|{registro['strategy_id']}|{registro['entrada_gatilho']}|{dia}"


def _le_chaves_existentes():
    if not _LOG.exists():
        return set()
    chaves = set()
    with open(_LOG, encoding="utf-8") as fh:
        for line in fh:
            try:
                chaves.add(_chave(json.loads(line)))
            except (json.JSONDecodeError, KeyError):
                continue
    return chaves


def main():
    with urllib.request.urlopen(_URL, timeout=10) as resp:
        snapshot = json.load(resp)
    print(f"scan lido de {_URL} — gerado_em {snapshot.get('gerado_em')}")
    print("MODO: DRY RUN. Nenhuma ordem é enviada. Nenhuma conexão MT5 é aberta.\n")

    sinais = list(_sinais_em_gatilho(snapshot))
    if not sinais:
        print("nenhum em_gatilho neste snapshot agora — nada a registrar.")
        return

    existentes = _le_chaves_existentes()
    _LOG.parent.mkdir(parents=True, exist_ok=True)
    novos = 0
    with open(_LOG, "a", encoding="utf-8") as fh:
        for ticker, frame, dono, linha in sinais:
            registro = _payload(ticker, frame, dono, linha)
            chave = _chave(registro)
            print(json.dumps(registro, indent=2, ensure_ascii=False))
            if _ja_registrado_hoje(existentes, chave):
                print("  (já registrado hoje — não duplicado no log)\n")
                continue
            fh.write(json.dumps(registro, ensure_ascii=False) + "\n")
            existentes.add(chave)
            novos += 1
            print()
    print(f"registrados {novos} sinal(is) novo(s) em {_LOG}")


if __name__ == "__main__":
    main()

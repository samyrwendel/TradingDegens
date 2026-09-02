"""Ponte sinal→MT5, em MODO SIMULAÇÃO — ESTUDO-MT5-SINAIS.md, task 20260901-036.

NÃO conecta em corretora nenhuma, NÃO abre o terminal MT5, NÃO envia ordem
nenhuma — nem em conta demo. Lê o scan JÁ SALVO em disco (`/api/scan/salvo`,
leitura pública, $0), acha os `em_gatilho` de agora, traduz cada um pro
FORMATO que uma ordem MT5 pediria (símbolo mapeado, lote pela banca fixa,
SL/TP) e IMPRIME o que teria sido mandado. É a prova de conceito da TRADUÇÃO
— a parte que não depende de corretora nem de Windows pra existir.

O mapeamento de símbolo é uma ESTIMATIVA (ver a tabela de confiança no
estudo) até o Samyr escolher uma corretora de verdade e alguém conferir a
Observação de Mercado (Market Watch) do terminal.
"""
import json
import urllib.request

_URL = "http://127.0.0.1:8781/api/scan/salvo"
_BANCA_USD = 100.0

# ESTIMATIVA — nenhum destes foi conferido numa corretora real (não há conta
# aberta). "?" = confiança baixa ou símbolo não identificado com segurança.
# unidade_lote = quanto DE UMA UNIDADE (1 ação, ou 1 moeda) um "lote 1.0"
# representa no MT5 — placeholder até a corretora escolhida declarar o dela
# (ações costumam ser 1 ou 100; cripto varia MUITO, ver o estudo).
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
    "LINK-USD": {"simbolo": "LINKUSD", "confianca": "media", "unidade_lote": 1},
    "ZEC-USD": {"simbolo": "?",        "confianca": "baixa", "unidade_lote": None},
}

# Lote MÍNIMO típico declarado por corretoras MT5 pra CFD de cripto — 0.01 é o
# valor mais citado no mercado (NÃO medido numa corretora real). Serve só pra
# mostrar o ACHADO: em BTC, 0.01 lote de 1 BTC/lote já estoura os US$100.
_LOTE_MINIMO_TIPICO_CRIPTO = 0.01


def _sinais_em_gatilho(snapshot):
    """(ticker, frame, dono, linha) de cada estado 'em_gatilho' no snapshot —
    dono é '123' (linha do topo) ou 'storm' (sub-objeto), mesma distinção que
    o ledger (scanner.SETUPS_DO_LEDGER) já usa."""
    for ativo in snapshot.get("ativos") or []:
        ticker = ativo.get("ticker")
        for f in ativo.get("frames") or []:
            if f.get("estado") == "em_gatilho":
                yield ticker, f.get("frame"), "123", f
            storm = f.get("storm") or {}
            if storm.get("estado") == "em_gatilho":
                yield ticker, f.get("frame"), "storm", storm


def _ordem_mt5(ticker, frame, dono, linha):
    mapa = _MAPA_MT5.get(ticker, {"simbolo": "?", "confianca": "desconhecida", "unidade_lote": None})
    trigger = linha.get("trigger")
    sl, tp = linha.get("sl"), linha.get("tp")
    if mapa["simbolo"] == "?" or mapa["unidade_lote"] is None or not trigger:
        return {"ticker": ticker, "mt5_simbolo": None, "motivo": "sem mapeamento confiável — NÃO enviaria"}
    lote = round(_BANCA_USD / (trigger * mapa["unidade_lote"]), 4)
    aviso_lote_min = (lote < _LOTE_MINIMO_TIPICO_CRIPTO
                      if ticker.endswith("-USD") else False)
    return {
        "ticker": ticker, "frame": frame, "dono_do_sinal": dono,
        "mt5_simbolo": mapa["simbolo"], "confianca_mapeamento": mapa["confianca"],
        "direcao": linha.get("direction"), "entrada_gatilho": trigger,
        "sl": sl, "tp": tp,
        "banca_alvo_usd": _BANCA_USD,
        "lote_calculado": lote,
        "abaixo_do_lote_minimo_tipico": aviso_lote_min,
    }


def main():
    with urllib.request.urlopen(_URL, timeout=10) as resp:
        snapshot = json.load(resp)
    print(f"scan lido de {_URL} — gerado_em {snapshot.get('gerado_em')}")
    print("MODO: SIMULAÇÃO. Nenhuma ordem é enviada. Nenhuma conexão MT5 é aberta.\n")

    sinais = list(_sinais_em_gatilho(snapshot))
    if not sinais:
        print("nenhum em_gatilho neste snapshot agora — nada a traduzir.")
        return
    for ticker, frame, dono, linha in sinais:
        ordem = _ordem_mt5(ticker, frame, dono, linha)
        print(json.dumps(ordem, indent=2, ensure_ascii=False))
        print()


if __name__ == "__main__":
    main()

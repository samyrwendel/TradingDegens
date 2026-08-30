"""Quando o número começa a valer (task 20260830-037, DA-104): dias até n=5 e n=20 do gate, por setup.

Não é projeção de gosto — sai do ledger REAL (`scans.jsonl`) com as premissas escritas.
"""
import collections
import json

L = "/home/clawd/.tradingagents/logs/webui/scans.jsonl"
N_MINIMO, N_OPERAVEL = 5, 20

gat, fech = [], []
with open(L, encoding="utf-8") as fh:
    for line in fh:
        try:
            o = json.loads(line)
        except Exception:  # noqa: BLE001 — linha corrompida não derruba a estimativa
            continue
        (fech if o.get("tipo") == "fechamento" else gat).append(o)
gat = [g for g in gat if not g.get("tipo")]

por_setup = collections.Counter(g.get("setup", "123") for g in gat)
dias_setup = collections.defaultdict(set)
for g in gat:
    dias_setup[g.get("setup", "123")].add(str(g["ts"])[:10])

print(f"LEDGER REAL — {len(gat)} gatilhos, {len(fech)} fechamentos")
print(f"  janela: {min(str(g['ts'])[:10] for g in gat)} → {max(str(g['ts'])[:10] for g in gat)}")
for s in ("123", "storm"):
    d = len(dias_setup[s]) or 1
    print(f"  {s:6s}: {por_setup[s]:3d} gatilhos em {d} dia(s) → {por_setup[s]/d:.1f}/dia")

conv = len(fech) / len(gat) if gat else 0
print("\nPREMISSAS (declaradas, e conservadoras):")
print(f"  · conversão gatilho→fechamento observada: {len(fech)}/{len(gat)} = {conv:.0%}")
print("    (é PISO: gatilho que ainda não bateu TP/SL vai fechar depois — o tempo real")
print("     é MENOR que o estimado aqui)")
print("  · a agenda mantém, no mínimo, o ritmo de gatilhos observado (ela só ADICIONA")
print("    cobertura: hoje o scan só roda quando alguém abre a tela)")

print("\nDIAS ATÉ O GATE (n_fechados por setup):")
for s in ("123", "storm"):
    d = len(dias_setup[s]) or 1
    por_dia = por_setup[s] / d
    fech_dia = por_dia * conv
    if fech_dia <= 0:
        print(f"  {s:6s}: sem base pra estimar")
        continue
    print(f"  {s:6s}: {fech_dia:.1f} fechamento(s)/dia → "
          f"n={N_MINIMO} em ~{N_MINIMO/fech_dia:.0f} dias · "
          f"n={N_OPERAVEL} em ~{N_OPERAVEL/fech_dia:.0f} dias")

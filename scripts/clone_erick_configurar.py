#!/usr/bin/env python3
"""Configura capital + moeda do clone da carteira do Erick, e ATIVA na hora.

O COMANDO que a task 20260902-064 pediu — trocar capital/moeda depois é rodar
isto de novo, nunca editar o estado na mão nem inferir do histórico (DA-173).

Uso:
    python3 scripts/clone_erick_configurar.py <capital> <moeda>

O que faz, em ordem:
  1. configurar_capital(capital, moeda) — grava e (RE)ARMA a baseline (a
     história recomeça do zero a partir daqui; nenhuma posição atual do Erick
     é herdada — DA-173, dente test_dente_ativacao_nao_semeia_a_carteira_atual).
  2. Busca a leitura REAL da carteira dele (erick_carteira.carteira() — o
     MESMO caminho que o timer horário usa, cache de até 1h ou fetch ao vivo;
     NUNCA um snapshot fabricado).
  3. clone_erick.observar(atual) com essa leitura real — na primeira chamada
     pós-ativação isto GRAVA A BASELINE e não opera nada (mesmo efeito que o
     próximo disparo do timer teria; só não espera a hora virar).
  4. Imprime o estado final como prova.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradingagents.dataflows import erick_carteira as ec  # noqa: E402
from tradingagents.webui import clone_erick as C  # noqa: E402


def main() -> int:
    if len(sys.argv) != 3:
        print(f"Uso: {sys.argv[0]} <capital> <moeda>", file=sys.stderr)
        return 2
    capital = float(sys.argv[1])
    moeda = sys.argv[2]

    C.configurar_capital(capital, moeda)
    est_pos_config = C.estado()
    print("── configurado ──")
    print(f"  estado: {est_pos_config['estado']}")
    print(f"  capital: {est_pos_config['capital']}")
    print(f"  moeda: {est_pos_config['moeda']}")
    print(f"  ativado_em: {est_pos_config['ativado_em']}")
    print(f"  baseline_definida (antes da 1a leitura): {est_pos_config['baseline_definida']}")

    atual = ec.carteira()
    if not atual:
        print("ERRO: erick_carteira.carteira() não devolveu leitura — "
              "capital/moeda ficaram configurados, mas a baseline NÃO foi "
              "gravada (fica pro próximo disparo do timer horário).",
              file=sys.stderr)
        return 1

    r = C.observar(atual)
    print("── primeira observação (deve ser baseline, zero operação) ──")
    print(f"  estado: {r['estado']}")
    print(f"  ops: {r['ops']!r}")
    print(f"  nota: {r.get('nota', '(sem nota — NÃO foi a primeira leitura pós-ativação)')}")

    ledger = C.carrega_ledger()
    print(f"  ledger total de linhas: {len(ledger)}")

    est_final = C.estado()
    print("── estado final ──")
    for k, v in est_final.items():
        print(f"  {k}: {v}")

    if r["ops"]:
        print("ALERTA: a primeira observação gerou operação(ões) — NÃO deveria "
              "(a baseline deveria ter absorvido tudo sem operar). Investigue "
              "antes de confiar no clone.", file=sys.stderr)
        return 1
    if not est_final["baseline_definida"]:
        print("ALERTA: baseline NÃO ficou definida após a observação.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

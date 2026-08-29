#!/usr/bin/env python
"""Purga categorias do cache DA-058 cuja SEMÂNTICA mudou.

Por que existe: quando o significado de uma resposta cacheada muda, a chave ganha
um ``_SEMANTICA_KEY`` novo (ver ``earnings_calendar`` e ``finnhub_earnings``) e as
entradas antigas viram ÓRFÃS — inalcançáveis, porque a chave é um hash, mas eternas
no disco, porque entrada de data passada é gravada permanente. Foi o que sobrou do
C3: 43 arquivos órfãos na categoria ``earnings_next``, 20 deles envenenados.

Como a chave não guarda a versão em claro, não há como apagar SÓ as órfãs: purga-se
a categoria inteira. O custo é re-buscar o que ainda valia (uma vez); o benefício é
não carregar resposta de uma semântica morta.

Uso:
    python scripts/purge_cache_semantica.py --listar
    python scripts/purge_cache_semantica.py earnings_next earnings_reported
    python scripts/purge_cache_semantica.py --todas

RODE ISTO no mesmo commit em que bumpar um ``_SEMANTICA_KEY``.
"""
from __future__ import annotations

import argparse
import os
import sys

from tradingagents.datacache import cache


def _categorias() -> list[tuple[str, int]]:
    try:
        nomes = sorted(os.listdir(cache.CACHE_DIR))
    except OSError:
        return []
    out = []
    for nome in nomes:
        d = os.path.join(cache.CACHE_DIR, nome)
        if not os.path.isdir(d):
            continue
        try:
            n = len([x for x in os.listdir(d) if x.endswith(".json")])
        except OSError:
            n = 0
        out.append((nome, n))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("categorias", nargs="*", help="categorias a purgar")
    ap.add_argument("--listar", action="store_true", help="só lista o que existe")
    ap.add_argument("--todas", action="store_true", help="purga TODAS as categorias")
    args = ap.parse_args(argv)

    inventario = _categorias()
    if args.listar or (not args.categorias and not args.todas):
        print(f"cache em {cache.CACHE_DIR}")
        for nome, n in inventario:
            print(f"  {nome:<28} {n:>6} entrada(s)")
        if not args.listar:
            print("\nnada purgado — passe categorias ou --todas")
        return 0

    alvos = [n for n, _ in inventario] if args.todas else args.categorias
    total = 0
    for nome in alvos:
        n = cache.purge_category(nome)
        total += n
        print(f"purgada {nome}: {n} entrada(s)")
    print(f"total: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

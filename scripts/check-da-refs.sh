#!/usr/bin/env bash
# check-da-refs.sh [--all] [dir] — toda citação `DA-NNN` fora do diário aponta para uma DA que DECISIONS.md TEM.
#   padrão (pre-commit): só as linhas ADICIONADAS no staged — modo doc: avisa e sai 0 · modo mecanismo: BLOQUEIA (exit 1)
#   --all: inventário do working tree por número (informativo, sempre exit 0)
#   fora da varredura: DECISIONS.md, DECISIONS-INDEX.md, DECISIONS-arquivo/ (caderno anterior, congelado) e .adas/
#   entrada de caderno anterior se cita como "caderno NNN", nunca como DA-NNN — DA-NNN neste repo é sempre ESTE diário
set -uo pipefail

SELFDIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
MODE=staged; DIR=""
for a in "$@"; do case "$a" in --all) MODE=all ;; -h|--help) sed -n '2,6p' "$0"; exit 0 ;; *) DIR="$a" ;; esac; done
[ -z "$DIR" ] && DIR="$(cd "$SELFDIR/.." && pwd)"
cd "$DIR" || { echo "✗ check-da-refs: diretório '$DIR' inacessível"; exit 2; }
DEC="${DECISIONS:-DECISIONS.md}"
[ -f "$DEC" ] || { echo "ℹ check-da-refs: sem $DEC — nada a conferir"; exit 0; }

MODO=""
[ -f .adas/profile.json ] && MODO="$(grep -oE '"modo"[[:space:]]*:[[:space:]]*"[a-z]+"' .adas/profile.json 2>/dev/null | sed -E 's/.*"([a-z]+)"$/\1/')"
[ "$MODO" = mecanismo ] || MODO=doc

declare -A exists
while read -r n; do [ -n "$n" ] && exists[$((10#$n))]=1; done < <(grep -oE '^## DA-[0-9]+' "$DEC" | grep -oE '[0-9]+')

hits=""
if [ "$MODE" = staged ]; then
  git rev-parse --show-toplevel >/dev/null 2>&1 || { echo "ℹ check-da-refs: sem git — use --all"; exit 0; }
  hits="$(git diff --cached -U0 --diff-filter=AMR -- . ":(exclude)$DEC" ':(exclude)DECISIONS-INDEX.md' ':(exclude)DECISIONS-arquivo' ':(exclude).adas' 2>/dev/null | awk '
    /^\+\+\+ / { f = ($0 ~ /^\+\+\+ b\//) ? substr($0, 7) : ""; next }
    /^@@/      { split($0, a, " "); split(a[3], b, ","); ln = substr(b[1], 2) + 0; next }
    /^\+/      { if (f != "") { s = $0; while (match(s, /DA-[0-9]{3}/)) { print f ":" ln ":" substr(s, RSTART, RLENGTH); s = substr(s, RSTART + RLENGTH) } } ln++; next }
    /^-/       { next }
               { ln++ }')"
else
  hits="$(grep -rnoE 'DA-[0-9]{3}' --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=.venv --exclude-dir=DECISIONS-arquivo --exclude-dir=.adas --exclude="$DEC" --exclude=DECISIONS-INDEX.md --exclude='*.lock' . 2>/dev/null | sed 's#^\./##')"
fi

n_total=0; n_bad=0; bad_lines=""; bad_nums=""
while IFS= read -r h; do
  [ -z "$h" ] && continue
  n_total=$((n_total+1))
  num="${h##*DA-}"
  if [ -z "${exists[$((10#$num))]:-}" ]; then
    n_bad=$((n_bad+1)); bad_nums+="DA-$num"$'\n'
    bad_lines+="  ${h%:DA-*}  DA-$num (não existe em $DEC — se é do caderno anterior, escreva 'caderno $num')"$'\n'
  fi
done <<< "$hits"

if [ "$MODE" = all ]; then
  echo "ℹ check-da-refs --all: $n_total citação(ões) DA-NNN no working tree, $n_bad a número que $DEC não tem (inventário, não gate)"
  [ -n "$bad_nums" ] && printf '%s' "$bad_nums" | sort | uniq -c | sort -rn | head -40 | sed 's/^/    /'
  exit 0
fi
if [ "$n_bad" = 0 ]; then echo "✓ check-da-refs: $n_total citação(ões) DA-NNN nas linhas adicionadas — todas existem em $DEC"; exit 0; fi
printf '%s' "$bad_lines"
if [ "$MODO" = mecanismo ]; then echo "✗ check-da-refs: $n_bad citação(ões) a DA que $DEC não tem — modo mecanismo: não entra"; exit 1; fi
echo "• check-da-refs: $n_bad citação(ões) a DA que $DEC não tem — modo doc: aviso, não impede (registre a DA antes de citá-la)"; exit 0

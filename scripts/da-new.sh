#!/usr/bin/env bash
# da-new.sh — a porta única para anexar uma decisão ao DECISIONS.md (número = max+1, nunca reusado;
# lock + snapshot antes de escrever). Escreve o ÚNICO formato que scripts/da-index.sh lê:
#   ## DA-NNN — Título
#   `escopo: x` · `saga: y` · `data: AAAA-MM-DD` · `refs: —` · `supersede: —`
#   **Regra:** / **Motivo:** / **Trade-off:** / **Lição:** / **Decidido por:**
# Uso: da-new.sh <escopo|-> <saga|-> "<título>" [dir]
#   [dir] ausente = raiz do projeto deste script (scripts/..) — nunca o cwd, nunca $HOME.
set -uo pipefail

SELFDIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
usage() { echo 'uso: da-new.sh <escopo|-> <saga|-> "<título>" [dir]'; exit 2; }
[ $# -ge 3 ] || usage
ESCOPO="$1"; SAGA="$2"; TITULO="$3"; DIR="${4:-$(cd "$SELFDIR/.." && pwd)}"
[ "$ESCOPO" = "-" ] && ESCOPO="—"; [ "$SAGA" = "-" ] && SAGA="—"
DEC="$DIR/DECISIONS.md"
[ -f "$DEC" ] || { echo "✗ da-new: $DEC não existe"; exit 2; }

ADAS_DIR="$DIR/.adas"
mkdir -p "$ADAS_DIR/snapshots" 2>/dev/null || true
exec 9>"$ADAS_DIR/da.lock"
flock -w 10 9 || { echo "✗ da-new: não consegui o lock em 10s (outro processo escrevendo?)"; exit 1; }

ts="$(date -u +%Y%m%dT%H%M%SZ)"
cp "$DEC" "$ADAS_DIR/snapshots/DECISIONS.md.$ts" 2>/dev/null || { echo "✗ da-new: snapshot falhou — abortando sem tocar no diário"; exit 1; }
ls -1 "$ADAS_DIR/snapshots"/DECISIONS.md.* 2>/dev/null | sort | head -n -30 | xargs -r rm -f --

max=0
while read -r n; do
  n="$((10#$n))"; [ "$n" -gt "$max" ] && max="$n"
done < <(grep -oE '^## DA-[0-9]+' "$DEC" | grep -oE '[0-9]+')
novokey="DA-$(printf '%03d' "$((max+1))")"

{
  echo ""
  echo "## $novokey — $TITULO"
  echo "\`escopo: $ESCOPO\` · \`saga: $SAGA\` · \`data: $(date +%F)\` · \`refs: —\` · \`supersede: —\`"
  echo "**Regra:** "
  echo "**Motivo:** "
  echo "**Trade-off:** "
  echo "**Lição:** "
  echo "**Decidido por:** "
} >> "$DEC"

echo "✓ da-new: $novokey anexada no fim de $DEC (snapshot: $ADAS_DIR/snapshots/DECISIONS.md.$ts)"
echo "  Preencha Regra/Motivo/Trade-off/Lição/Decidido por dentro de $novokey — nunca em DA anterior. Depois: bash scripts/da-index.sh update"
echo "$novokey"

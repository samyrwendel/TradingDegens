#!/usr/bin/env bash
# adas-report.sh — o estado do ADAS deste projeto, só com o que é MEDÍVEL (nunca inventa "% de aderência").
# Uso: bash scripts/adas-report.sh [dir]
set -uo pipefail
SELFDIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
DIR="${1:-$(cd "$SELFDIR/.." && pwd)}"
cd "$DIR" || exit 2
SKILLS_DIR="${SKILLS_DIR:-.claude/skills}"; SPECS_DIR="${SPECS_DIR:-.specs}"
ADAS="${ADAS:-ADAS.md}"; DECISIONS="${DECISIONS:-DECISIONS.md}"

modo="$(grep -oE '"modo"[[:space:]]*:[[:space:]]*"[a-z]+"' .adas/profile.json 2>/dev/null | sed -E 's/.*"([a-z]+)"$/\1/')"
faixas=$(find "$SKILLS_DIR" -name SKILL.md -not -path '*/_template/*' 2>/dev/null | wc -l | tr -d ' ')
das=$(grep -cE '^## DA-[0-9]+' "$DECISIONS" 2>/dev/null || echo 0)
placeholders=$(grep -rlE --include='*.md' --include='*.css' --exclude-dir=_template '<PLACEHOLDER|<faixa>|<NNN>|<PROJETO>|<nome>' "$SPECS_DIR" "$SKILLS_DIR" "$ADAS" "$DECISIONS" AGENTS.md 2>/dev/null | wc -l | tr -d ' ')
# débito localizado: marcador `adas:` na linha do atalho consciente (comentário de qualquer linguagem)
debt=$(grep -rnE '(#|//|/\*|<!--|--)[[:space:]]*adas:' --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=dist --exclude-dir=.adas --exclude=adas-report.sh . 2>/dev/null | wc -l | tr -d ' ')
health="(check-adas ausente)"
[ -f scripts/check-adas.sh ] && health="$(bash scripts/check-adas.sh . 2>/dev/null | tail -1)"

echo "┌─ ADAS · relatório ────────────────────────────────"
echo "│ MEDÍVEL (contado, não estimado):"
printf "│   modo ................... %s\n" "${modo:-não declarado (adas-init.sh --modo doc|mecanismo)}"
printf "│   faixas ativas .......... %s\n" "$faixas"
printf "│   decisões (DA-NNN) ...... %s\n" "$das"
printf "│   débito marcado (adas:) . %s   (listar: grep -rn 'adas:' --exclude-dir=.git .)\n" "$debt"
printf "│   placeholders pendentes . %s%s\n" "$placeholders" "$([ "$placeholders" != 0 ] && echo '  ⚠ bootstrap incompleto')"
echo "│   saúde (check-adas) ..... $health"
echo "│"
echo "│ NÃO MEDÍVEL aqui (e o ADAS NÃO inventa número):"
echo "│   • \"% de aderência\" / \"% de drift evitado\" — não há baseline do que a LLM"
echo "│     TERIA inventado sem as faixas. Reportar % seria chute."
echo "│   • Impacto REAL = benchmark A/B: mesma tarefa COM × SEM as faixas (LoC, custo, retrabalho)."
echo "└───────────────────────────────────────────────────"

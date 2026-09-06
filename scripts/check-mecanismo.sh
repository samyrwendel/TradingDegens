#!/usr/bin/env bash
# check-mecanismo.sh — o contrato do MODO MECANISMO: invariante sem gatilho NÃO entra.
#
# Lê as linhas numeradas do núcleo do ADAS.md (entre <!-- adas-core-start/end -->):
#   N. **<regra>** — mecanismo: <arquivo> · teste: <arquivo>
# e exige, por invariante:
#   mecanismo: arquivo que EXISTE e está REGISTRADO em algum lugar que o executa
#              (.claude/settings.json, pre-commit instalado, scripts/install-hooks.sh,
#              package.json, Makefile, .github/workflows/*) — gate/hook/check, não prosa;
#   teste:     arquivo que RODA (bash) e sai 0.
# Texto fora das linhas numeradas é ignorado: pode existir, não governa.
# Uso: bash scripts/check-mecanismo.sh [dir]   → exit 1 se algum invariante falha.
set -uo pipefail

SELFDIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
DIR="${1:-$(cd "$SELFDIR/.." && pwd)}"
cd "$DIR" || exit 2
ADAS="${ADAS:-ADAS.md}"
[ -f "$ADAS" ] || { echo "✗ check-mecanismo: sem $ADAS"; exit 1; }

registros=(.claude/settings.json scripts/install-hooks.sh package.json Makefile)
hooks_dir="$(git rev-parse --git-path hooks 2>/dev/null || true)"
[ -n "$hooks_dir" ] && registros+=("$hooks_dir/pre-commit")
for w in .github/workflows/*.yml .github/workflows/*.yaml; do [ -f "$w" ] && registros+=("$w"); done

registrado() {  # $1 = caminho do mecanismo
  local r
  for r in "${registros[@]}"; do [ -f "$r" ] && grep -qF -- "$1" "$r" 2>/dev/null && return 0; done
  return 1
}

fail=0; n_ok=0; n_inv=0
while IFS= read -r line; do
  case "$line" in [0-9]*.\ *) ;; *) continue ;; esac
  n_inv=$((n_inv+1))
  num="${line%%.*}"
  regra="$(printf '%s' "$line" | sed -E 's/^[0-9]+\.[[:space:]]*//; s/[[:space:]]*—[[:space:]]*mecanismo:.*$//; s/\*\*//g')"
  mec="$(printf '%s' "$line" | grep -oE 'mecanismo:[[:space:]]*[^[:space:]·]+' | head -1 | sed -E 's/^mecanismo:[[:space:]]*//')"
  tst="$(printf '%s' "$line" | grep -oE 'teste:[[:space:]]*[^[:space:]·]+' | head -1 | sed -E 's/^teste:[[:space:]]*//')"
  probs=""
  if [ -z "$mec" ]; then
    probs="invariante sem gatilho não entra — vire hook/check/gate (mecanismo: <arquivo>) ou tire daqui"
  elif [ ! -f "$mec" ]; then
    probs="mecanismo '$mec' não existe"
  elif ! registrado "$mec"; then
    probs="mecanismo '$mec' existe mas NADA o executa (registre em settings.json / install-hooks.sh / package.json / Makefile / workflow)"
  fi
  if [ -z "$tst" ]; then
    probs="${probs:+$probs; }sem teste: — invariante sem teste que falha é honra, não gatilho"
  elif [ ! -f "$tst" ]; then
    probs="${probs:+$probs; }teste '$tst' não existe"
  elif ! timeout 60 bash "$tst" >/dev/null 2>&1; then
    probs="${probs:+$probs; }teste '$tst' FALHOU (rode-o: bash $tst)"
  fi
  if [ -n "$probs" ]; then
    echo "✗ [mecanismo] invariante $num «${regra:0:60}»: $probs"; fail=1
  else
    echo "✓ [mecanismo] invariante $num «${regra:0:60}» — $mec · teste ok"; n_ok=$((n_ok+1))
  fi
done < <(sed -n '/<!-- adas-core-start -->/,/<!-- adas-core-end -->/p' "$ADAS")

[ "$n_inv" = 0 ] && { echo "✗ check-mecanismo: nenhum invariante numerado no núcleo do $ADAS — modo mecanismo sem invariante não governa nada"; exit 1; }
if [ "$fail" -ne 0 ]; then
  echo "✗ check-mecanismo: $((n_inv-n_ok)) de $n_inv invariante(s) sem gatilho registrado e testado — não entram; corrija ou remova"
  exit 1
fi
echo "✓ check-mecanismo: $n_inv invariante(s), cada um com mecanismo registrado e teste verde"

#!/usr/bin/env bash
# adas-init.sh — o dia 0 do ADAS num comando: declara o MODO, grava a versão do esqueleto,
# gera o índice de decisões, instala o pre-commit (se há git) e sela a instalação.
#
# Uso: bash scripts/adas-init.sh --modo doc|mecanismo [--fonte <clone do repo adas>] [dir]
#
# O modo responde UMA pergunta: quem está no ato?
#   doc       — há um humano na sessão; quem aplica a regra é o dono. O check lembra, não impede.
#   mecanismo — agente headless, ou repo commitado por agente. Invariante SEM gatilho
#               (hook/check/gate registrado + teste) NÃO entra no ADAS.md: check-mecanismo FALHA.
#
# Idempotente: rodado de novo, não sobrescreve nada preenchido — só re-sela. Exceção declarada:
# .adas/skeleton-version acompanha --fonte (ver passo 4), senão um erro do dia 0 ficaria para sempre.
set -uo pipefail

SELFDIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
MODO=""; FONTE=""; DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --modo) MODO="${2:-}"; shift 2 ;;
    --fonte) FONTE="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) DIR="$1"; shift ;;
  esac
done
[ -z "$DIR" ] && DIR="$(cd "$SELFDIR/.." && pwd)"

case "$MODO" in
  doc|mecanismo) ;;
  *)
    echo "✗ adas-init: falta --modo. Quem está no ato?"
    echo "    humano na sessão (o dono lê e corrige na hora)        → --modo doc"
    echo "    agente headless, ou repo commitado por agente          → --modo mecanismo"
    exit 2 ;;
esac
command -v jq >/dev/null 2>&1 || { echo "✗ adas-init: precisa de jq no PATH"; exit 2; }
cd "$DIR" || exit 2
mkdir -p .adas

# 1) modo → .adas/profile.json (cria se não existe; não toca nas outras chaves)
[ -f .adas/profile.json ] || echo '{}' > .adas/profile.json
tmp="$(mktemp)"; jq --arg m "$MODO" '.modo = $m' .adas/profile.json > "$tmp" && mv "$tmp" .adas/profile.json

# 2) ADAS.md do modo. Só troca enquanto o ADAS.md ainda é o template do OUTRO modo
#    (marca <!-- adas-modo: x -->); ADAS.md preenchido nunca é sobrescrito.
if [ "$MODO" = mecanismo ] && grep -q '<!-- adas-modo: doc -->' ADAS.md 2>/dev/null \
   && [ -f mecanismo/ADAS.mecanismo.md ]; then
  rm -f ADAS.md && mv mecanismo/ADAS.mecanismo.md ADAS.md
  echo "• ADAS.md = template do modo mecanismo (o do modo doc saiu; está no repo adas se precisar)"
elif [ "$MODO" = doc ] && [ -f mecanismo/ADAS.mecanismo.md ]; then
  rm -f mecanismo/ADAS.mecanismo.md
fi

# 3) data de adoção na DA-001 do template (só o token literal; diário preenchido não muda)
if grep -q '<YYYY-MM-DD>' DECISIONS.md 2>/dev/null; then
  sed -i "s/<YYYY-MM-DD>/$(date +%F)/g" DECISIONS.md
fi

# 4) versão do esqueleto — de --fonte (clone do repo adas). Quando --fonte RESOLVE, grava SEMPRE
#    (um "nao-registrada" de uma execução anterior sem --fonte é corrigido, não ignorado em silêncio);
#    sem --fonte, só preenche o que está vazio e avisa — nunca apaga um valor já gravado.
v=""; [ -n "$FONTE" ] && v="$(git -C "$FONTE" rev-parse --short=7 HEAD 2>/dev/null)"
if [ -n "$v" ]; then
  old="$(cat .adas/skeleton-version 2>/dev/null || true)"
  echo "$v" > .adas/skeleton-version
  [ -n "$old" ] && [ "$old" != "$v" ] && echo "• skeleton-version: $old → $v (substituído: --fonte resolveu)"
else
  [ -n "$FONTE" ] && echo "• --fonte '$FONTE' não resolve a um commit (não é clone git legível) — versão não gravada por ele"
  if [ ! -s .adas/skeleton-version ]; then
    echo "nao-registrada" > .adas/skeleton-version
    echo "• skeleton-version não registrada (passe --fonte <clone do repo adas> para gravar o commit)"
  fi
fi

# 5) índice de decisões nasce do diário
if ! grep -qsE '^\.adas/snapshots/?$' .gitignore; then printf '.adas/snapshots/\n.adas/da.lock\n' >> .gitignore; fi

bash scripts/da-index.sh update . >/dev/null || { echo "✗ adas-init: da-index update falhou"; exit 1; }

# 6) pre-commit = o gate (se há git)
if git rev-parse --show-toplevel >/dev/null 2>&1; then
  bash scripts/install-hooks.sh >/dev/null || echo "• install-hooks falhou — pre-commit não instalado"
fi

# 7) prova: o check RODOU aqui, neste modo
echo "✓ adas-init: modo=$MODO · versão=$(cat .adas/skeleton-version) · índice gerado · pre-commit $(git rev-parse --show-toplevel >/dev/null 2>&1 && echo instalado || echo 'pulado (sem git)')"
bash scripts/check-adas.sh --seal .

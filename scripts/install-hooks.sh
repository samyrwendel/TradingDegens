#!/usr/bin/env bash
# install-hooks.sh — instala o pre-commit dos gates do ADAS: o commit é o gate;
# o que a máquina cobra roda no pre-commit, não na honra.
#
# Achado que motivou: os gates existiam e NINGUÉM os chamava — check-secrets.sh com
# SEVERITY=block e zero invocações no commit; repos sem pre-commit nenhum (só .sample).
#
# O pre-commit instalado roda, NESTA ORDEM:
#   1. check-secrets.sh   → BLOCK (segredo staged nunca entra)
#   2. check-adas.sh      → severidade que ele já tem (frontmatter quebrado = BLOCK; em modo
#      mecanismo, invariante sem gatilho = BLOCK; o resto WARN visível)
#   3. da-index.sh check  → BLOCK se o índice de DAs divergir — só se existem
#      scripts/da-index.sh e DECISIONS.md
#
# Idempotente: rodar 2x = mesmo resultado. Hook alheio pré-existente é PRESERVADO
# (vira .git/hooks/pre-commit.local e é chamado ao final do nosso).
# `git commit --no-verify` continua possível — por isso o CI repete os mesmos gates.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "✗ install-hooks: não é um repo git"; exit 1; }
HOOKS_DIR="$(git -C "$ROOT" rev-parse --git-path hooks)"
case "$HOOKS_DIR" in /*) ;; *) HOOKS_DIR="$ROOT/$HOOKS_DIR" ;; esac
PC="$HOOKS_DIR/pre-commit"
MARK="# pre-commit ADAS (gerado por scripts/install-hooks.sh)"

# nada a instalar = falha BARULHENTA (gate que não faz nada é mentira de segurança).
if [ ! -f "$ROOT/scripts/check-secrets.sh" ] && [ ! -f "$ROOT/scripts/check-adas.sh" ]; then
  echo "✗ install-hooks: nenhum gate encontrado (scripts/check-secrets.sh, scripts/check-adas.sh) — nada a instalar"; exit 1
fi

# hook alheio → preserva como pre-commit.local (chamado no fim do nosso)
if [ -f "$PC" ] && ! grep -qF "$MARK" "$PC" 2>/dev/null; then
  mv "$PC" "$HOOKS_DIR/pre-commit.local"
  echo "• pre-commit pré-existente preservado em pre-commit.local (será chamado após os gates)"
fi

cat > "$PC" <<EOF
#!/usr/bin/env bash
$MARK
# Reinstalar/atualizar: bash scripts/install-hooks.sh · Pular conscientemente: --no-verify
# (o CI repete os mesmos gates — pular aqui não pula lá)
set -uo pipefail
cd "\$(git rev-parse --show-toplevel)"

# 1) segredo staged = BLOCK, sempre
if [ -f scripts/check-secrets.sh ]; then
  bash scripts/check-secrets.sh || { echo "✗ pre-commit: check-secrets BLOQUEOU (segredo no staged)"; exit 1; }
fi

# 2) auditoria do ADAS — severidade dela (frontmatter quebrado ou, em modo mecanismo,
#    invariante sem gatilho = exit 1 = bloqueia)
if [ -f scripts/check-adas.sh ]; then
  bash scripts/check-adas.sh . || { echo "✗ pre-commit: check-adas reprovou"; exit 1; }
fi

# 3) índice de DAs sincronizado
if [ -f scripts/da-index.sh ] && [ -f DECISIONS.md ]; then
  bash scripts/da-index.sh check . \\
    || { echo "✗ pre-commit: índice de DAs divergente — rode scripts/da-index.sh update e adicione ao commit"; exit 1; }
fi

# hook local pré-existente (preservado pelo install-hooks)
if [ -f scripts/check-da-refs.sh ] && [ -f DECISIONS.md ]; then
  bash scripts/check-da-refs.sh || { echo "✗ pre-commit: citação DA-NNN a decisão que o diário não tem (modo mecanismo)"; exit 1; }
fi

if [ -x "\$(git rev-parse --git-path hooks)/pre-commit.local" ]; then
  "\$(git rev-parse --git-path hooks)/pre-commit.local" || exit 1
fi
exit 0
EOF
chmod +x "$PC"
echo "✓ pre-commit instalado em $PC (secrets→BLOCK · check-adas · da-index · da-refs; --no-verify existe, o CI repete)"

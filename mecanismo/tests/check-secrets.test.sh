#!/usr/bin/env bash
# Teste do gatilho "segredo nunca entra no repo": check-secrets.sh tem de BLOQUEAR um token
# commitado/staged e passar num repo limpo. O token falso é montado em runtime para este
# arquivo não acionar o próprio gate.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
CS="$HERE/../../scripts/check-secrets.sh"
[ -f "$CS" ] || { echo "✗ sem scripts/check-secrets.sh"; exit 1; }
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
tk="ghp_$(printf '0%.0s' $(seq 36))"

mkdir -p "$T/sujo/scripts" && cd "$T/sujo" && git init -q && git config user.email t@t && git config user.name t \
  && cp "$CS" scripts/ && echo "tk='$tk'" > c.js && git add -A && git commit -qm x
bash scripts/check-secrets.sh >/dev/null 2>&1 && { echo "✗ token commitado passou"; exit 1; }

mkdir -p "$T/limpo/scripts" && cd "$T/limpo" && git init -q && git config user.email t@t && git config user.name t \
  && cp "$CS" scripts/ && echo ok > a.txt && git add -A && git commit -qm x
bash scripts/check-secrets.sh >/dev/null 2>&1 || { echo "✗ repo limpo foi bloqueado"; exit 1; }
# allowlist declarada (caminho | motivo | data): fixture com padrão de chave no histórico passa SÓ com o
# arquivo versionado; fixture nova com segredo real continua bloqueada (o gate é calibrado, não desligado).
mkdir -p "$T/fx/scripts" "$T/fx/tests" "$T/fx/.adas" && cd "$T/fx" && git init -q && git config user.email t@t && git config user.name t \
  && cp "$CS" scripts/ && echo "hdr = 'Bearer test-$(printf 'x%.0s' $(seq 24))'" > tests/fixture.test.js && git add -A && git commit -qm fixture
bash scripts/check-secrets.sh >/dev/null 2>&1 && { echo "✗ fixture 'Bearer test-…' sem allowlist passou (porta 2)"; exit 1; }
printf 'tests/fixture.test.js | token de teste, sem valor real | 2026-09-06\n' > .adas/secrets-allowlist
bash scripts/check-secrets.sh >/dev/null 2>&1 || { echo "✗ allowlist declarada não isentou a fixture"; exit 1; }
echo "tk='$tk'" > tests/nova.test.js && git add -A && git commit -qm nova
bash scripts/check-secrets.sh >/dev/null 2>&1 && { echo "✗ segredo em fixture NOVA passou com allowlist (gate desligado)"; exit 1; }
echo "✓ check-secrets bloqueia token, passa repo limpo, e a allowlist isenta só o caminho declarado"

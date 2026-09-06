#!/usr/bin/env bash
# check-secrets — GATE de segurança (ADAS · faixa seguranca-acesso). BLOQUEIA segredo entrando no repo.
# O hook avisa na EDIÇÃO; este BLOQUEIA no COMMIT/DEPLOY. Pré-commit por padrão (conteúdo STAGED).
#   bash scripts/check-secrets.sh            # staged (pré-commit)
#   bash scripts/check-secrets.sh --all      # toda a árvore tracked + porta 2 (histórico do git)
#   bash scripts/check-secrets.sh --dir src  # um diretório
# Gate: "precommit": "bash scripts/check-secrets.sh"  ·  no deploy junto dos outros check-*.
#
# PORTAS 1 e 2 das "seis portas do app": são as duas MECÂNICAS (grep puro, sem prova humana — as
# outras quatro exigem evidência e vivem em scripts/check-app-security.sh). Linhas
# marcadas "[porta 1" / "[porta 2" são o contrato que check-adas.sh lê pra montar o
# resumo das seis portas — não mude o prefixo sem atualizar os dois lados.
set -uo pipefail

mode="staged"; dir="."
while [ $# -gt 0 ]; do
  case "$1" in
    --all) mode="all" ;;
    --dir) mode="dir"; dir="${2:-.}"; shift ;;
  esac; shift
done

# Deploy/pós-commit: nada staged → o modo staged seria um no-op VERDE (falsa garantia
# na última barreira). Cai pra varredura completa em vez de aprovar sem examinar nada.
# (Efeito aceito BY DESIGN: amend-só-mensagem/commit vazio com segredo histórico na
#  árvore passam a bloquear — "se já vazou, ROTACIONE"; use --dir pra escopo menor.)
if [ "$mode" = "staged" ] && [ -z "$(git diff --cached --name-only 2>/dev/null)" ]; then
  echo "• staged vazio — nada a examinar aí; caindo para varredura completa (--all)"
  mode="all"
fi

# alta confiança → BLOCK (token GitHub, chave privada, AWS, Slack, Google API)
HI='ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,}|gh[osu]_[A-Za-z0-9]{36}|-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}|xox[baprs]-[0-9A-Za-z-]{10,}|AIza[0-9A-Za-z_-]{35}'
# heurística key=value → WARN (pode ser falso-positivo)
LO='(api[_-]?key|secret|passwd|password|access[_-]?token|auth[_-]?token)["'"'"' ]*[:=]["'"'"' ]*[A-Za-z0-9/_+.-]{16,}'

# ── PORTA 1 — chave/token no que é servido ao NAVEGADOR ─────────────────────
# Padrões pedidos pela DA: sk- (OpenAI/Anthropic), AIza (Google), ghp_ (GitHub), AKIA
# (AWS), xox[abp]- (Slack), "Bearer <token literal>", e KEY=valor dos quatro provedores
# citados por nome. Reusa o HI de cima (mesma classe) mais o que falta pra cobrir a
# lista exata da DA.
FRONT_KEY='sk-[A-Za-z0-9]{16,}|AIza[0-9A-Za-z_-]{35}|ghp_[A-Za-z0-9]{36}|AKIA[0-9A-Z]{16}|xox[abp]-[0-9A-Za-z-]{10,}|[Bb]earer +[A-Za-z0-9._-]{16,}|(ELEVENLABS_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|TELEGRAM_BOT_TOKEN) *[:=] *[A-Za-z0-9]{8,}'
# NEXT_PUBLIC_/VITE_/REACT_APP_ resolvem em BUILD TIME — o valor vai pro bundle público
# mesmo que o código "pareça" server-side. Proibido quando o NOME da variável contém
# KEY/SECRET/TOKEN/PASS (case-insensitive): a intenção do nome já denuncia segredo.
FRONT_PREFIX='(NEXT_PUBLIC_|VITE_|REACT_APP_)[A-Z0-9_]*(KEY|SECRET|TOKEN|PASS)[A-Z0-9_]*[[:space:]]*[:=]'
# Diretórios convencionalmente SERVIDOS ao navegador (build output / templates
# renderizados). Heurística por NOME de componente do caminho — sem stack conhecida
# não dá pra fazer melhor que convenção.
SERVED_DIR_RE='(^|/)(static|build|dist|public|templates)(/|$)'

block=0; warn=0
base="$dir"; [ "$mode" = "all" ] && base="."

# ── ALLOWLIST DECLARADA — fixture conhecida, não segredo ─────────────────────────────────
# .adas/secrets-allowlist (versionado): uma linha por CAMINHO isento, no formato
#   <caminho relativo à raiz> | <motivo> | <AAAA-MM-DD>
# Só o caminho listado é isento (árvore e histórico — porta 2). Linha sem motivo ou sem data
# NÃO vale (avisa e ignora): isenção sem porquê é o gate desligado com outro nome. Caso real:
# token de teste ("Bearer test-…") numa fixture commitada acusava porta 2 em todo commit e só
# reescrever a história "resolveria". Teto declarado: um segredo REAL que entrar num arquivo já
# listado passa — por isso a lista é de caminhos de teste, curta e revisada; arquivo novo nunca
# está nela. Exceção no CÓDIGO (regex solta) é proibida: a isenção mora no arquivo, com motivo.
ALLOWLIST_FILE="${SECRETS_ALLOWLIST:-.adas/secrets-allowlist}"
allow_paths=""
if [ -f "$ALLOWLIST_FILE" ]; then
  while IFS= read -r _al; do
    case "$_al" in ''|'#'*) continue ;; esac
    _ap=$(printf '%s' "$_al" | cut -d'|' -f1 | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    _am=$(printf '%s' "$_al" | cut -d'|' -f2 | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    _ad=$(printf '%s' "$_al" | cut -d'|' -f3 | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    if [ -z "$_ap" ] || [ -z "$_am" ] || ! printf '%s' "$_ad" | grep -qE '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'; then
      echo "• allowlist: entrada INVÁLIDA ignorada (precisa de: caminho | motivo | AAAA-MM-DD): $_al"; warn=1; continue
    fi
    allow_paths="${allow_paths}${_ap}
"
  done < "$ALLOWLIST_FILE"
  [ -n "$allow_paths" ] && echo "ℹ allowlist ($ALLOWLIST_FILE): $(printf '%s' "$allow_paths" | grep -c .) caminho(s) isento(s) por motivo declarado: $(printf '%s' "$allow_paths" | tr '\n' ' ')"
fi
is_allowed() { [ -n "$allow_paths" ] && printf '%s\n' "$allow_paths" | grep -qxF -- "${1#./}"; }
# Exclusão por SUFIXO (.example/.sample/.template), não por "logo depois de .env":
# ".env.enterprise.example" (achado real ao rodar isto contra um projeto de verdade,
# num caso real) tem SEGMENTO A MAIS entre ".env" e ".example" e escapava da
# exclusão antiga (\.env\.(example|...)$ exige os dois lado a lado) — virava falso
# "BLOCK .env tracked" sobre um arquivo que É o contrato (chave sem valor), não o
# segredo. Qualquer coisa que já passou pelo filtro ".env" e termina em
# example/sample/template é template, não importa quantos segmentos tem no meio.
env_tracked(){ grep -E '(^|/)\.env($|\.[^/]*)$' | grep -vE '\.(example|sample|template)$'; }
# Conteúdo de um arquivo rastreado, no modo certo: STAGED (índice, `git show :path`)
# quando ainda não commitado, ou o que está NO DISCO/tracked quando já é all/dir —
# um helper só, pra portas 1/2 não duplicarem a lógica staged×all três vezes.
_conteudo() {
  if [ "$mode" = "staged" ] && [ -d .git ]; then git show ":$1" 2>/dev/null
  else cat "$base/$1" 2>/dev/null; fi
}

if [ "$mode" = "staged" ] && [ -d .git ]; then
  if git diff --cached --name-only 2>/dev/null | env_tracked >/dev/null; then
    echo "✗ [BLOCK] .env sendo commitado — ponha no .gitignore (use .env.example SEM valores)"; block=1
  fi
  _staged_files=$(git diff --cached --name-only 2>/dev/null || true)
  _staged_chk=""; while IFS= read -r _sf; do [ -z "$_sf" ] && continue; is_allowed "$_sf" || _staged_chk="${_staged_chk}${_sf}
"; done <<EOF
$_staged_files
EOF
  added=$(printf '%s' "$_staged_chk" | xargs -d '\n' -r git diff --cached -U0 -- 2>/dev/null | grep '^+' | grep -v '^+++' || true)
  hits=$(printf '%s\n' "$added" | grep -nEi "$HI" || true)
  [ -n "$hits" ] && { echo "✗ [BLOCK] possível SEGREDO no conteúdo staged:"; printf '%s\n' "$hits" | sed 's/^/    /' | head -8; block=1; }
  warns=$(printf '%s\n' "$added" | grep -Ei "$LO" || true)
  [ -n "$warns" ] && { echo "• [warn] credencial key=value no staged — confirme se não é segredo"; warn=1; }
else
  base="$dir"; [ "$mode" = "all" ] && base="."
  list=$(git -C "$base" ls-files 2>/dev/null)
  [ -z "$list" ] && list=$(cd "$base" 2>/dev/null && find . -type f -not -path '*/node_modules/*' -not -path '*/.git/*' 2>/dev/null)
  printf '%s\n' "$list" | env_tracked >/dev/null && { echo "✗ [BLOCK] .env tracked no repo (use .env.example)"; block=1; }
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    p="$base/$f"; [ -f "$p" ] || continue
    case "$f" in *node_modules/*|*.min.*|*.lock|*/.git/*) continue ;; esac
    is_allowed "$f" && continue
    h=$(grep -nEi "$HI" "$p" 2>/dev/null || true)
    [ -n "$h" ] && { echo "✗ [BLOCK] possível SEGREDO em $f:"; printf '%s\n' "$h" | sed 's/^/    /' | head -4; block=1; }
  done <<EOF
$list
EOF
fi

# ── PORTA 1 — chave/token no que é servido ao navegador. Roda em QUALQUER
# modo (staged ou all/dir): o nome do arquivo já basta pra saber se ele é "servido"
# (SERVED_DIR_RE), então não precisa esperar --all pra pegar um segredo novo indo
# pra dentro de static/build/dist/public/templates.
porta1_files=""
if [ "$mode" = "staged" ] && [ -d .git ]; then
  porta1_files=$(git diff --cached --name-only 2>/dev/null || true)
else
  porta1_files=$(git -C "$base" ls-files 2>/dev/null)
  [ -z "$porta1_files" ] && porta1_files=$(cd "$base" 2>/dev/null && find . -type f -not -path '*/node_modules/*' -not -path '*/.git/*' 2>/dev/null | sed 's#^\./##')
fi
p1_hit=0
served=$(printf '%s\n' "$porta1_files" | grep -E "$SERVED_DIR_RE" || true)
if [ -n "$served" ]; then
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    is_allowed "$f" && continue
    h=$(_conteudo "$f" | grep -nEi "$FRONT_KEY" || true)
    [ -n "$h" ] && { echo "✗ [BLOCK] [porta 1 — chave no front] $f serve chave/token ao navegador:"; printf '%s\n' "$h" | sed 's/^/    /' | head -4; block=1; p1_hit=1; }
  done <<EOF
$served
EOF
fi
# NEXT_PUBLIC_/VITE_/REACT_APP_ com nome de KEY/SECRET/TOKEN/PASS — checa QUALQUER
# arquivo rastreado (não só os diretórios servidos): é ONDE A VARIÁVEL NASCE
# (.env.example, config do bundler) que decide se ela vai pro bundle público.
while IFS= read -r f; do
  [ -z "$f" ] && continue
  case "$f" in *node_modules/*|*.min.*|*.lock|*/.git/*) continue ;; esac
  h=$(_conteudo "$f" | grep -nE "$FRONT_PREFIX" || true)
  [ -n "$h" ] && { echo "✗ [BLOCK] [porta 1 — chave no front] $f declara variável NEXT_PUBLIC_/VITE_/REACT_APP_ com nome de segredo:"; printf '%s\n' "$h" | sed 's/^/    /' | head -4; block=1; p1_hit=1; }
done <<EOF
$porta1_files
EOF
[ "$p1_hit" = 0 ] && echo "✓ [porta 1 — chave no front] nada em static/build/dist/public/templates, nenhum NEXT_PUBLIC_/VITE_/REACT_APP_ com nome de segredo"

# ── PORTA 2 — segredo no HISTÓRICO do git. Só em all/dir: reescrever a
# história não é frequente, e a varredura é sobre commits PASSADOS — não muda entre
# um commit novo e outro, então não vale pagar rev-list+grep em CADA pré-commit.
if [ "$mode" != "staged" ] && [ -d .git ]; then
  # .env.example/.sample/.template são o CONTRATO (chaves sem valor) — sempre
  # permitidos, mesma exclusão de env_tracked() acima. `git log -- pathspec` não
  # filtra por sufixo sozinho, então pega o nome de arquivo por commit (--name-only)
  # e só mantém o commit se SOBRAR pelo menos um arquivo que não é exemplo.
  hist_names=$(git log --all --full-history --format='@@%H %s' --name-only \
    -- '*.env' '.env.*' 'auth.json' 2>/dev/null | awk '
    /^@@/ { if (commit != "" && keep) print commit; commit=$0; keep=0; next }
    NF && $0 !~ /\.(example|sample|template)$/ { keep=1 }
    END { if (commit != "" && keep) print commit }
  ')
  hist_content=""
  revs=$(git rev-list --all 2>/dev/null || true)
  if [ -n "$revs" ]; then
    hist_content=$(printf '%s\n' "$revs" | xargs git grep -lE "$FRONT_KEY" 2>/dev/null || true)
    # allowlist: "rev:caminho" cujo caminho está isento sai da acusação (o resto fica)
    if [ -n "$allow_paths" ] && [ -n "$hist_content" ]; then
      hist_content=$(printf '%s\n' "$hist_content" | while IFS= read -r _hc; do is_allowed "${_hc#*:}" || printf '%s\n' "$_hc"; done)
    fi
  fi
  if [ -n "$hist_names" ] || [ -n "$hist_content" ]; then
    echo "✗ [BLOCK] [porta 2 — .env no histórico] segredo/arquivo sensível JÁ ESTEVE no git:"
    [ -n "$hist_names" ] && { echo "  arquivos (.env/.env.*/auth.json, exceto .example/.sample/.template):"; printf '%s\n' "$hist_names" | sed 's/^@@/    /' | head -6; }
    [ -n "$hist_content" ] && { echo "  commits com padrão de chave:"; printf '%s\n' "$hist_content" | sed 's/^/    /' | head -6; }
    echo "  rotacione ANTES de limpar o histórico — apagar o arquivo no HEAD não apaga o commit antigo"
    block=1
  else
    echo "✓ [porta 2 — .env no histórico] nenhum .env/.env.*/auth.json nem padrão de chave em nenhum commit"
  fi
fi

if [ "$block" -ne 0 ]; then echo; echo "✗ check-secrets: segredo detectado — NÃO commitar. Se já vazou, ROTACIONE o segredo."; exit 1; fi
if [ "$warn" -ne 0 ]; then echo; echo "⚠ check-secrets: avisos (acima) — revise"; exit 0; fi
echo "✓ check-secrets: nenhum segredo de alta confiança detectado"

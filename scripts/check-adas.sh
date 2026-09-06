#!/usr/bin/env bash
# check-adas.sh — auditoria de higiene do ADAS deste projeto.
# Uso: bash scripts/check-adas.sh [--seal] [dir]
#   [dir] ausente = a raiz do projeto deste script (scripts/..) — nunca o cwd.
#   --seal grava .adas/install-check (a prova de que o check RODOU aqui, neste modo).
# Lê o modo em .adas/profile.json:
#   doc       — avisa; quem aplica a regra é o dono presente na sessão. Este check lembra, não impede.
#   mecanismo — os mesmos avisos + scripts/check-mecanismo.sh: invariante sem gatilho = FAIL (exit 1).
# Saída: linhas "•" são avisos (contam), linhas "ℹ" são informação (não contam), "✗" bloqueia.
set -uo pipefail

SELFDIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
SEAL=0; DIR=""
for a in "$@"; do case "$a" in --seal) SEAL=1 ;; *) DIR="$a" ;; esac; done
[ -z "$DIR" ] && DIR="$(cd "$SELFDIR/.." && pwd)"
cd "$DIR" || { echo "✗ check-adas: diretório '$DIR' inacessível"; exit 2; }

SKILLS_DIR="${SKILLS_DIR:-.claude/skills}"
SPECS_DIR="${SPECS_DIR:-.specs}"
ADAS="${ADAS:-ADAS.md}"
DECISIONS="${DECISIONS:-DECISIONS.md}"
warn=0; block=0; notes=0
note(){ echo "• $*"; notes=$((notes+1)); }
info(){ echo "ℹ $*"; }

GIT=0; git rev-parse --show-toplevel >/dev/null 2>&1 && GIT=1
HAS_COMMIT=0; [ "$GIT" = 1 ] && git rev-parse HEAD >/dev/null 2>&1 && HAS_COMMIT=1

# 0) modo — declarado por adas-init.sh; ausente audita como doc e diz o comando
MODO=""
[ -f .adas/profile.json ] && MODO="$(grep -oE '"modo"[[:space:]]*:[[:space:]]*"[a-z]+"' .adas/profile.json 2>/dev/null | sed -E 's/.*"([a-z]+)"$/\1/')"
case "$MODO" in
  doc|mecanismo) ;;
  "") info "instalação sem modo declarado — rode: bash scripts/adas-init.sh --modo doc|mecanismo (auditando como doc)"; MODO=doc ;;
  *) note "modo '$MODO' desconhecido em .adas/profile.json (use doc|mecanismo)"; warn=1; MODO=doc ;;
esac

# 1) placeholders — o aviso legítimo do dia 0, com a lista do que falta preencher
ph="$(grep -rlE --include='*.md' --include='*.css' --exclude-dir=_template '<PLACEHOLDER|<faixa>|<NNN>|<PROJETO>|<nome>' "$SPECS_DIR" "$SKILLS_DIR" "$ADAS" "$DECISIONS" AGENTS.md 2>/dev/null | sort -u | tr '\n' ' ')"
[ -n "$ph" ] && { note "PLACEHOLDER ainda presente — preencha: ${ph% }"; warn=1; }

# 2) faixas: governadas (deste projeto) × dependência (terceiros) — por sinal, sem lista
GOV=(); DEP=()
while IFS= read -r f; do
  sd="$(dirname "$f")"; own=""
  if [ "$GIT" = 1 ] && [ -n "$(git ls-files -- "$sd" 2>/dev/null | head -1)" ]; then
    own=g
  else
    p="$sd"
    while [ -n "$p" ] && [ "$p" != "$SKILLS_DIR" ] && [ "$p" != "." ] && [ "$p" != "/" ]; do
      [ -e "$p/.git" ] && { own=d; break; }
      p="$(dirname "$p")"
    done
  fi
  if [ -z "$own" ]; then
    if grep -qiE "extra(í|i)do de|\.specs/|DA-[0-9]" "$f" 2>/dev/null; then own=g
    else
      top="${sd#"$SKILLS_DIR"/}"; top="${top%%/*}"
      { [ -n "$top" ] && grep -qw -- "$top" "$ADAS" 2>/dev/null; } && own=g || own=d
    fi
  fi
  if [ "$own" = g ]; then GOV+=("$f"); else DEP+=("$f"); fi
done < <(find "$SKILLS_DIR" -name SKILL.md -not -path "*/_template/*" 2>/dev/null)

fm_issue() {  # frontmatter name+description (senão NÃO dispara)
  local fm; fm="$(awk '/^---[[:space:]]*$/{c++; next} c==1{print} c>=2{exit}' "$1" 2>/dev/null)"
  { printf '%s\n' "$fm" | grep -q "^name:" && printf '%s\n' "$fm" | grep -q "^description:"; } \
    || echo "SEM frontmatter name/description (não vai disparar)"
}
thin_issue() {  # trigger magro (piso mecânico do description)
  local fm desc thin n_trig n_symp
  fm="$(awk '/^---[[:space:]]*$/{c++; next} c==1{print} c>=2{exit}' "$1" 2>/dev/null)"
  desc="$(printf '%s\n' "$fm" | awk '/^description:/{flag=1} flag && /^[a-z_]+:/ && !/^description:/{flag=0} flag{print}' | tr -d '\n')"
  [ -z "$desc" ] && return 0
  thin=""
  [ "${#desc}" -lt 400 ] && thin="${thin}<400 chars; "
  n_trig=$(printf '%s' "$desc" | tr ',;' '\n' | grep -c .)
  [ "$n_trig" -lt 12 ] && thin="${thin}só ${n_trig} gatilhos (<12); "
  printf '%s' "$desc" | grep -qi "SEMPRE" || thin="${thin}sem cláusula 'use SEMPRE que'; "
  printf '%s' "$desc" | grep -qi "MESMO" || thin="${thin}sem negação 'MESMO que não peça'; "
  n_symp=$(printf '%s' "$desc" | grep -o "'" | wc -l)
  [ "$n_symp" -lt 4 ] && thin="${thin}<2 sintomas citados ('...'); "
  if [ "$n_symp" -ge 4 ] && ! printf '%s' "$desc" | grep -qiE \
    "\bagente (decide|estabelece|institui|cria|adota)\b|\bpor conta pr[oó]pria\b|\bsozinho\b|\beu vou\b|\bsem (o )?(dono|usu[aá]rio) (pedir|falar|mandar)\b"; then
    thin="${thin}COBERTURA DE ATOR: só fala do dono, nenhum ato do agente citado; "
  fi
  [ -n "$thin" ] && echo "TRIGGER MAGRO: ${thin}— engordar (sinônimos+sintomas+vocabulário real do usuário)"
  return 0
}
prov_issue() { grep -qiE "extra(í|i)do de|\.specs/|DA-[0-9]" "$1" || echo "sem procedência (cite .specs/ ou DA-NNN)"; }

for f in ${GOV[@]+"${GOV[@]}"}; do m="$(fm_issue "$f")"; [ -n "$m" ] && { note "FAIXA $m: $f"; block=1; }; done
for f in ${GOV[@]+"${GOV[@]}"}; do m="$(thin_issue "$f")"; [ -n "$m" ] && { note "$m em $f"; warn=1; }; done
for f in ${GOV[@]+"${GOV[@]}"}; do m="$(prov_issue "$f")"; [ -n "$m" ] && { note "faixa $m: $f"; warn=1; }; done
if [ "${#DEP[@]}" -gt 0 ]; then
  dep_probs=0
  for f in ${DEP[@]+"${DEP[@]}"}; do
    m="$(fm_issue "$f")$(thin_issue "$f")"; [ -n "$m" ] && dep_probs=$((dep_probs+1))
    if [ "${ADAS_CHECK_DEPS:-0}" = "1" ]; then
      m="$(fm_issue "$f")";   [ -n "$m" ] && echo "  · dep: $m — $f"
      m="$(thin_issue "$f")"; [ -n "$m" ] && echo "  · dep: $m — $f"
    fi
  done
  info "${#DEP[@]} skill(s) de DEPENDÊNCIA (terceiros) fora da auditoria, ${dep_probs} com achados — reporte upstream; ADAS_CHECK_DEPS=1 exibe"
fi

# 3) drift: faixa/.specs mudou depois do ADAS.md; mudança não commitada (só com ≥1 commit — dia 0 é dia 0)
if [ "$HAS_COMMIT" = 1 ]; then
  adas_t=$(git log -1 --format=%ct -- "$ADAS" 2>/dev/null || echo 0)
  while IFS= read -r f; do
    ft=$(git log -1 --format=%ct -- "$f" 2>/dev/null || echo 0)
    if [ "${ft:-0}" -gt "${adas_t:-0}" ]; then note "DRIFT: '$f' mudou depois do $ADAS — REGENERE o $ADAS"; warn=1; break; fi
  done < <(find "$SKILLS_DIR" "$SPECS_DIR" \( -name "*.md" -o -name "*.css" \) -not -path "*/_template/*" 2>/dev/null)
  if git status --porcelain -- "$SKILLS_DIR" "$SPECS_DIR" 2>/dev/null | grep -q .; then
    note "faixa/.specs com mudança NÃO COMMITADA — commite junto com o $ADAS regenerado (mesmo commit)"; warn=1
  fi
fi

# 4) DA citada existe no diário
for da in $(grep -rhoE "DA-[0-9]{3}" "$SKILLS_DIR" "$ADAS" 2>/dev/null | sort -u); do
  grep -q "$da" "$DECISIONS" 2>/dev/null || { note "$da citada mas ausente do $DECISIONS"; warn=1; }
done

# 5) âncora: a ferramenta descobre o ADAS sozinha?
anchor=""
for a in AGENTS.md CLAUDE.md .cursorrules; do [ -f "$a" ] && anchor="$a" && break; done
if [ -z "$anchor" ]; then note "sem arquivo-âncora (AGENTS.md/CLAUDE.md) — a ferramenta não descobre o ADAS sozinha"; warn=1
elif ! grep -q "ADAS.md" "$anchor" 2>/dev/null; then note "âncora '$anchor' não aponta pro $ADAS — adicione 'leia ADAS.md'"; warn=1; fi

# 6) núcleo reinjetável: marcadores + teto de 60 linhas
if [ -f "$ADAS" ]; then
  if ! grep -q "adas-core-start" "$ADAS" 2>/dev/null; then note "sem marcadores <!-- adas-core-start/end --> no $ADAS — o núcleo reinjetado cai no fallback (topo do arquivo)"; warn=1
  elif ! grep -q "adas-core-end" "$ADAS" 2>/dev/null; then note "marcador adas-core-start SEM adas-core-end no $ADAS — núcleo vira o arquivo inteiro"; warn=1
  else
    core_n=$(sed -n '/<!-- adas-core-start -->/,/<!-- adas-core-end -->/p' "$ADAS" 2>/dev/null | wc -l | tr -d ' ')
    core_n=$((core_n>2 ? core_n-2 : 0))
    [ "${core_n:-0}" -gt 60 ] && { note "núcleo adas-core com ${core_n} linhas (>60) — enxugue: é reinjetado a cada fronteira de contexto"; warn=1; }
  fi
else
  note "sem $ADAS"; warn=1
fi

# 7) versão do esqueleto instalado
if [ ! -s .adas/skeleton-version ]; then
  note "sem .adas/skeleton-version — rode: bash scripts/adas-init.sh --modo $MODO --fonte <clone do repo adas>"; warn=1
elif [ "${ADAS_CHECK_REMOTE:-0}" = "1" ] && command -v git >/dev/null 2>&1; then
  local_v="$(cut -c1-7 .adas/skeleton-version 2>/dev/null | head -1 | tr -d '[:space:]')"
  remote_v="$(timeout 5 git ls-remote https://github.com/samyrwendel/adas HEAD 2>/dev/null | cut -c1-7)"
  if [ -n "$remote_v" ] && [ -n "$local_v" ] && [ "$local_v" != "$remote_v" ]; then
    note "esqueleto instalado ($local_v) ≠ canônico remoto ($remote_v) — veja o que mudou no repo adas"; warn=1
  fi
fi

# 8) enforcement JIT por faixa (modo doc: a faixa chega no instante da edição)
if [ "$MODO" = doc ] && [ -d .claude ]; then
  if [ ! -f .claude/hooks/adas-inject.sh ]; then note "ENFORCEMENT: sem .claude/hooks/adas-inject.sh — a faixa não chega no ato da edição; a governança é só documento"; warn=1
  elif ! grep -q "adas-inject" .claude/settings.json 2>/dev/null; then note "ENFORCEMENT: adas-inject.sh existe mas não está registrado em .claude/settings.json — o hook nunca dispara"; warn=1; fi
fi
# 9) runtime host (opcional): só reclama de quem INSTALOU o host e esqueceu este repo
if [ -d "$HOME/.claude" ]; then
  _rconf="${ADAS_REPOS_CONF:-$HOME/.claude/adas/repos.conf}"
  _want_host=0; grep -qE '"runtime"[[:space:]]*:[[:space:]]*"host"' .adas/profile.json 2>/dev/null && _want_host=1
  if [ ! -f "$HOME/.claude/hooks/adas-activate.sh" ]; then
    if [ "$_want_host" = 1 ]; then note "ENFORCEMENT: .adas/profile.json pede runtime host, mas ele não está instalado nesta máquina (host/install.sh do repo adas)"; warn=1
    else info "runtime host (reinjeção do núcleo em compaction/subagente) não instalado nesta máquina — opcional: bash host/install.sh $PWD (repo adas)"; fi
  elif ! grep -q "adas-activate\|adas-route" "$HOME/.claude/settings.json" 2>/dev/null; then
    note "ENFORCEMENT: MEIA-INSTALAÇÃO — hooks adas-*.sh copiados mas ausentes de ~/.claude/settings.json (nunca disparam); rode host/install.sh de novo"; warn=1
  elif [ -f "$_rconf" ] && ! grep -qxF "$PWD" "$_rconf" 2>/dev/null; then
    note "ENFORCEMENT: runtime host instalado mas este repo está FORA do repos.conf ($_rconf) — reinjeção não cobre este repo; rode: bash host/install.sh $PWD"; warn=1
  fi
fi

# 10) índice de decisões nasceu e está sincronizado
if [ -f "$DECISIONS" ] && [ -f scripts/da-index.sh ]; then
  if [ ! -f DECISIONS-INDEX.md ]; then note "sem DECISIONS-INDEX.md — o índice não nasceu; rode: bash scripts/da-index.sh update"; warn=1
  elif ! bash scripts/da-index.sh check . >/dev/null 2>&1; then note "DECISIONS-INDEX.md DIVERGE do $DECISIONS — rode: bash scripts/da-index.sh update"; warn=1; fi
fi

# 11) selo: a instalação foi PROVADA neste modo?
if [ "$SEAL" != "1" ]; then
  if [ ! -f .adas/install-check ]; then note "sem .adas/install-check — a instalação nunca foi PROVADA; rode: bash scripts/adas-init.sh --modo $MODO"; warn=1
  else
    sealed_v="$(awk -F': ' '/^skeleton-version:/{print $2}' .adas/install-check 2>/dev/null | tr -d '[:space:]')"
    sealed_m="$(awk -F': ' '/^modo:/{print $2}' .adas/install-check 2>/dev/null | tr -d '[:space:]')"
    cur_v="$(cut -c1-7 .adas/skeleton-version 2>/dev/null | head -1 | tr -d '[:space:]')"
    if [ -n "$cur_v" ] && [ "$sealed_v" != "$cur_v" ]; then note "selo OBSOLETO (provado em '${sealed_v:-?}', esqueleto agora '$cur_v') — re-sele: bash scripts/check-adas.sh --seal"; warn=1
    elif [ -n "$sealed_m" ] && [ "$sealed_m" != "$MODO" ]; then note "selo de OUTRO modo (provado em '$sealed_m', agora '$MODO') — re-sele: bash scripts/check-adas.sh --seal"; warn=1; fi
  fi
fi

# 12) seis portas de segurança do app: só FALHA avisa; débito fica visível como informação
_porta_mec() { local marca="$1" out="$2"
  if printf '%s\n' "$out" | grep -q "✗ \[BLOCK\] \[$marca"; then echo "FALHA"
  elif printf '%s\n' "$out" | grep -q "✓ \[$marca"; then echo "PASSA"; else echo "?"; fi; }
if [ "$GIT" != 1 ]; then p1="sem git (não verificável)"; p2="$p1"
elif [ -f scripts/check-secrets.sh ]; then
  sec_out="$(bash scripts/check-secrets.sh --all 2>/dev/null)"
  p1="$(_porta_mec "porta 1" "$sec_out")"; p2="$(_porta_mec "porta 2" "$sec_out")"
else p1="sem check-secrets.sh"; p2="$p1"; fi
asec_out=""; [ -f scripts/check-app-security.sh ] && asec_out="$(bash scripts/check-app-security.sh 2>/dev/null)"
_porta_prova() { local marca="$1"
  [ -f scripts/check-app-security.sh ] || { echo "sem check-app-security.sh"; return; }
  if printf '%s\n' "$asec_out" | grep -q "✗ \[seis portas\].*($marca)"; then echo "FALHA (sem prova válida)"
  elif printf '%s\n' "$asec_out" | grep -q "✓ \[seis portas\].*($marca): N/A"; then echo "N/A (justificado)"
  elif printf '%s\n' "$asec_out" | grep -q "✓ \[seis portas\].*($marca)"; then echo "PASSA"
  else echo "débito"; fi; }
p3="$(_porta_prova "porta 3")"; p4="$(_porta_prova "porta 4")"; p5="$(_porta_prova "porta 5")"; p6="$(_porta_prova "porta 6")"
portas="1 chave-no-front=$p1 · 2 env-historico=$p2 · 3 validacao-servidor=$p3 · 4 arquivo-publico=$p4 · 5 erro-fala-demais=$p5 · 6 rate-limit=$p6"
case "$portas" in *FALHA*|*"?"*) note "SEIS PORTAS: $portas"; warn=1 ;; *) info "seis portas do app: $portas" ;; esac

# 13) detector de ator (modo doc, git, ≥10 commits): os commits viraram de agente? Sinal + sugestão, não veredito.
if [ "$MODO" = doc ] && [ "$HAS_COMMIT" = 1 ]; then
  n_commits=$(git rev-list --count HEAD 2>/dev/null || echo 0)
  if [ "$n_commits" -ge 10 ]; then
    n_ag=0; n_hum=0
    while IFS= read -r h; do
      if git log -1 --format='%an %ae%n%b' "$h" | grep -qiE 'claude|codex|copilot|gpt|gemini|\bbot\b|Co-Authored-By:.*(Claude|Codex|Copilot|GPT|Gemini)'; then n_ag=$((n_ag+1)); else n_hum=$((n_hum+1)); fi
    done < <(git rev-list -20 HEAD)
    if [ "$n_hum" = 0 ] && [ "$n_ag" -ge 8 ]; then
      last_adas="$(git log -1 --format=%H -- "$ADAS" 2>/dev/null)"
      m_since=0; [ -n "$last_adas" ] && m_since=$(git rev-list --count "$last_adas..HEAD" 2>/dev/null || echo 0)
      note "ATOR: $n_ag dos últimos $((n_ag+n_hum)) commits são de agente e há $m_since commit(s) desde a última mudança do $ADAS — as obrigações do modo doc (dobrar na faixa, regenerar no mesmo commit) não estão sendo executadas; o modo mecanismo troca obrigação por gate. Sugestão: bash scripts/adas-init.sh --modo mecanismo"; warn=1
    fi
  fi
fi

# 14) modo mecanismo: invariante sem gatilho não entra
if [ "$MODO" = mecanismo ]; then
  if [ -f scripts/check-mecanismo.sh ]; then
    if ! mec_out="$(bash scripts/check-mecanismo.sh . 2>&1)"; then
      printf '%s\n' "$mec_out" | sed 's/^/  /'; echo "✗ modo mecanismo: invariante sem gatilho registrado e testado — não entra"; block=1
    else info "modo mecanismo: $(printf '%s\n' "$mec_out" | tail -1)"; fi
  else note "modo mecanismo sem scripts/check-mecanismo.sh — nada cobra os invariantes"; warn=1; fi
fi

v="ok"; [ "$warn" -ne 0 ] && v="warn"; [ "$block" -ne 0 ] && v="block"
if [ "$SEAL" = "1" ]; then
  mkdir -p .adas
  { echo "selado: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "skeleton-version: $(cut -c1-7 .adas/skeleton-version 2>/dev/null | head -1 | tr -d '[:space:]')"
    echo "modo: $MODO"; echo "veredito: $v"; echo "avisos: $notes"
  } > .adas/install-check
  echo "🔏 selo gravado em .adas/install-check (modo $MODO, veredito: $v, $notes aviso(s)) — versione-o: é a prova, não cache"
fi
if [ "$block" -ne 0 ]; then echo "✗ check-adas: bloqueado (acima) — corrija antes de seguir"; exit 1; fi
if [ "$warn" -ne 0 ]; then echo "⚠ check-adas: $notes aviso(s) — não bloqueia (modo $MODO)"; exit 0; fi
case "$MODO" in
  doc) echo "✓ check-adas: higiene ok — modo doc: quem aplica a regra é o dono presente; este check lembra, não impede" ;;
  *)   echo "✓ check-adas: higiene ok — modo mecanismo: todo invariante do núcleo tem gatilho registrado e teste verde" ;;
esac

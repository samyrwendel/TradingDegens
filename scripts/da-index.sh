#!/usr/bin/env bash
# da-index.sh — índice de decisões GERADO de DECISIONS.md, e os checks de qualidade do diário.
# Uso: da-index.sh update|check [dir]
#      da-index.sh list [--escopo a,b] [--saga slug] [--vigentes] [--desde AAAA-MM-DD] [dir]
#      da-index.sh show DA-NNN [dir]
#   [dir] ausente = a raiz do projeto deste script (scripts/..) — nunca o cwd, nunca $HOME.
# Formato lido (o mesmo que scripts/da-new.sh escreve):
#   ## DA-NNN — Título
#   `escopo: x` · `saga: y` · `data: AAAA-MM-DD` · `refs: —` · `supersede: —`
#   **Regra:** … **Motivo:** … **Trade-off:** … **Lição:** … **Decidido por:** …
# ponytail: o parser awk abaixo é herdado inteiro; campos consolida/historico/medido são lidos e
# ignorados aqui (sagas/cabeças não existem nesta versão) — podar quando um harness próprio cobrir o parser.
set -uo pipefail

SELFDIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
SEP=$'\037'
DA_DIR_DEFAULT="$(cd "$SELFDIR/.." && pwd)"

_parse_awk() {
cat <<'AWKEOF'
BEGIN { SEP = sprintf("%c", 31); n = 0; inbody = 0 }
function trim(s) { gsub(/^[ \t]+|[ \t]+$/, "", s); return s }
function refs_in(s,    out) {
  out = ""
  while (match(s, /DA-[0-9]+[a-z]?/)) { out = out (out=="" ? "" : ",") substr(s, RSTART, RLENGTH); s = substr(s, RSTART+RLENGTH) }
  return out
}
function refs_in_plain(s,    out) {
  out = ""
  while (match(s, /DA-[0-9]+/)) { out = out (out=="" ? "" : ",") substr(s, RSTART, RLENGTH); s = substr(s, RSTART+RLENGTH) }
  return out
}
function csv_add(list, item) { if (item == "") return list; if (list == "") return item; return list "," item }
function mark_targets(map_name, targets, by,    k, a, i, key) {
  k = split(targets, a, ",")
  for (i=1; i<=k; i++) {
    key = trim(a[i]); if (key == "") continue
    sub(/^DA-/, "", key)
    if (key == by) continue
    if (map_name == "sup") { if (!(key in supby)) supby[key] = by }
    else if (map_name == "par") { if (!(key in parby)) parby[key] = by }
    else if (map_name == "con") { if (!(key in conby)) conby[key] = by }
  }
}

# ---- Regra fallback (b), consciente de PARÁGRAFO (a convenção do diário fix pós-teste 03/09) --------------
# Um parágrafo pode vir hard-wrapped em várias linhas físicas. A linha ORIGINAL só olhava a
# 1a linha física pra decidir "é metadado?" — uma continuação como "por:** fulano. **Escopo:**"
# (2a linha de um `**Data:** ... **Implementado\npor:** fulano...` quebrado) passava batida e
# virava a Regra. Agora classifica o PARÁGRAFO inteiro (até linha em branco/heading/---) pela
# 1a linha, e só then extrai a 1a FRASE (até .!?) do parágrafo de conteúdo real.
function is_metadata_start(l) {
  if (l ~ /^\*\*(Data|Status|Autor|Decidido por|Implementado por|Gatilho|Pedido do|Escopo)[:]?\*\*?/) return 1
  if (l ~ /^Relacionadas:/) return 1
  if (l ~ /^`/) return 1
  if (l ~ /^>/) return 1
  if (l ~ /^---/) return 1
  if (l ~ /^[([]/) return 1
  if (l ~ /^\*?"/) return 1
  return 0
}
function strip_prefix_markup(s,    r) {
  r = s
  sub(/^[ \t]*([-*][ \t]+|[0-9]+\.[ \t]+)/, "", r)
  gsub(/\*\*/, "", r)
  return trim(r)
}
function first_sentence(s,    n2, i, ch, nx) {
  n2 = length(s)
  for (i = 1; i <= n2; i++) {
    ch = substr(s, i, 1)
    if (ch == "." || ch == "!" || ch == "?") {
      nx = (i < n2) ? substr(s, i+1, 1) : ""
      if (nx == "" || nx == " " || nx == "\t") return substr(s, 1, i)
    }
  }
  return ""
}
function valid_sentence(s,    c) {
  if (s == "") return 0
  if (length(s) < 30 || length(s) > 240) return 0
  c = substr(s, 1, 1)
  if (c ~ /^[]["'`(){}*_.,;:!?#>—–-]/) return 0
  return 1
}
# para_line: alimenta uma linha do corpo no acumulador do canal ch ("f"=1o parágrafo do
# corpo inteiro, "d"=1o parágrafo dentro de uma seção ### Decisão). Fecha o parágrafo em
# linha branca/heading/---; se o parágrafo fechado NÃO era metadado, tenta commitá-lo
# (uma tentativa só — falhar validação não passa pro parágrafo seguinte, vira "none").
function para_line(ch, line) {
  if (ptried[ch]) return
  if (!pactive[ch]) return
  if (line ~ /^[[:space:]]*$/ || line ~ /^#/ || line ~ /^---[ \t]*$/) {
    if (pbufopen[ch]) {
      if (!pbufmeta[ch]) para_commit(ch)
      pbufopen[ch] = 0; pbuf[ch] = ""; pbufmeta[ch] = 0
    }
    return
  }
  if (!pbufopen[ch]) { pbufopen[ch] = 1; pbufmeta[ch] = is_metadata_start(line); pbuf[ch] = trim(line) }
  else pbuf[ch] = pbuf[ch] " " trim(line)
}
function para_commit(ch,    cleaned, sent) {
  cleaned = strip_prefix_markup(pbuf[ch])
  sent = first_sentence(cleaned)
  if (valid_sentence(sent)) presult[ch] = sent
  ptried[ch] = 1
}
function para_finalize(ch) {
  if (!ptried[ch] && pbufopen[ch] && !pbufmeta[ch]) para_commit(ch)
}
function iso_from_br(d,    a) { split(d, a, "/"); return a[3] "-" a[2] "-" a[1] }

function flush(   ) {
  para_finalize("f"); para_finalize("d")
  if (cur != "") {
    d0 = presult["d"]; f0 = presult["f"]
    if (regra == "") { if (d0 != "") { regra = d0; regrasrc = "decisao" } else if (f0 != "") { regra = f0; regrasrc = "paragrafo" } else regrasrc = "none" }
    bodydata = (bodydata_label != "") ? bodydata_label : bodydateline
    n++
    ord[n] = cur; rawnum[n] = curraw; lineno[n] = curline; title_a[n] = t
    escopo_a[n] = e_escopo; saga_a[n] = e_saga; data_a[n] = e_data; refs_a[n] = e_refs
    consolida_a[n] = e_consolida; supersede_a[n] = e_supersede; medido_a[n] = e_medido
    regra_a[n] = regra; regrasrc_a[n] = regrasrc
    motivo_a[n] = motivo; tradeoff_a[n] = tradeoff; licao_a[n] = licao
    bodydata_a[n] = bodydata; corpolines_a[n] = bl
    haspaste_a[n] = (commitcount >= 3 || efficiency_seen == 1 || tablelines >= 2) ? 1 : 0
    historico_a[n] = historico_list
    if (e_supersede != "") mark_targets("sup", e_supersede, curraw)
    if (e_consolida != "") mark_targets("con", e_consolida, curraw)
  }
  cur=""; curraw=""; curline=0; t=""
  e_escopo=""; e_saga=""; e_data=""; e_refs=""; e_consolida=""; e_supersede=""; e_medido=""
  regra=""; regrasrc="none"; motivo=""; tradeoff=""; licao=""; bodydata=""; bodydata_label=""; bodydateline=""; d0=""; f0=""
  bl=0; commitcount=0; efficiency_seen=0; tablelines=0
  inhist=0; historico_list=""; tagline_seen=0
  ptried["f"]=0; ptried["d"]=0; pactive["f"]=1; pactive["d"]=0
  pbufopen["f"]=0; pbufopen["d"]=0; pbuf["f"]=""; pbuf["d"]=""; pbufmeta["f"]=0; pbufmeta["d"]=0
  presult["f"]=""; presult["d"]=""
}
/^## DA-[0-9]+/ { inbody = 1 }
!inbody && /^- \*\*DA-[0-9]+\*\*/ && /[Ss]upersed[a-z]* (pela|por) DA-[0-9]+/ {
  match($0, /DA-[0-9]+/); self = substr($0, RSTART+3, RLENGTH-3)
  rest = substr($0, RSTART+RLENGTH)
  if (match(rest, /[Ss]upersed[a-z]* (pela|por) DA-[0-9]+/)) {
    seg = substr(rest, RSTART, RLENGTH); match(seg, /DA-[0-9]+/)
    tgt = substr(seg, RSTART+3, RLENGTH-3)
    if (!(self in supby)) supby[self] = tgt
  }
}
/^## DA-[0-9]+ — / {
  flush()
  match($0, /DA-[0-9]+/); curraw = substr($0, RSTART+3, RLENGTH-3)
  cur = "DA-" curraw; curline = NR
  t = $0; sub(/^## DA-[0-9]+ — /, "", t)
  bl = 0
  next
}
cur == "" { next }
{
  line = $0; bl++
  # tag line = linha que COMEÇA com `chave: ...` (escopo/saga/...). Prosa que só CITA "`escopo:" no meio
  # ("DA-NNN passa a `escopo: instância`.") não é tag line — antes era engolida aqui e a heurística
  # "passa a escopo" (½ parcial) nunca a via. Regressão medida na task 20260906-034; teste no smoke.
  if (bl <= 3 && !tagline_seen && line ~ /^`[a-z-]+:/ && (index(line, "`escopo:") > 0 || index(line, "`saga:") > 0)) {
    tagline_seen = 1
    s = line
    while (match(s, /`[^`]+`/)) {
      tag = substr(s, RSTART+1, RLENGTH-2); s = substr(s, RSTART+RLENGTH)
      ci = index(tag, ":")
      if (ci > 0) {
        key = trim(substr(tag, 1, ci-1)); val = trim(substr(tag, ci+1))
        if (val == "—" || val == "-") val = ""
        if (key == "escopo") e_escopo = csv_add(e_escopo, val)
        else if (key == "saga") { k2 = split(val, sa, ","); for (j2=1;j2<=k2;j2++) e_saga = csv_add(e_saga, trim(sa[j2])) }
        else if (key == "data") e_data = val
        else if (key == "refs") e_refs = csv_add(e_refs, val)
        else if (key == "consolida") e_consolida = csv_add(e_consolida, val)
        else if (key == "supersede") e_supersede = csv_add(e_supersede, val)
        else if (key == "medido") e_medido = val
      }
    }
    next
  }
  if (regrasrc != "regra" && regra == "" && match(line, /^\*\*Regra:?\*\*[:]?[ \t]*/)) { regra = substr(line, RLENGTH+1); regrasrc = "regra" }
  if (motivo == "" && match(line, /^\*\*Motivo:?\*\*[:]?[ \t]*/)) motivo = substr(line, RLENGTH+1)
  if (tradeoff == "" && match(line, /^\*\*Trade-off:?\*\*[:]?[ \t]*/)) tradeoff = substr(line, RLENGTH+1)
  if (licao == "" && match(line, /^\*\*Li(ção|cao):?\*\*[:]?[ \t]*/)) licao = substr(line, RLENGTH+1)
  # data do corpo: (2) linha "**Data:** YYYY-MM-DD" rotulada — pega o PREFIXO ISO (o resto da
  # linha pode ter hora "20:07", parêntese "(noite autônoma)" etc.; só a data importa aqui)
  if (bodydata_label == "" && match(line, /\*\*Data:?\*\*[:]?[ \t]*/)) {
    bl_cand = substr(line, RSTART+RLENGTH)
    if (match(bl_cand, /^[0-9]{4}-[0-9]{2}-[0-9]{2}/)) bodydata_label = substr(bl_cand, RSTART, RLENGTH)
  }
  # data do corpo: (3) 1a data (BR DD/MM/YYYY ou ISO) que abre a linha isolada — convenção comum
  # quando não há rótulo "Data:" (ex.: "**23/08/2026** · refs ..."). NÃO pega data em prosa
  # solta no meio da frase (ex.: "[fulano, 22/07/2026, msg 1051]") — só a que abre a linha.
  if (bodydateline == "" && match(line, /^\*{0,2}[0-9]{2}\/[0-9]{2}\/[0-9]{4}\*{0,2}/)) {
    rawdt = substr(line, RSTART, RLENGTH); gsub(/\*/, "", rawdt); bodydateline = iso_from_br(rawdt)
  } else if (bodydateline == "" && match(line, /^\*{0,2}[0-9]{4}-[0-9]{2}-[0-9]{2}\*{0,2}/)) {
    rawdt = substr(line, RSTART, RLENGTH); gsub(/\*/, "", rawdt); bodydateline = rawdt
  }
  if (!ptried["d"] && !pactive["d"]) {
    if (line ~ /^### Decis/ || line ~ /^\*\*Decis[^*]*\*\*[[:space:]]*$/) {
      pactive["d"] = 1
    } else if (match(line, /^\*\*Decis[^*]*\*\*[[:space:]]*:?[[:space:]]*/) && length(line) > RLENGTH) {
      pactive["d"] = 1
      para_line("d", substr(line, RLENGTH+1))
    }
  } else if (!ptried["d"]) {
    para_line("d", line)
    if (line ~ /^#/) pactive["d"] = 0
  }
  if (!ptried["f"]) para_line("f", line)
  if (line ~ /^### Hist(ó|o)rico/) inhist = 1
  else if (inhist && line ~ /^(##|---)/) inhist = 0
  else if (inhist && line ~ /^- DA-[0-9]/) historico_list = historico_list (historico_list=="" ? "" : ",") refs_in(line)
  if (line ~ /^commit [0-9a-f]{40}$/) commitcount++
  if (index(line, "Efficiency meter") > 0) efficiency_seen = 1
  if (index(line,"────")>0 || index(line,"━━━━")>0 || index(line,"════")>0 || index(line,"░░░░")>0 || index(line,"▓▓▓▓")>0 || index(line,"█████")>0) tablelines++
  if (line ~ /(NÃO|não|nao|Não) supersede/) next
  if (match(line, /[Ss]upersed(ida|ed)[a-z]* (pela|por) DA-[0-9]+/)) {
    seg = substr(line, RSTART, RLENGTH); match(seg, /DA-[0-9]+/)
    tgt = substr(seg, RSTART+3, RLENGTH-3)
    if (!(curraw in supby)) supby[curraw] = tgt
    next
  }
  if (match(line, /[Ss]upersede[a-z]*/)) {
    seg = substr(line, RSTART)
    if (match(seg, /[;.]/)) seg = substr(seg, 1, RSTART-1)
    r = refs_in_plain(seg)
    if (r != "") {
      if (seg ~ /(PARCIAL|parcial|a linha|o escopo|a regra|o crit(é|e)rio|no campo)/) mark_targets("par", r, curraw)
      else mark_targets("sup", r, curraw)
    }
    next
  }
  if (match(line, /corrige o escopo[a-z ]* d[aeo][a-z]* DA-[0-9]+/) || (line ~ /passam? a `?escopo:/ && line ~ /DA-[0-9]+/)) {
    mark_targets("par", refs_in_plain(line), curraw)
  }
}
END {
  flush()
  for (i=1;i<=n;i++) totalcnt[rawnum[i]]++
  for (i=1;i<=n;i++) {
    if (totalcnt[rawnum[i]] > 1) { seen[rawnum[i]]++; suf = sprintf("%c", 96+seen[rawnum[i]]); key_a[i] = "DA-" rawnum[i] suf }
    else key_a[i] = "DA-" rawnum[i]
  }
  for (i=1; i<=n; i++) {
    ksuf = key_a[i]; sub(/^DA-/, "", ksuf)
    ms = (ksuf in supby) ? supby[ksuf] : ((rawnum[i] in supby) ? supby[rawnum[i]] : "")
    mp = (ksuf in parby) ? parby[ksuf] : ((rawnum[i] in parby) ? parby[rawnum[i]] : "")
    mc = (ksuf in conby) ? conby[ksuf] : ((rawnum[i] in conby) ? conby[rawnum[i]] : "")
    out = key_a[i] SEP rawnum[i] SEP lineno[i] SEP title_a[i] SEP \
          escopo_a[i] SEP saga_a[i] SEP data_a[i] SEP refs_a[i] SEP \
          consolida_a[i] SEP supersede_a[i] SEP medido_a[i] SEP \
          regra_a[i] SEP regrasrc_a[i] SEP motivo_a[i] SEP tradeoff_a[i] SEP \
          licao_a[i] SEP bodydata_a[i] SEP corpolines_a[i] SEP haspaste_a[i] SEP \
          historico_a[i] SEP ms SEP mp SEP mc
    print out
  }
}
AWKEOF
}

declare -a KEY RAWNUM LINE TITLE ESCOPO SAGA DATATAG REFS CONSOLIDA SUPERSEDE MEDIDO
declare -a REGRA REGRASRC MOTIVO TRADEOFF LICAO BODYDATA CORPOLINES HASPASTE HISTORICO MSUP MPAR MCON
N=0

load_records() {
  local dir="$1"; local dec="$dir/DECISIONS.md"
  [ -f "$dec" ] || { echo "✗ da-index: $dec não existe — dir='$dir'" >&2; exit 1; }
  [ -r "$dec" ] || { echo "✗ da-index: $dec sem permissão de leitura" >&2; exit 1; }
  KEY=(); RAWNUM=(); LINE=(); TITLE=(); ESCOPO=(); SAGA=(); DATATAG=(); REFS=(); CONSOLIDA=(); SUPERSEDE=(); MEDIDO=()
  REGRA=(); REGRASRC=(); MOTIVO=(); TRADEOFF=(); LICAO=(); BODYDATA=(); CORPOLINES=(); HASPASTE=(); HISTORICO=(); MSUP=(); MPAR=(); MCON=()
  local i=0
  while IFS="$SEP" read -r f1 f2 f3 f4 f5 f6 f7 f8 f9 f10 f11 f12 f13 f14 f15 f16 f17 f18 f19 f20 f21 f22 f23; do
    KEY[i]="$f1"; RAWNUM[i]="$f2"; LINE[i]="$f3"; TITLE[i]="$f4"; ESCOPO[i]="$f5"; SAGA[i]="$f6"; DATATAG[i]="$f7"
    REFS[i]="$f8"; CONSOLIDA[i]="$f9"; SUPERSEDE[i]="$f10"; MEDIDO[i]="$f11"; REGRA[i]="$f12"; REGRASRC[i]="$f13"
    MOTIVO[i]="$f14"; TRADEOFF[i]="$f15"; LICAO[i]="$f16"; BODYDATA[i]="$f17"; CORPOLINES[i]="$f18"; HASPASTE[i]="$f19"
    HISTORICO[i]="$f20"; MSUP[i]="$f21"; MPAR[i]="$f22"; MCON[i]="$f23"
    i=$((i+1))
  done < <(awk "$(_parse_awk)" "$dec")
  N=$i
}

mark_of() {   # "sup:x" | "par:y" | ""
  local i="$1"
  [ -n "${MSUP[i]}" ] && { echo "sup:${MSUP[i]}"; return; }
  [ -n "${MPAR[i]}" ] && { echo "par:${MPAR[i]}"; return; }
  echo ""
}
mark_render() {   # "🔄 por DA-x" | "½ por DA-y" | ""
  local m; m="$(mark_of "$1")"
  [ -z "$m" ] && { echo ""; return; }
  case "${m%%:*}" in sup) echo "🔄 por DA-${m#*:}" ;; par) echo "½ por DA-${m#*:}" ;; esac
}

trunc() {  # trunc "$texto" N — corta na última palavra que cabe, nunca no meio
  local s="$1" n="$2" cut
  if [ "${#s}" -le "$n" ]; then printf '%s' "$s"; return; fi
  cut="${s:0:$((n-3))}"
  case "$cut" in *' '*) cut="${cut% *}" ;; esac
  printf '%s...' "$cut"
}

nv() { echo $((10#${1:-0})); }   # valor numérico seguro ("008" não é octal)

index_line_for() {
  local i="$1"
  local escopo="${ESCOPO[i]:-—}"; local saga="${SAGA[i]:-—}"
  local marca; marca="$(mark_render "$i")"
  local regra="${REGRA[$i]}"; [ "${REGRASRC[$i]}" = "none" ] && regra=""
  local sufixo=""; [ -n "$marca" ] && sufixo=" · $marca"
  printf -- "- %s · %s · %s%s — %s%s\n" \
    "${KEY[$i]}" "$escopo" "$saga" "$sufixo" "${TITLE[$i]}" "$([ -n "$regra" ] && echo " — $(trunc "$regra" 140)" || echo "")"
}

build_index() {
  local dir="$1"; local dec="$dir/DECISIONS.md"
  local sha; sha="$(sha256sum "$dec" | cut -c1-16)"
  echo "# Índice de Decisões — GERADO de DECISIONS.md (NÃO EDITE À MÃO)"
  echo "# Regenerar: bash scripts/da-index.sh update · Conferir: bash scripts/da-index.sh check"
  echo "# Sem marca = vigente · 🔄 por DA-x = SUPERSEDIDA (íntegra) · ½ por DA-y = alterada (parcial — o resto vale) · texto integral no DECISIONS.md"
  echo "# fonte: $N DAs · sha256(DECISIONS.md)=$sha"
  echo
  local i
  for ((i=0; i<N; i++)); do index_line_for "$i"; done
}

_epoch_dias() {  # AAAA-MM-DD → dias desde 1970 (Howard Hinnant days_from_civil), em _EPOCH
  local y=${1:0:4} m=${1:5:2} d=${1:8:2}
  y=$((10#$y)); m=$((10#$m)); d=$((10#$d))
  [ "$m" -le 2 ] && y=$((y-1))
  local era=$(( (y >= 0 ? y : y-399) / 400 ))
  local yoe=$((y - era*400)) mp=$(( (m + 9) % 12 ))
  local doy=$(( (153*mp + 2)/5 + d - 1 ))
  local doe=$(( yoe*365 + yoe/4 - yoe/100 + doy ))
  _EPOCH=$(( era*146097 + doe ))
}

run_quality_checks() {   # WARN, nunca FAIL: quem decide se é defeito é gente
  local dir="$1" i k
  for ((i=0; i<N; i++)); do
    [ "${CORPOLINES[i]}" -gt 120 ] && echo "WARN c1: ${KEY[i]} corpo com ${CORPOLINES[i]} linhas (teto 120)"
    [ "${HASPASTE[i]}" = "1" ] && echo "WARN c2: ${KEY[i]} parece ter paste de terminal — evidência vai pro anexo"
  done
  declare -A cnt
  for ((i=0; i<N; i++)); do cnt["${RAWNUM[i]}"]=$(( ${cnt["${RAWNUM[i]}"]:-0} + 1 )); done
  for k in "${!cnt[@]}"; do [ "${cnt[$k]}" -gt 1 ] && echo "WARN c4: DA-$k duplicada (número nunca se reusa)"; done
  for ((i=0; i<N; i++)); do
    local falt=""
    [ -z "${DATATAG[i]}" ] && falt+="data "
    [ "${REGRASRC[i]}" != "regra" ] && falt+="Regra "
    [ -n "$falt" ] && echo "WARN c5: ${KEY[i]} sem: $falt(tag \`data:\` e linha **Regra:** são obrigatórias)"
  done
  for ((i=0; i<N; i++)); do
    local l="${LICAO[i]}"; [ -z "$l" ] && continue
    case "$l" in *"~/"*|*"/home/"*|*".sh"*|*".py"*|*"scripts/"*) echo "WARN c6: ${KEY[i]} Lição parece regra disfarçada (caminho/extensão): ${l:0:80}" ;; esac
  done
  local dec="$dir/DECISIONS.md" cited unknown=""
  declare -A exists
  for ((i=0; i<N; i++)); do exists["$(nv "${RAWNUM[i]}")"]=1; done
  # números dentro de `passa-a-limpo: …` apontam para um diário ANTERIOR (caderno congelado em DECISIONS-arquivo/), não para este — não são citação local
  for cited in $(sed -E 's/`passa-a-limpo:[^`]*`//g' "$dec" 2>/dev/null | grep -oE 'DA-[0-9]+' | sort -u); do
    [ -z "${exists[$(nv "${cited#DA-}")]:-}" ] && unknown+="$cited "
  done
  [ -n "$unknown" ] && echo "WARN c11: citações a número inexistente no diário: $unknown"
  # c12: 3 DAs com a MESMA tag saga: em ≤7 dias sem supersede: → conserto de sintoma? vá na raiz antes da 4ª
  declare -A by_saga
  local s
  for ((i=0; i<N; i++)); do
    [ -z "${SAGA[i]}" ] && continue
    IFS=',' read -ra _sg <<< "${SAGA[i]}"
    for s in "${_sg[@]}"; do s="${s// /}"; [ -n "$s" ] && by_saga["$s"]+="$i "; done
  done
  for s in "${!by_saga[@]}"; do
    # shellcheck disable=SC2206
    local -a idx=(${by_saga[$s]}) ds=() sup=() keys=()
    local j
    for j in "${idx[@]}"; do
      local dt="${DATATAG[j]:-${BODYDATA[j]}}"
      [[ "$dt" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || continue
      _epoch_dias "$dt"; ds+=("$_EPOCH"); sup+=("${SUPERSEDE[j]}"); keys+=("${KEY[j]}")
    done
    for ((j=2; j<${#ds[@]}; j++)); do
      if [ -z "${sup[j]}" ] && [ -z "${sup[j-1]}" ] && [ -z "${sup[j-2]}" ] && [ $(( ds[j] - ds[j-2] )) -le 7 ]; then
        echo "WARN c12: saga '$s' — 3ª rodada em ≤7 dias sem supersede: (${keys[j-2]}, ${keys[j-1]}, ${keys[j]}) — vá na raiz antes da 4ª"; break
      fi
    done
  done
  return 0
}

usage() {
  sed -n '3,6p' "$0"; exit 2
}

cmd_update() {
  local dir="${1:-$DA_DIR_DEFAULT}"
  load_records "$dir"
  local tmp; tmp="$(mktemp)"
  build_index "$dir" > "$tmp" || { rm -f "$tmp"; echo "✗ da-index: geração do índice falhou"; exit 1; }
  mv "$tmp" "$dir/DECISIONS-INDEX.md"
  echo "✓ da-index: DECISIONS-INDEX.md ($N DAs)"
  run_quality_checks "$dir"
}

cmd_check() {
  local dir="${1:-$DA_DIR_DEFAULT}"
  [ -f "$dir/DECISIONS-INDEX.md" ] || { echo "✗ da-index: $dir/DECISIONS-INDEX.md NÃO EXISTE — rode: bash scripts/da-index.sh update"; exit 1; }
  load_records "$dir"
  local tmp rc=0; tmp="$(mktemp)"
  build_index "$dir" > "$tmp"
  if ! cmp -s "$tmp" "$dir/DECISIONS-INDEX.md"; then
    echo "✗ da-index: DECISIONS-INDEX.md DIVERGE do DECISIONS.md"; diff "$dir/DECISIONS-INDEX.md" "$tmp" 2>/dev/null | head -10; rc=1
  fi
  rm -f "$tmp"
  run_quality_checks "$dir"
  [ "$rc" = 0 ] && echo "✓ da-index: DECISIONS-INDEX.md sincronizado com DECISIONS.md"
  exit "$rc"
}

cmd_list() {
  local escopo="" saga_f="" vigentes=0 desde="" dir="$DA_DIR_DEFAULT"
  while [ $# -gt 0 ]; do
    case "$1" in
      --escopo) escopo="$2"; shift 2 ;;
      --saga) saga_f="$2"; shift 2 ;;
      --vigentes) vigentes=1; shift ;;
      --desde) desde="$2"; shift 2 ;;
      *) dir="$1"; shift ;;
    esac
  done
  load_records "$dir"
  local i
  for ((i=0; i<N; i++)); do
    if [ -n "$escopo" ]; then
      local match=0 fe se
      IFS=',' read -ra _fe <<< "$escopo"; IFS=',' read -ra _se <<< "${ESCOPO[i]}"
      for fe in "${_fe[@]}"; do for se in "${_se[@]}"; do [ "$se" = "$fe" ] && match=1; done; done
      [ "$match" = 0 ] && continue
    fi
    [ -n "$saga_f" ] && { [[ ",${SAGA[i]}," == *",$saga_f,"* ]] || continue; }
    [ "$vigentes" = 1 ] && [ -n "$(mark_of "$i")" ] && continue
    if [ -n "$desde" ]; then local d="${DATATAG[i]:-${BODYDATA[i]}}"; [[ "$d" < "$desde" ]] && continue; fi
    index_line_for "$i"
  done
}

collapse_paste() {
  awk '
    function is_paste_start(l) {
      return (l ~ /^commit [0-9a-f]{40}$/) || (index(l,"Efficiency meter")>0) || \
             (index(l,"────")>0) || (index(l,"━━━━")>0) || (index(l,"════")>0) || (index(l,"░░░░")>0) || \
             (index(l,"▓▓▓▓")>0) || (index(l,"█████")>0)
    }
    function is_prose(l) { return (l ~ /^\*\*[^*]+:?\*\*/) || (l ~ /^### /) }
    {
      if (!inpaste && is_paste_start($0)) { inpaste=1; skipped=0 }
      if (inpaste) {
        if (is_prose($0)) { if (skipped>0) print "  [... bloco de paste omitido (" skipped " linhas) ...]"; inpaste=0; print; next }
        skipped++; next
      }
      print
    }
    END { if (inpaste && skipped>0) print "  [... bloco de paste omitido (" skipped " linhas) ...]" }
  '
}

cmd_show() {
  local arg="${1:-}" dir="${2:-$DA_DIR_DEFAULT}"
  case "$arg" in ""|--*) echo "uso: da-index.sh show DA-NNN [dir]   (show --saga saiu nesta versão: use list --saga <slug>)"; exit 2 ;; esac
  load_records "$dir"
  local i found=-1
  for ((i=0; i<N; i++)); do [ "${KEY[i]}" = "$arg" ] && { found=$i; break; }; done
  [ "$found" = -1 ] && { echo "✗ da-index show: $arg não encontrada"; exit 1; }
  local start="${LINE[found]}" end
  if [ $((found+1)) -lt "$N" ]; then end=$(( ${LINE[$((found+1))]} - 1 )); else end='$'; fi
  sed -n "${start},${end}p" "$dir/DECISIONS.md" | collapse_paste
}

MODE="${1:-}"; shift || true
case "$MODE" in
  update) cmd_update "$@" ;;
  check) cmd_check "$@" ;;
  list) cmd_list "$@" ;;
  show) cmd_show "$@" ;;
  sagas|export-saga) echo "✗ da-index: '$MODE' saiu nesta versão (sagas/cabeças) — use: list --saga <slug>; a versão anterior está na tag v3-full do repo adas"; exit 2 ;;
  *) usage ;;
esac

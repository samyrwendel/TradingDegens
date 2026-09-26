#!/usr/bin/env bash
# check-da-mecanismo — DA que PROÍBE precisa de MECANISMO que reprova a violação.
# Regra do ADAS: DA que proíbe só vale com trava que reprova a violação.
#
# Uma DA cuja linha **Regra:** (o parágrafo inteiro, não o corpo da DA) contenha
# proibição ou exclusividade — nunca · proibido · não pode · jamais · somente · só —
# tem que trazer uma linha **Mecanismo:** (ou **Mecanismo (…):**) citando pelo menos UM caminho (arquivo,
# diretório ou unit systemd) que EXISTE no disco. Sem isso:
#   FAIL  se a DA tem data >= DA_MECANISMO_DESDE (padrão 2026-09-26) — DA nova não entra;
#   WARN  se é anterior — vira fila (levantamento retroativo), não bloqueia.
# "só" descritivo dentro da Regra não é isento: reescreva a Regra, não isente.
#
# Uso: check-da-mecanismo.sh [--list] [DECISIONS.md] [raiz]
#   --list  imprime também os WARN um a um (padrão: só a contagem)
#   raiz    onde resolver caminho relativo do Mecanismo (padrão: dir do DECISIONS.md)
# Saída: linhas "FAIL DA-NNN …" / "WARN DA-NNN …" e um resumo; exit 1 se houver FAIL.
set -uo pipefail
export LC_ALL=C.UTF-8  # classe de caractere e -i do grep sobre acento (ó, ã) precisam de UTF-8

LIST=0; ARGS=()
for a in "$@"; do case "$a" in --list) LIST=1 ;; *) ARGS+=("$a") ;; esac; done
DEC="${ARGS[0]:-DECISIONS.md}"
[ -f "$DEC" ] || { echo "check-da-mecanismo: $DEC não existe"; exit 2; }
ROOT="${ARGS[1]:-$(cd "$(dirname "$DEC")" && pwd)}"
DESDE="${DA_MECANISMO_DESDE:-2026-09-26}"
# DA SEM data (formato de diário sem `data:`) cairia sempre no WARN — brecha. Corte por número:
# DA-NNN acima do número gravado em <raiz>/.adas/da-mecanismo-corte (a maior DA na adoção) é nova.
CORTE_NUM="${DA_MECANISMO_CORTE_NUM:-$(tr -dc '0-9' 2>/dev/null < "$ROOT/.adas/da-mecanismo-corte")}"
PROIBE='(^|[^[:alnum:]_])(nunca|proibid[oa]s?|n[ãa]o pode|jamais|somente|s[óo])([^[:alnum:]_]|$)'

# Um registro por DA: id, data, regra, mecanismo separados por \037 (TAB colapsaria campo vazio no read) (parágrafos achatados em 1 linha).
# Parágrafo de campo = da linha **Campo:** até linha em branco, heading, --- ou o próximo **Campo:**.
extrai() {
  awk '
    function flush() { if (id != "") printf "%s\037%s\037%s\037%s\n", id, dt, regra, mec }
    function campo(l) { return l ~ /^\*\*[^*]+:\*\*/ }
    /^## (Decisão Arquitetural )?DA-[0-9]+/ {
      flush(); match($0, /DA-[0-9]+/); id = substr($0, RSTART, RLENGTH)
      dt = ""; regra = ""; mec = ""; em = ""; next
    }
    id == "" { next }
    /^#/ || /^---[[:space:]]*$/ || /^[[:space:]]*$/ { em = ""; }
    dt == "" && match($0, /`data: [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]`/) { dt = substr($0, RSTART + 7, 10) }
    dt == "" && /\*\*Data:\*\*/ && match($0, /[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]/) { dt = substr($0, RSTART, 10) }
    campo($0) {
      em = ""
      if ($0 ~ /^\*\*Regra[^*]*:\*\*/ && regra == "") { em = "r"; sub(/^\*\*Regra[^*]*:\*\*[ \t]*/, ""); regra = $0; next }
      if ($0 ~ /^\*\*Mecanismo[^*]*:\*\*/ && mec == "") { em = "m"; sub(/^\*\*Mecanismo[^*]*:\*\*[ \t]*/, ""); mec = $0 " "; next }
      next
    }
    em == "r" { regra = regra " " $0 }
    em == "m" { mec = mec " " $0 }
    END { flush() }
  ' "$1" | tr -d '\r'
}

# 0 se algum caminho citado no Mecanismo existe. Candidatos: tokens entre crases e
# tokens soltos com "/" ou extensão de arquivo/unit. Resolve ~/, absoluto, relativo à raiz,
# nome solto em raiz/ e raiz/scripts/, e unit via systemctl (user e system).
caminho_existe() {
  local mec="$1" t p
  while IFS= read -r t; do
    t="${t%%[),.;:]}"; t="${t#(}"
    [ -z "$t" ] && continue
    case "$t" in "~/"*) p="$HOME/${t#\~/}" ;; /*) p="$t" ;; *) p="$ROOT/$t" ;; esac
    [ -e "$p" ] && return 0
    case "$t" in */*) ;; *) [ -e "$ROOT/scripts/$t" ] && return 0 ;; esac
    case "$t" in
      *.service|*.timer|*.path|*.socket)
        systemctl --user cat "$t" >/dev/null 2>&1 && return 0
        systemctl cat "$t" >/dev/null 2>&1 && return 0 ;;
    esac
  done < <(
    { printf '%s\n' "$mec" | grep -oE '`[^`]+`' | tr -d '`' | tr ' ' '\n'
      printf '%s\n' "$mec" | tr ' ' '\n' | grep -E '/|\.(sh|py|js|ts|md|json|service|timer|path|socket)$'
    } | grep -vE '^\*|^-' | sort -u
  )
  return 1
}

nfail=0; nwarn=0; nproib=0; nok=0
while IFS=$'\037' read -r id dt regra mec; do
  printf '%s\n' "$regra" | grep -qiE "$PROIBE" || continue
  nproib=$((nproib + 1))
  motivo=""
  if [ -z "${mec// /}" ]; then
    motivo="Regra proíbe e não há **Mecanismo:**"
  elif ! caminho_existe "$mec"; then
    motivo="**Mecanismo:** não cita caminho/unit que exista no disco"
  fi
  if [ -z "$motivo" ]; then nok=$((nok + 1)); continue; fi
  nova=0
  if [ -n "$dt" ]; then [[ ! "$dt" < "$DESDE" ]] && nova=1
  elif [ -n "$CORTE_NUM" ] && [ "$((10#${id#DA-}))" -gt "$((10#$CORTE_NUM))" ]; then nova=1; fi
  if [ "$nova" = 1 ]; then
    echo "FAIL $id (${dt:-sem data, acima do corte DA-$CORTE_NUM}) — $motivo (DA nova: não entra sem trava)"; nfail=$((nfail + 1))
  else
    [ "$LIST" = 1 ] && echo "WARN $id (${dt:-sem data}) — $motivo"
    nwarn=$((nwarn + 1))
  fi
done < <(extrai "$DEC")

echo "check-da-mecanismo: $nproib DA(s) com proibição na Regra · $nok com mecanismo existente · $nfail FAIL (>= $DESDE) · $nwarn WARN (antigas, fila)"
[ "$nfail" -eq 0 ]

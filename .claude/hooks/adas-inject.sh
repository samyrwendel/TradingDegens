#!/usr/bin/env bash
# ADAS PreToolUse — injeta a spec destilada da faixa no INSTANTE da edição (Edit|Write|MultiEdit).
# FONTE ÚNICA chamável e versionada com o código: usada pelo .claude/settings.json deste repo E pelo
# roteador user-level (host/adas-route.sh do repo adas) quando a sessão nasce fora do repo.
# Duplique um bloco do case por faixa. REQUER 'jq' no PATH (sem jq: falha aberto, no-op silencioso).
f="$(jq -r '.tool_input.file_path // empty' 2>/dev/null)"
[ -z "$f" ] && exit 0

case "$f" in
  *"/<glob-da-faixa>/"*)
    ctx="ADAS <faixa>: <spec destilada em 1 parágrafo, com os PROIBIDOS e a regra-chave>. Spec: .claude/skills/<faixa>/SKILL.md" ;;
  *) exit 0 ;;
esac

printf '%s' "$ctx" | jq -Rs '{hookSpecificOutput:{hookEventName:"PreToolUse",additionalContext:.}}' 2>/dev/null || true
exit 0

#!/usr/bin/env bash
# PostToolUse (Edit|Write|MultiEdit): editou DECISIONS.md → o índice nasce junto.
payload="$(cat 2>/dev/null || true)"
f="$(printf '%s' "$payload" | jq -r '.tool_input.file_path // empty' 2>/dev/null || true)"
case "$f" in */DECISIONS.md) ;; *) exit 0 ;; esac
d="$(dirname "$f")"
[ -f "$d/scripts/da-index.sh" ] && bash "$d/scripts/da-index.sh" update "$d" >/dev/null 2>&1
exit 0

#!/usr/bin/env bash
# check-app-security — os QUATRO SLOTS COM PROVA das "seis portas do app" (faixa
# seguranca-acesso). As outras duas portas (chave no front, .env no histórico)
# são MECÂNICAS (grep puro) e vivem em scripts/check-secrets.sh — estas quatro não dão
# pra derivar por grep: validação-só-na-tela, arquivo-público, erro-fala-demais e
# rate-limit exigem um TESTE do próprio projeto (bater a rota sem credencial, forçar
# um erro, disparar N requisições seguidas). Este script não RODA o teste — ele
# CONFERE que a prova foi registrada ("declaração sem prova não conta").
#
# Lê .adas/seguranca-app.json:
#   { "<porta>": { "estado": "passa"|"na"|"debito", "evidencia": "<comando + resultado
#                  ou caminho do teste>", "data": "YYYY-MM-DD" }, ... }
#
# Veredito por slot:
#   arquivo ausente / chave ausente / estado "debito"  → WARN (débito adas: visível)
#   estado "passa" ou "na" SEM evidência                → FAIL (sem prova não conta)
#   estado "passa" ou "na" COM evidência                → PASS
#   estado fora do vocabulário (passa/na/debito)         → FAIL
#
# Uso:
#   bash scripts/check-app-security.sh              # lê .adas/seguranca-app.json na raiz
#   bash scripts/check-app-security.sh --dir <path>  # raiz alternativa (ex.: monorepo)
set -uo pipefail

base="."
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) base="${2:-.}"; shift ;;
  esac; shift
done

JSON="$base/.adas/seguranca-app.json"

# As QUATRO portas com prova (3, 4, 5, 6) — a lista é o contrato:
# check-adas.sh itera o MESMO array pra montar o resumo das seis portas.
PORTAS_COM_PROVA="validacao_servidor arquivo_publico erro_fala_demais rate_limit"
porta_rotulo() {
  case "$1" in
    validacao_servidor) echo "validação só na tela (porta 3)" ;;
    arquivo_publico) echo "arquivo público / link direto (porta 4)" ;;
    erro_fala_demais) echo "erro que fala demais (porta 5)" ;;
    rate_limit) echo "rate limit / teto de gasto (porta 6)" ;;
    *) echo "$1" ;;
  esac
}

fail=0; warn=0

if ! command -v jq >/dev/null 2>&1; then
  echo "⚠ [seis portas] jq ausente — não dá pra ler $JSON; tratando as 4 portas como débito"
  for p in $PORTAS_COM_PROVA; do
    echo "  adas: débito — $(porta_rotulo "$p") sem prova verificável (jq ausente pra ler $JSON)"
  done
  warn=1
  echo
  echo "⚠ check-app-security: instale jq pra este check valer algo"
  exit 0
fi

if [ ! -f "$JSON" ]; then
  echo "⚠ [seis portas] sem $JSON — nenhuma das 4 portas com prova foi atestada ainda"
  for p in $PORTAS_COM_PROVA; do
    echo "  adas: débito — $(porta_rotulo "$p") sem arquivo .adas/seguranca-app.json (nunca provada)"
  done
  echo
  echo "⚠ check-app-security: 4 débito(s) — rode o teste de cada porta e grave a evidência em $JSON"
  exit 0
fi

if ! jq -e . "$JSON" >/dev/null 2>&1; then
  echo "✗ [seis portas] $JSON não é JSON válido"
  echo
  echo "✗ check-app-security: arquivo de prova corrompido — corrija $JSON"
  exit 1
fi

for p in $PORTAS_COM_PROVA; do
  rot="$(porta_rotulo "$p")"
  entry=$(jq -c --arg k "$p" '.[$k] // empty' "$JSON" 2>/dev/null)
  if [ -z "$entry" ]; then
    echo "  adas: débito — $rot sem chave '$p' em $JSON (nunca provada)"; warn=1
    continue
  fi
  estado=$(printf '%s' "$entry" | jq -r '.estado // empty' 2>/dev/null)
  evidencia=$(printf '%s' "$entry" | jq -r '.evidencia // empty' 2>/dev/null)
  case "$estado" in
    debito)
      echo "  adas: débito — $rot (ver $JSON)"; warn=1 ;;
    passa|na)
      if [ -z "$evidencia" ] || [ "$evidencia" = "null" ]; then
        echo "✗ [seis portas] $rot: estado '$estado' SEM evidência — declaração sem prova não conta"
        fail=1
      else
        rotulo_estado="PASSA"; [ "$estado" = "na" ] && rotulo_estado="N/A (justificado)"
        echo "✓ [seis portas] $rot: $rotulo_estado — $evidencia"
      fi ;;
    *)
      echo "✗ [seis portas] $rot: estado '$estado' inválido (use passa|na|debito)"
      fail=1 ;;
  esac
done

echo
if [ "$fail" -ne 0 ]; then
  echo "✗ check-app-security: prova inválida — 'passa'/'na' sem evidência não vale, corrija $JSON"
  exit 1
fi
if [ "$warn" -ne 0 ]; then
  echo "⚠ check-app-security: débito visível (acima) — não bloqueia, mas fica no relatório"
  exit 0
fi
echo "✓ check-app-security: as 4 portas com prova estão PASSA ou N/A justificado"

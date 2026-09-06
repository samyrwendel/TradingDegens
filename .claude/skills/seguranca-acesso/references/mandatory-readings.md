# Leituras obrigatórias — faixa `seguranca-acesso`

> Antes de mexer em segredo/`.env`/operação de repo, confirme estes pontos no projeto real.

## Ler/confirmar SEMPRE
- `.gitignore` — cobre `.env`, `.env.*` (exceto `.env.example`/`.env.sample`), `*.pem`, `*.key`, pastas de credencial.
- `.env.example` (ou `.env.sample`) — existe e tem as chaves **SEM valores** (é o contrato; o `.env` real fica fora do git).
- O escopo do token em uso (se houver automação): é **fine-grained least-privilege**? (não `admin:org`/`delete_repo` pra push/PR).
- Onde o segredo de runtime vive vs. o segredo de CLI/git — **não** devem ser o mesmo.

## Checklist antes de "feito"
- [ ] `bash scripts/check-secrets.sh` passou (nenhum segredo de alta confiança).
- [ ] `git add` foi explícito (nenhum `.env`/segredo staged sem querer).
- [ ] Nenhum valor de segredo foi impresso/colado em log/chat/PR (só mascarado).
- [ ] Operação de repo (criar/apagar/mergear/visibilidade) foi autorizada explicitamente.

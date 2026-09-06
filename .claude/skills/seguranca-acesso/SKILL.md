---
name: seguranca-acesso
description: "Faixa de Segredos & Acesso — governa segredo, token, chave, .env, credencial, e operações de repositório (criar/apagar/mergear/tornar público). Use SEMPRE que a tarefa tocar: token/PAT do GitHub/cloud, chave de API, chave privada, .env/variável de ambiente, credencial, senha, segredo; criar/clonar/apagar repo, abrir/mergear PR, push/force-push, mudar visibilidade ou permissão, deploy key, webhook. Também em sintomas: 'vazou', 'expôs a chave', 'commitei o .env', 'token largo', 'qual permissão', 'tá seguro?', 'guarda essa chave', 'onde tá a credencial?'. Dispara MESMO que o usuário não use a palavra 'segredo', e dispara IGUAL quando o agente decide sozinho, por conta própria, criar/rotacionar/colar/ler uma credencial sem o dono pedir."
when_to_use: "Qualquer tarefa que manuseie segredo/credencial/token, .env, ou faça operação de repositório (criar/apagar/mergear/visibilidade). Enforcement: scripts/check-secrets.sh."
---

# Segredos & Acesso

> Faixa UNIVERSAL (as regras valem em qualquer projeto). Procedência: governança transversal do ADAS;
> complementa a constituição `.specs/`. Fonte da verdade = esta SKILL + `scripts/check-secrets.sh`.
> Caminho crítico = trate como dinheiro.

## Quando se aplica
Token/PAT, chave de API/privada, `.env`, credencial, senha, segredo; e operações de repo (criar/apagar/mergear PR,
push/force-push, mudar visibilidade/permissão, deploy key, webhook). Inclui os sintomas ("vazou", "commitei o .env").

## Leituras obrigatórias (antes de editar)
Ver `references/mandatory-readings.md` — confirmar `.gitignore` cobre `.env`/segredos, e que existe `.env.example` SEM valores.

## Regras — FAÇA
1. **Segredo NUNCA no repo.** `.env`/token/key no `.gitignore`; versionar só `.env.example`/`.env.sample` **sem valores**.
2. **Token least-privilege.** Use **fine-grained** (escopo por repo + só as permissões necessárias: Contents/Pull requests). Nunca PAT clássico amplo (`admin:org`, `delete_repo`, `workflow`) pra um trabalho de push/PR.
3. **Separe segredo de runtime (app `.env`) de segredo de CLI/git.** Não reusar o PAT de automação dentro do `.env` de uma app de produção.
4. **Rotacione ao menor sinal de vazamento** (segredo commitado, logado, colado em chat/PR). Assuma comprometido.
5. **Rode `scripts/check-secrets.sh` antes do commit** (e no deploy). Adote como pré-commit/gate.
6. **`git add <arquivos>` explícito** — nunca `-A`/`.` (varre `.env`/segredo/config sem querer). Mensagem com `Co-Authored-By` se IA.

## Regras — NÃO FAÇA
1. NUNCA commitar/colar/logar `.env`, token, chave privada, senha. NUNCA **imprimir o valor** de um segredo — só mascarado (`<set>`) ou metadado (scopes).
2. NUNCA **vasculhar credencial fora do arquivo/local explicitamente apontado** pelo usuário (não fazer fan-out por `.env`/cofres atrás de token — caça de credencial é violação, mesmo "pra ajudar").
3. NUNCA executar **ação irreversível de repo** — apagar repo, force-push em `main`, tornar público/privado, mudar permissão/colaborador, mergear PR — **sem confirmação explícita do humano**. Criar repo/abrir PR/merge = pede o "sim".
4. NUNCA usar um token amplo quando um fine-grained resolve. NUNCA deixar segredo em URL/query string.

## Seis portas do app

> Extraído do checklist de segurança de app gerado por IA (padrão de falha nº1 desses
> apps): seis buracos que se repetem projeto após projeto porque ninguém os checa de
> nascença. Todo projeto com ADAS nasce com as seis como obrigações verificáveis —
> **PASSA com evidência, N/A justificado, ou débito `adas:` visível**. Uma regra sem
> check não conta; uma declaração sem prova não conta. As duas primeiras são MECÂNICAS
> (grep, `scripts/check-secrets.sh`); as outras quatro são SLOTS COM PROVA
> (`scripts/check-app-security.sh` + `.adas/seguranca-app.json`) — exigem um teste do
> próprio projeto, não dá pra derivar por grep. Se o SEU projeto registrou a adoção
> como decisão própria, cite o número dela aqui (`DA-NNN`, no `DECISIONS.md` deste
> repo) — o esqueleto não assume um número que não é seu.

| # | Porta | O buraco | Como conferir | Como resolver |
|---|-------|----------|----------------|----------------|
| 1 | **Chave no front** | `NEXT_PUBLIC_`/`VITE_`/`REACT_APP_` resolvem em BUILD TIME — o valor vai pro bundle público mesmo "parecendo" server-side; ou uma chave literal (`sk-`, `AIza`, `ghp_`, `AKIA`, `xox[abp]-`) fica em `static/build/dist/public/templates`. | `bash scripts/check-secrets.sh --all` (mecânico). | Segredo real nunca leva prefixo público; se o front precisa de uma chave, é uma chave PÚBLICA por natureza (ex.: chave publishable do Stripe) ou passa por um proxy server-side. |
| 2 | **`.env` no histórico** | Arquivo apagado no HEAD continua vivo em algum commit antigo — `git log`/`grep` no histórico ainda o acha. | `bash scripts/check-secrets.sh --all` (mecânico: `git log --all --full-history` + `git rev-list --all \| git grep`). | **Rotacione o segredo ANTES de limpar o histórico** (apagar o arquivo no HEAD não invalida a credencial); só depois reescreva a história (`git filter-repo`/BFG) se for mesmo necessário. |
| 3 | **Validação só na tela** | O front esconde o botão/campo, mas a rota aceita a escrita de qualquer cliente que bater direto na API — preço, role ou user_id vindos do corpo da requisição, não derivados da sessão. | Slot com prova: bata a rota de escrita SEM cookie/token válido e confirme 401/403; tente mandar um preço/role diferente do que a sessão autoriza e confirme que o servidor ignora/recusa. Registre em `.adas/seguranca-app.json`. | Toda rota de escrita gateia no SERVIDOR antes de tocar o dado; preço/role/user_id sempre derivados da sessão, nunca aceitos do cliente. |
| 4 | **Arquivo público** | Upload/documento/backup exposto por link previsível (id sequencial, nome de arquivo adivinhável) sem controle de acesso. | Slot com prova: tente path traversal (`../../etc/passwd`, `..%2f..%2f`) → 404; confirme que link de arquivo sensível usa URL assinada com expiração, ou que não há rota estática nenhuma servindo diretório de upload. Registre em `.adas/seguranca-app.json`. | URL assinada com expiração curta, ou serve só por rota autenticada — nunca por caminho estático previsível. |
| 5 | **Erro que fala demais** | Resposta de erro devolve stack trace, caminho de arquivo, nome de tabela, ou diferencia "usuário não existe" de "senha errada" (enumeração de conta). | Slot com prova: force erro (id inexistente, JSON inválido, credencial errada) e confirme que o corpo é genérico; confirme que login devolve a MESMA mensagem pra usuário inexistente e senha errada; confirme que logs não gravam credencial/CPF em claro. Registre em `.adas/seguranca-app.json`. | Handler de erro genérico (sem detalhe interno) em toda resposta pro cliente; mensagem de auth sempre igual, não importa qual metade errou. |
| 6 | **Sem rate limit** | Nenhum teto de requisição em login (força-bruta viável) nem nas rotas caras (IA paga — custo sem controle). | Slot com prova: dispare N requisições seguidas em login e na rota cara e confirme recusa (429/backoff/CAPTCHA) depois de um teto; confirme que existe teto de gasto declarado no provedor de IA. Registre em `.adas/seguranca-app.json`. | Rate limit (token-bucket/janela) nas rotas de auth e nas custosas; teto de gasto configurado no provedor (ou no seu proxy de LLM). |

**Enforcement:** `scripts/check-secrets.sh` (portas 1-2) + `scripts/check-app-security.sh`
(fixture de teste que casa o padrão de chave — ex.: `Bearer test-…` — é isenta SÓ por `.adas/secrets-allowlist`,
arquivo versionado `caminho | motivo | data`; nunca por exceção no script)
(portas 3-6, lê `.adas/seguranca-app.json`) — os dois entram no gate junto dos demais
`check-*.sh`. `check-adas.sh` resume as seis numa linha só, por projeto.

## Trava obrigatória
Antes de qualquer operação de segredo/repo: confirme (a) que não há valor de segredo entrando no repo/log/chat, e (b) que a operação de repo é reversível OU foi explicitamente autorizada nesta conversa. Na dúvida, **pare e pergunte**.

# mecanismo/ — o modo MECANISMO materializado

**A regra do modo:** invariante sem gatilho NÃO entra. Regra que importa vira teste que falha,
deny no hook ou check no pre-commit; o resto é consulta por `scripts/da-index.sh show`.

**Por quê.** Medido onde não há humano no ato (frota de agentes headless): a regra no documento
carregado no contexto teve adesão zero — os erros aconteceram com a regra na janela; os gates
que negam com o conserto inline tiveram a maior adesão. Em repo com o dono presente isso não
foi medido: só relatado. Por isso existem dois modos, e este é o obrigatório sem humano no ato.

**O que este diretório traz:**
- `ADAS.mecanismo.md` — o template do `ADAS.md` deste modo (`adas-init.sh --modo mecanismo`
  o renomeia para `ADAS.md`). Cada invariante é UMA linha numerada com `mecanismo:` + `teste:`.
- `tests/check-secrets.test.sh` — o teste do gatilho de referência (`scripts/check-secrets.sh`,
  registrado no pre-commit por `scripts/install-hooks.sh`).

**O que ainda NÃO traz** (gatilhos de referência ficam para a próxima rodada): guard de worker
headless (nega `run_in_background`/suíte sem timeout), guard de afirmação falsa na resposta.

**O que NÃO conta como gatilho:** aviso, lembrete, texto injetado, frase no prompt — por mais
bem escritos. Só o que nega ou bloqueia. Por isso `check-mecanismo.sh` só aceita `mecanismo:`
apontando para arquivo executado por `.claude/settings.json`, pre-commit, `install-hooks.sh`,
`package.json`, `Makefile` ou workflow de CI.

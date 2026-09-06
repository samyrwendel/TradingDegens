# TradingDegens ADAS — Governança do Projeto (pacote para LLMs)
<!-- adas-modo: doc -->

> **O que é isto.** "ADAS" (Anti-Drift Adherence System) é o conjunto de **faixas/guard-rails** que
> descrevem o que este projeto já decidiu — vocabulário, cor, ciclo de vida do trade, dados, testes —
> para o assistente de IA usar o que existe em vez de inventar. Este arquivo é a destilação **autocontida**.
>
> **Modo doc: quem aplica a regra é o dono, presente na sessão.** Este documento lembra; ele não
> impede nada. O que a máquina cobra de verdade está em `scripts/check-*.sh` e no pre-commit.
>
> **Procedência.** Gerado em **2026-09-06** no passar a limpo do caderno central do servidor para o
> diário local (`DECISIONS.md`, **DA-001…DA-023**, uma DA por saga). **Fonte da verdade = `DECISIONS.md`
> e as faixas em `.claude/skills/`; se este doc divergir, regenere.** As faixas de design/vocabulário/
> arquitetura ainda NÃO foram extraídas para `.specs/` (placeholders): até lá, a regra vigente de cada
> assunto é a DA da saga correspondente.

---

<!-- adas-core-start -->
<!-- NÚCLEO: o que o runtime host (opcional) reinjeta em SessionStart/SubagentStart. ~30-45 linhas. -->
## Como usar (qualquer LLM)
1. **Leia ANTES de produzir qualquer coisa** — código, UI, texto, decisão, feature.
2. **Adesão > invenção.** Se já existe token/componente/padrão/decisão, **use o que existe**.
3. **Consolidar > reescrever · padronizar > inventar · medir antes de substituir · nunca regredir o que funciona.**
4. Lógica pode vir de referências; **a identidade visual/de marca NUNCA**. Nada mockado/hardcoded — fonte real.

### Escada de decisão — pare no 1º degrau que resolve *(padrão do [ponytail](https://github.com/DietrichGebert/ponytail), MIT)*
1. **Precisa existir?** (o pedido pede isso mesmo, agora?)
2. **Já existe no projeto?** Reusa o helper/componente/padrão que está lá.
3. **A stdlib resolve?** 4. **A plataforma faz nativo?** 5. **Uma dependência já instalada resolve?**
6. **Dá uma linha?** 7. **Só então:** o mínimo que funciona — menor diff, menos arquivos, deletar > adicionar.
> **Os não-negociáveis NÃO são "preguiça":** compreensão do problema, validação no limite de confiança,
> erro que evita perda de dado, segurança/acessibilidade e o **caminho do dinheiro testado** ficam SEMPRE
> (faixa `seguranca-acesso` + os `check-*.sh`).

### Atalho consciente = marcador `adas:` na linha exata *(débito honesto)*
```
# adas: gateado só neste card; varrer telas irmãs — ver DA-NNN
```
`scripts/adas-report.sh` conta faixas/DAs/débito/saúde e **se recusa a inventar "% de aderência"**.

### Mapa rápido — qual faixa para qual tarefa
| Sua tarefa toca… | Faixa |
|---|---|
| tela, cor, card, faixa, gráfico, mobile (`tradingagents/webui/static/`) | **Decisões** — sagas `paleta-td`, `camadas-grafico`, `faixa-do-card`, `leitura-por-card`, `grafico-controles`, `fantasma-td` |
| nome de setup/frame/fase/Éden, hora, veredito | **Decisões** — saga `vocabulario-canonico` (um produtor por eixo) |
| dado, cache, scan, agenda, ausência declarada | **Decisões** — sagas `cache-dados`, `cadencia-scan`, `ausencia-declarada`, `ultimo-scan-em-disco` |
| trade: entrada, desfecho, track record, Storm123, método do analista | **Decisões** — sagas `ciclo-desfecho`, `track-record`, `storm-eden`, `metodo-analista`, `carteira-analista`, `execucao-td` |
| segredo, token, `.env`, credencial, login do dono, operação de repo | **`seguranca-acesso`** + saga `portao-autoria-td` |
| tomar/mudar/questionar uma decisão · citar `DA-NNN` em código | **Decisões** (`DECISIONS.md`) + §3 "Dois numeradores" |
<!-- adas-core-end -->

---

## 0. Reforço automático (hooks — só Claude Code)
1. **JIT por faixa** — `PreToolUse` (`.claude/settings.json` → `.claude/hooks/adas-inject.sh`) injeta a
   faixa relevante a cada `Edit|Write|MultiEdit` de arquivo que casa com o glob da faixa (hoje só o
   template: nenhuma faixa de design foi extraída ainda).
2. **Índice de DAs** — `PostToolUse` em `DECISIONS.md` regenera `DECISIONS-INDEX.md` no ato.
3. **Pre-commit** (`scripts/install-hooks.sh`): segredo BLOQUEIA · `check-adas` · índice sincronizado ·
   `check-da-refs` (citação nova a DA que o diário não tem: aviso em modo doc).

**Não funciona em outra LLM** — por isso este documento existe.

## 1. FAIXA: Segredos & Acesso — `seguranca-acesso` (pronta)
**Quando aplicar:** token, chave, `.env`, credencial, login do dono, push/visibilidade do repo.
**Mecanismo:** `scripts/check-secrets.sh` (portas 1-2, mecânicas) + `.adas/seguranca-app.json`
(portas 3-6, com evidência; `scripts/check-app-security.sh` cobra). Regra do produto: saga
`portao-autoria-td` (portão de autoria ≠ portão de custo; isenção é allowlist).

## 2. FAIXA: Decisões — DA-NNN no `DECISIONS.md`
**Quando aplicar:** SEMPRE que uma decisão for tomada/mudada/questionada — **e em todo fix aprovado**.
Loop: decisão → `scripts/da-new.sh produto <saga> "<título>"` → preenche a DA + atualiza a faixa +
regenera este doc, **no mesmo commit**; **supersede, não delete**. Fix aprovado que representa uma
**CLASSE** de erro → a regra dobra na faixa que dispara no momento certo — aprendizado só em chat/doc
morto NÃO conta como registrado. Citação `DA-NNN` em código só a decisão que este diário TEM
(`scripts/check-da-refs.sh`, no pre-commit); entrada do caderno anterior se cita `caderno NNN` (§3).
Uma DA por saga no nascimento (2026-09-06); a 3ª rodada na mesma saga em ≤7 dias sem `supersede:` é
acusada pelo `check` (c12): vá na raiz antes da 4ª.

## 3. Dois numeradores, uma regra de leitura (o caderno e o diário)
Este repo convive com DOIS diários que usam a mesma sintaxe `DA-NNN`:

- **Caderno** — o diário central do servidor de origem (o `DECISIONS.md` da raiz de lá, numeração 001–238 em
  2026-09-06). As 101 entradas deste produto estão CONGELADAS, com os números originais, em
  `DECISIONS-arquivo/caderno-servidor-2026-08-23-a-2026-09-04.md`. Nunca se edita, nunca se renumera
  (a única alteração foi a preparação para publicação, declarada no cabeçalho do arquivo).
- **Diário** — `DECISIONS.md` deste repo, DA-001 em diante, nascido em 2026-09-06 como passar a limpo
  do caderno (a DA-001 conta o método; cada DA diz em `passa-a-limpo:` de quais entradas veio).

Regra de leitura de uma citação `DA-NNN` no código, teste, docstring ou commit:
1. **Linha commitada ANTES do commit de adoção** (o que criou `DECISIONS.md`:
   `git log --diff-filter=A --format='%h %ad' --date=short -- DECISIONS.md`) → **caderno**. São ~850
   citações em `.py/.js/.css/.html`; **não se reescrevem** — continuam verdadeiras, apontam para o arquivo.
2. **Linha commitada DEPOIS** → **diário**. Em linha nova, entrada do caderno se escreve **`caderno NNN`**.
3. **De-para:** tag `passa-a-limpo:` de cada DA (diário → caderno) e `DECISIONS-arquivo/de-para.md`
   (caderno → diário). Achar uma entrada do caderno: `grep -n '^## DA-NNN ' DECISIONS-arquivo/*.md`.
4. **Em dúvida sobre uma linha:** `git blame -L N,N --date=short ARQUIVO` e compare com a data do commit
   de adoção. Trabalho escrito antes e commitado depois (acontece) segue o item 2: o pre-commit avisa.
5. **Nunca se cita número que o diário ainda não tem** — `scripts/check-da-refs.sh` acusa a linha
   adicionada. Medido em 2026-09-06: o código não cita nenhum número ≤ 023 do caderno (os menores são
   033, 034 e 039); até o diário chegar ao número 033, toda citação nova a número inexistente é pega pelo
   check — a partir daí, os itens 1–2 decidem.

**Histórico que motivou a regra (04/09, task de 04/09):** entre 03/09 e 04/09 o código citou
números "adiantados" (201, 202, 204, 205, 214) que o caderno ainda não tinha; quando o caderno os
alcançasse, cada um seria uma decisão diferente, e o comentário apontaria em silêncio para o lugar
errado. De-para aplicado então (números do caderno): 201→190, 202/214→129, 205/204/214→190, e as demais
ocorrências de 205 divididas entre 190 (vocabulário do cabeçalho), 191 (sequência de 3 candles do
analista) e a nova 193 (eixo temporal único). A regra de então — "só se cita número que existe" — continua;
o que mudou é QUAL diário: o local.

### Índice de decisões
Gerado em **`DECISIONS-INDEX.md`** (`bash scripts/da-index.sh update`); ler uma: `bash scripts/da-index.sh show DA-NNN`.
Nascimento (2026-09-06): **DA-001** adoção e método · **DA-002…DA-023** uma por saga (dados, vocabulário,
método do analista, Storm123/Éden, ciclo/desfecho, track record, medição, cor/paleta, camadas do gráfico,
fantasma, faixa do card, leitura por card, controles, último scan, multiframe, cadência, ausência declarada,
carteira do analista, portão de autoria, publicar, suíte de testes, execução).

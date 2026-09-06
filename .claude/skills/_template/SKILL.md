---
name: <faixa-em-kebab-case>
# FORMATO = Anthropic Skill oficial (name + description). NÃO reinvente o formato — siga anthropics/skills
# (spec/ + template/) + skill-creator; o ADAS só soma a governança no CORPO. O triggering vai TODO no description.
# description = O ROTEADOR, e "PUSHY" (guidance oficial p/ combater UNDERtriggering): liste EXAUSTIVAMENTE
# todo sinônimo do domínio + SINTOMAS ("tá feio", "quebrado", "não aparece") e diga "use SEMPRE que ...,
# MESMO que o usuário não peça com essas palavras". Gatilho magro = faixa que nunca acorda. Otimize com o
# skill-creator (description-improver). when_to_use abaixo é OPCIONAL/extra (o oficial põe tudo no description).
description: "<O que esta faixa governa> — use SEMPRE que a tarefa tocar <lista exaustiva de gatilhos,
  palavras-chave, sinônimos e SINTOMAS>, MESMO que o usuário não diga explicitamente; e ao <consultar
  histórico / antes de refatorar algo decidido conscientemente>."
when_to_use: "<opcional, não-canônico> cenários + globs (glob/da/faixa/**)."
---

# <Nome da Faixa>

> Procedência: extraído de `.specs/<arquivo>` + DA-<NNN>. Fonte da verdade = `.specs/` + esta faixa.
> **Formato** segue o padrão oficial Anthropic Skills ([spec](https://github.com/anthropics/skills/tree/main/spec)
> · [template](https://github.com/anthropics/skills/tree/main/template) · `skill-creator`); o ADAS só soma a
> semântica de governança (procedência, trava, DA-NNN). Se divergir da fonte, regenere.

## Quando se aplica
<resumo dos gatilhos do frontmatter, em 1–2 linhas.>

## Fonte da verdade (NÃO hardcodar)
- Valores canônicos vivem em: `<caminho>` — reusar via `<helper/classe/import>`, nunca valor cru.
- Componentes/módulos canônicos: `<lista>` — REUSAR, nunca recriar.

## Inventário REUSE-FIRST (o que já existe e DEVE ser reusado antes de criar novo)
- `<Componente/Módulo>` — <pra quê serve>. Usar isto em vez de inventar.
- `<…>`

## Leituras obrigatórias (ANTES de editar — cura da reinvenção; padrão do spec-skills)
Antes de produzir, LER os arquivos reais listados em `references/mandatory-readings.md` (o componente
canônico, o token/spec, o exemplo que já existe). Reuso-por-construção: você só reinventa o que não leu.
Exemplos canônicos preenchidos ficam em `references/few-shots/` (a LLM imita o estilo da casa em vez de inventar).

## Trava obrigatória (pare em vez de chutar)
- NÃO prossiga se o alvo não estiver inequivocamente identificado; havendo ambiguidade (>1 arquivo/módulo/
  componente que casa), **pare e pergunte** — não adivinhe.
- Antes de qualquer edição, **localize e leia o código/spec real** do alvo. "Morto/ausente" exige verificação
  (handler dinâmico? registrado em outro lugar?) — confirmar antes de deletar/recriar.
- Esta faixa faz <X>; ela NÃO inventa estrutura/contrato fora do escopo nem cria valor canônico novo (isso é DA-NNN).

## Regras — FAÇA
1. <regra positiva concreta>
2. <…>

## Regras — NÃO FAÇA
1. NUNCA <anti-padrão>. (Legados PROIBIDOS: `<lista>`.)
2. <…>

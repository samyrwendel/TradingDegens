# Leituras obrigatórias — faixa `<nome>`

> Padrão importado do `spec-skills` (github.com/samyrwendel/adas, origem: spec-skills): a faixa **se recusa a escrever antes de
> ler o código/spec real**. É o antídoto estrutural à reinvenção — você só reinventa o que não leu.
> Liste aqui os arquivos REAIS (caminho exato) que a LLM TEM de abrir antes de produzir nesta faixa.

## Ler SEMPRE antes de editar nesta faixa
- `<caminho/do/componente-ou-módulo-canônico>` — o que reusar (não recriar).
- `<caminho/da/spec-ou-token>` — a fonte da verdade dos valores (não hardcodar).
- `<caminho/do/exemplo-que-já-existe>` — o estilo da casa a imitar.

## Few-shots (estilo canônico)
Exemplos preenchidos em `references/few-shots/` — a LLM pattern-matcheia o padrão certo em vez de inventar:
- `references/few-shots/<caso>.example.<ext>`

## Checklist antes de dar "feito"
- [ ] Li os arquivos acima e reusei o que já existe (inventário REUSE-FIRST da SKILL.md).
- [ ] Alvo inequívoco (sem ambiguidade) — senão parei e perguntei (trava obrigatória).
- [ ] Nenhum valor canônico novo criado sem DA-NNN.

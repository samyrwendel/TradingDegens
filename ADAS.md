# ADAS — TradingDegens

> Este projeto não tem faixas/skills próprias instaladas; a governança de decisões vive
> no diário CENTRAL do servidor (`~/DECISIONS.md`), não num `DECISIONS.md` local. Este
> arquivo existe só para registrar UMA convenção que já causou colisão real (task
> 20260904-007).

## Convenção de numeração de DA-NNN no código

**Toda referência `DA-NNN` em comentário/docstring deste repo aponta para uma entrada
REAL de `~/DECISIONS.md`.** Não existe numeração "conceitual" local nem prefixo
alternativo — o código nunca antecipa um número que o diário ainda não tem.

Antes de escrever `(DA-NNN)` num comentário, confira com
`~/scripts/da-index.sh show DA-NNN` (ou `grep "^## DA-" ~/DECISIONS.md`) que a entrada
já existe. Se a decisão ainda não foi registrada, registre-a primeiro
(`~/scripts/da-new.sh`) e só então cite o número real — nunca o contrário.

**Histórico do problema:** entre 03/09 e 04/09 o código passou a citar `DA-201`,
`DA-202`, `DA-204`, `DA-205` e `DA-214` como convenção "adiantada" em relação ao
diário (que estava em DA-190~192 no mesmo período). Quando o diário alcançasse esses
números, cada um seria uma decisão TOTALMENTE DIFERENTE, e o comentário apontaria em
silêncio para o lugar errado. Corrigido na task 20260904-007 (de-para completo no
commit): `DA-201`→`DA-190`, `DA-202/214`→`DA-129`, `DA-205/204/214`→`DA-190`, e as
demais ocorrências de `DA-205` (eixo temporal único) foram divididas entre `DA-190`
(vocabulário do cabeçalho), `DA-191` (sequência de 3 candles do Erick) e a nova
`DA-193` (eixo temporal único — registrada retroativamente, pois nunca tinha entrada
própria no diário).

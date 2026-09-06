# <PROJETO> — Constituição (.specs)

> A camada mais ESTÁVEL e COMPARTILHADA do ADAS. Vive fora das skills de um repo só porque é a raiz
> de onde as faixas são extraídas (todas as superfícies/repos derivam daqui). Muda raramente; quando
> muda, é decisão grande (registre uma DA-NNN). As faixas em `.claude/skills/*/SKILL.md` citam este
> arquivo como procedência.

## Identidade / Princípios invariantes
<1 parágrafo: o caráter do produto que NUNCA muda. Ex.: "dark premium, frio, técnico sem intimidar".>

## Valores canônicos (a fonte da verdade física)
- **Tokens crus:** `.specs/tokens.css` (cores/raios/fontes). Nunca hardcodar — sempre referenciar.
- **<outro invariante>:** <ex.: endereços de contrato / wallet de taxa / vocabulário de marca>.

## Espelhos (onde cada valor é COPIADO — propagar em TODOS ao mudar)
| Valor canônico | Espelho 1 | Espelho 2 | Espelho 3 |
|---|---|---|---|
| tokens (cor) | `<repo>/.../index.css` | `<repo>/.../tailwind.config.js` | `<repo>/.../theme.js` |
| <…> | | | |

## Regra de ouro
ADESÃO > INVENÇÃO. Se já existe token/componente/padrão/decisão, **use o que existe**. Lógica pode vir
de referências externas; **a identidade visual/de marca NUNCA**. Nada mockado/hardcoded — fonte real.

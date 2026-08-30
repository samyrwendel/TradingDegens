# Fila de UI — pedidos do Samyr (29/08/2026)

Anotado a pedido dele: **entra depois** das prioridades de correção (segurança do
`resume`/`cancel` e integridade do track record). Governança visual em `~/DECISIONS.md` DA-070.

## Regra que vale pra todos os itens (DA-070)

- **Card quadrado** — sem cantos arredondados de cartão, referência Quantfury.
- **Zero degradê** — nenhum `linear-gradient`/`radial-gradient` como cor de fundo em
  NENHUMA superfície: card, linha, chip, botão, barra. (Redação corrigida em 29/08 — a
  anterior dizia só "card, linha ou chip" e era mais estreita que a instrução do Samyr.)
  Estado continua sendo cor, mas chapada (preenchimento sólido ou borda-esquerda).
  Única exceção: degradê que carrega INFORMAÇÃO — a máscara tracejada da legenda EMA.
- Toda mudança aqui exige print antes/depois (DA-062).

## 1. Barra de controle — tudo numa linha só

Hoje a barra quebra e o bloco MODELOS fica pendurado à direita, cortado.

- Botões (Ativo · Data · Tempo · Método · Analisar · ↻) alinhados na **mesma linha**.
- Bloco **MODELOS** em **duas linhas** — `RÁPIDO` em cima, `PESADO` embaixo — mas o par
  contando como **um único elemento da barra**, sem quebrar a altura dos demais.

## 2. Card de resultado — veredito em cima, gatilhos embaixo à direita

- O **veredito da análise** (MANTER / COMPRAR / …) **permanece** onde está, no topo.
- Os **gatilhos** saem de onde estão e vão para o **canto inferior direito do card**,
  ao lado do preço atual, alinhados à direita.

## 3. Preço do 1-2-3 sempre no valor corrente (dado, não cosmética)

Toda análise 1-2-3 tem de buscar a cotação **atual** do ativo. Mercado fechado, pré-market e
after-market têm preços diferentes, e hoje a análise pode estar lendo o último fechamento como
se fosse "agora".

> Relacionado ao achado da revisão de 29/08: `_live_price` ganhou cache de 30s na task 006.
> O cache é bom para latência, mas precisa ficar claro **qual** preço está sendo mostrado
> (fechamento × pré × pós) e o rótulo tem de dizer.

## 4. Lista do scan — busca, dois modos de apresentação, timeframe destacado

- **Campo de busca** filtrando os resultados do scan.
- **Dois modos**: visão em **cards** e visão em **lista** (alternável).
- **Timeframe destacado no início** da linha — hoje ele fica colado no preço
  (`1d$513,530.15%`), sem respiro e sem hierarquia.

## 4b. Segunda rodada — pedidos do Samyr sobre a UI entregue (29/08, commit `2200a2e`)

- **Timeframe compacto na linha do scan.** O chip `1d` solto à esquerda resolveu o
  "colado no preço", mas ocupa demais. Usar o mesmo seletor compacto da barra de
  controle — `30m · 1h · 4h · D · S`, pills estreitas em sequência, com o ativo em
  destaque — em vez de um chip isolado por linha.
- **Grade de cards em múltiplas colunas.** A visão em Cards hoje empilha em coluna
  única. Deve acomodar mais de uma coluna, **até 3**, com quebra responsiva.

## 4c. Terceira rodada — o scan não pode apagar o resultado anterior (29/08) — ✅ FEITO (task 014, d558a26)

`runScan` (`app.js:4780-4797`) zera `$("scanList").innerHTML` **antes** do fetch, então a
lista fica vazia pelos ~8-12s da varredura. Pior: se o scan falhar, o `catch` só troca o
resumo — o resultado bom anterior já foi destruído e não volta.

Comportamento pedido: manter o scan anterior na tela até chegar a atualização, com um
indicador discreto de que está atualizando. Falha preserva o anterior e diz que falhou.

## 4d. Quarta rodada — "compra" quer dizer duas coisas na mesma tela (29/08) — ✅ FEITO (task 015, DA-075)

Levantado pelo Samyr olhando o ZEC-USD 4h: o card diz **COMPRA** / "Setup ativo agora"
com o preço em 835,20, enquanto a faixa verde desenhada diz **`compra 803,09`** e vai de
790,29 a 815,89 — o preço está claramente fora dela. Parece conflito, e a leitura
conflitante é culpa da tela.

São **dois setups independentes** que a tela nomeia igual:

1. **Região de compra na média** (`buy_zone`, `price_structure.py:1103-1109`) — recuo até
   uma média móvel ascendente, toca e reage. É a faixa em 803,09. O preço está fora.
2. **Padrão 1-2-3 de compra** — gatilho no rompimento da máxima do ponto 2 (834,82).
   Preço 835,20 rompeu por 0,05%. É daqui que vem o `COMPRA` do card.

O dado já sabe distinguir: o label da zona nasce como `"<MMS> — média onde reagiu em
<data>"` e o front tem a tag `"compra (recuo à média)"` — mas em `app.js:2380` o
qualificador só aparece no ramo `waiting`. Preço fora do recuo ⇒ sobra `compra` puro,
colado a um `COMPRA` que veio de outro setup.

Pedido: cada setup com nome próprio e inequívoco, sempre; o veredito do card declarando
de qual setup veio; e zona da média fora do preço declarando que não está ativa agora.

## 4e. Quinta rodada — o rótulo curto na barra de controle (29/08) — ✅ FEITO (task 016, `79e395e`)

A 012 encurtou o timeframe da LINHA DO SCAN por uma lista PARALELA (`SCAN_TF_CURTO`); a
barra lia de `ALL_TFS` e continuava com "Semanal | Diário | 4h | 1h | 15m". A lista
paralela morreu: `ALL_TFS` passou a carregar código · curto · completo e barra, gráfico e
scan derivam dela. Junto saiu a **caixa fantasma** dos grupos (`div.lb-tf` herdava o CSS
de `button.lb-tf`), e a largura devolvida foi pro nome do modelo (cortado em 120px → 181px
inteiro).

## 4f. Sexta rodada — TEMPO em DUAS fileiras, como o bloco MODELOS (29/08) — ✅ FEITO (task 017)

Pedido do Samyr sobre a barra da 016: os rótulos curtos ficaram bons, mas cinco pills em
fila ainda ocupam largura demais. TEMPO passa a usar a MESMA gramática do bloco MODELOS —
duas fileiras contando como **um** elemento da barra: em cima o macro (`S · D`), embaixo o
intradiário (`4h · 1h · 15m`).

O corte é **semântico**, não geométrico. O corte 3/2 (`S D 4h` / `1h 15m`) mede 102px
contra 115px do 2/3 — 13px a menos, e nenhum significado: separar macro de intradiário é o
que o olho já usa pra escolher o frame. A faixa é declarada **no próprio frame**, em
`ALL_TFS`, e não numa lista de "quem vai em cima" — frame novo entra na fileira certa
sozinho (foi lista paralela que causou a 016).

Medido em 1500px, com modelos longos reais:

- grupo TEMPO **185px → 115px**; campo ATIVO **147px → 178px** (a largura vai pro
  conteúdo: o ATIVO vivia esmagado abaixo da própria base de 160px)
- altura da barra **73px → 73px** — a pill perdeu o `line-height: var(--lh-12)` e mede
  26px, igual ao chip de modelo: 26 + 4 + 26 = os 56px que o par de MODELOS já ocupava
- largura mínima pra caber em uma linha: **1174px → 1104px**, e o limiar da container
  query desceu junto (1176 → 1106). Em **1440px** (o MacBook) a barra deixou de quebrar:
  **139px em duas fileiras → 73px em uma**, com o MODELOS saindo de pendurado sozinho na
  segunda fileira.
- em 1280px a barra continua quebrando (a coluna de conteúdo ali tem 962px, longe dos
  1106) e a primeira fileira ficou **17px mais alta** — é o TEMPO com a forma do MODELOS.
  É o preço da segunda fileira numa largura onde ela já quebrava antes.
- no telefone (390px) TEMPO **continua em fila única**: ali a barra já é uma coluna de
  blocos, empilhar só somaria altura sem devolver largura.

## 4g. Sétima rodada — "arruma essa bagunça": a tira do cabeçalho (29/08) — ✅ FEITO (task 018)

Print do ZEC-USD, palavras do Samyr. Saía assim, em fila única correndo até a borda:

`⬆️ gatilho 834,82  🛑 SL 764,76  🎯 TP 856,72  ⚖️ R:R 0,31  835,37 COTAÇÃO AGORA · 24H
· 29/08 20:42 ANÁLISE 834,74 em 2…`

São **duas famílias** que a tira misturava: níveis do PLANO (`#headTriggers`) e
COTAÇÕES/momentos (`#headPrice`). A forma escolhida, e o porquê de cada peça:

- **Duas linhas, uma família por linha.** Em cima MERCADO, embaixo PLANO — as duas
  ainda alinhadas à direita (contrato da task 010). A cotação vai em cima porque é a
  âncora contra a qual os níveis se leem, e porque o número grande **no meio da fila**
  era metade do motivo da tira ser ilegível.
- **Cada preço virou uma unidade fechada** (número · o que é · de quando). A tira
  quebra ENTRE unidades. A unidade só quebra por dentro quando não couber de jeito
  nenhum — e nunca transborda.
- **Rótulo ≠ carimbo.** "COTAÇÃO AGORA · 24H" é rótulo (caixa alta, apagado);
  "29/08 20:42" é carimbo (mono). Antes eram o mesmo texto colado, e não dava pra
  saber se o horário era da cotação ou da análise. Agora cada momento mora DENTRO da
  unidade do seu preço.
- **O "análise" solto virou régua** de 1px sólido (DA-070: régua, não degradê) + o
  rótulo da unidade. A palavra sozinha tinha o mesmo peso do resto e não separava nada.
- **A hora do candle parou de ser jogada fora.** O `as_of` da análise vem
  "2026-08-29 20:00" no intradiário e o `fmtDate` mostrava só "em 29/08" — justo o
  dado que distingue o momento da análise do da cotação (os dois caem no mesmo dia
  num frame de 4h). Mostrar o que já existe não é inventar; sem hora no dado, nada
  aparece.

Medido (ZEC-USD real, 4h):

- 1500 / 1280 / 1100px: tira de **43px**, nada fora da caixa, nada cortado
- card espremido pelo resizer da lateral (**394px**): empilha pra **83px** — a falta
  de espaço vira altura, nunca sumiço
- achado no caminho: com o rótulo mais longo que o backend produz de verdade ("último
  fechamento (pré-market sem negócio ainda)") a unidade travada em `nowrap`
  **transbordava** a borda no card de 390px. O `nowrap` da unidade caiu; o que
  continua atômico é número e carimbo.

## 4h. Oitava rodada — o modo LISTA vira TABELA (29/08) — ✅ FEITO (task 019)

"Faz colunas mais definidas e deixa cada informação em uma coluna pra ficar mais
organizado e permitir uma melhor comparação."

A causa: `.scan-line-row` era `display: flex; flex-wrap: wrap` e os níveis usavam
`margin-left: auto`. Cada linha se alinhava pelo **próprio conteúdo** — o gatilho do
MSFT, do LINK-USD e do ZEC-USD começavam em posições diferentes, e ainda sobrava um
rasgo vazio no meio da linha enquanto o texto do motivo truncava na borda.

Agora cada linha é uma **grade com o mesmo template** (nove colunas: tf · ativo ·
preço · dist · estado · gatilho · SL · TP/motivo · R:R), e o cabeçalho usa o template
idêntico. Decisões que a medição forçou:

- **`fr` em todas as colunas de dado**, não uma elástica só. A primeira tentativa pôs
  o TP como `1fr`: o número ficava 600px longe do SL e do próprio cabeçalho — o mesmo
  rasgo do `margin-left: auto`, só que dentro de uma célula.
- **Texto de motivo em forma curta + texto inteiro no `title`** (era a escolha a
  declarar): `🏁 no alvo`, `⚠️ sem alvo`, `invalidação 790,29`. Prosa numa coluna de
  tabela empurra tudo; e o texto não pode sumir, então vai pro title.
- **O qualificador "do preço atual" (task 012) virou `*` + legenda** embaixo da
  tabela. Apagá-lo seria pior: 0,21 medido do preço atual quer dizer outra coisa.
- **O rótulo por célula saiu da tela mas não do DOM** (`.scan-ck`, visualmente
  oculto): quem nomeia a coluna é o cabeçalho, mas numa `ul` sem `<th>` é esse rótulo
  que dá contexto a leitor de tela — e ele **volta a aparecer** quando a coluna
  estreita e a grade sai de cena.
- **Container query em 700px** (não media query — a lateral é redimensionável):
  abaixo disso as nove colunas não cabem, a linha volta a quebrar e o cabeçalho some
  junto (cabeçalho sem coluna embaixo mente).
- Alturas de linha iguais: o chip de estado ganhou `nowrap` e a coluna, piso de
  108px — chip quebrado em duas linhas desmancha a leitura em coluna tanto quanto o
  desalinhamento.

Modo CARDS **intocado** (escopo): os prints antes/depois de cards em 1500 e 1280 são
byte a byte idênticos.

## 4i. Nona rodada — "tá bem zoado pra celular" (29/08) — ✅ FEITO (task 020)

Print do Samyr no Chrome Android (ZEC-USD 4h). Cinco defeitos medidos em viewport de
telefone de verdade (390×844 e 360×800), mais um ponto de leitura:

1. **Tira do PLANO quebrando por acidente** — `[gatilho, SL, TP]` numa fileira e o
   **R:R sozinho** na outra, desalinhado. Agora é **grade 2×2 declarada**, encostada
   à esquerda (à direita cada fileira começava num lugar).
2. **Os dois preços viravam sopa.** No telefone cada preço fica numa linha e o
   **rótulo lidera** ("COTAÇÃO AGORA · 24H **835,37** 29/08 20:42" / "ANÁLISE
   **834,74** 29/08 20:00"). A régua da 018 sai: ela separava duas unidades lado a
   lado; empilhadas, quem separa é o rótulo.
3. **Legenda com 11 itens comia 4 linhas ANTES do gráfico** — no telefone ela desce
   pra **depois** do gráfico, inteira (esconder seria amputar). No desktop nada muda:
   os prints do card do gráfico em 1500 e 1280 são byte a byte idênticos.
4. **Rótulo de faixa cortado** — "recuo à média (MMS50) — não ativa agora 806,67" era
   mais largo que a área de plotagem. Escada: **texto inteiro → texto curto → só o
   preço**, medida contra a largura real do canvas. No telefone vira "recuo MMS50
   (inativa) 806,67"; no desktop, onde cabe, continua inteiro.
5. **Colisão com a régua direita** — consequência do item 4: o rótulo atravessava o
   eixo e ficava embaixo das pílulas de preço. Com a largura travada, não alcança
   mais o eixo.

**R:R < 1 virou ESTADO** (âmbar), não um número igual a outro qualquer: 0,31 arrisca
3,2x o que pretende ganhar. É a mesma gramática que o gráfico já usava dentro do
canvas (verde ≥ 1, âmbar < 1); agora vale no texto — que é o que se lê no celular
antes de rolar até o gráfico — e o `title` faz a conta.

**Achado NÃO corrigido** (fora dos cinco): no telefone os rótulos de preço dos pontos
1-2-3 desenhados sobre as velas se sobrepõem entre si quando os pontos ficam
próximos (visível como "①76,67" grudado em "752,54"). É colisão entre rótulos do
padrão, não com o eixo, e some no desktop. Fica anotado como próximo item.

## 5. Consequência da regra DA-070 no que já existe

- `.scan-frame-row` / `.scan-row`: trocar o gradiente de estado por borda-esquerda sólida.
- Chips de estado (EM GATILHO / EM MOVIMENTO / INVALIDOU / FORMANDO): manter a cor como
  informação, remover qualquer transição.

## 4j. Décima rodada — UM CARD POR ANÁLISE (29/08) — ✅ FEITO (task 021 + correção 024)

"São duas análises, uma do 123 e outra do veredito, e podem ficar em cards
específicos." E, na sequência: "cada análise deve ter seu resultado separado em cards
separados, com suas entradas, alvos, invalidação, SL etc".

O print mostrava a tira do PLANO no cabeçalho (gatilho · SL · TP · R:R) e, mais
abaixo, uma caixa solta sem vínculo visual nenhum com "Setup ativo agora · recuo à
média". Duas leituras independentes — que podem discordar, e ali discordavam —
amontoadas de um jeito que fazia uma parecer contradizer a outra. O R:R saía **três
vezes** na mesma tela.

Ficou registrado como **DA-077** (regra de leitura da tela). A divisão sai do DADO,
não de palpite:

- **1-2-3** → `pattern`, `invalidation`, `stop`, `target`, `risk_reward` (os cinco
  saem de `_pattern_levels(struct.pattern, …)` e são `None` juntos)
- **recuo à média** → `buy_zone` e o `pullback` que é recuo
- **de ninguém** → cotação, preço da análise e frame: o chão comum, fica FORA dos
  cards (cabeçalho em cima, frame no rodapé, uma vez só)
- **`setup_state` + `horizon`** → vão pro card que `setup_source` nomeia; sem dono
  (1-2-3 já acionado sem média ativa), caem no rodapé compartilhado

Decisões que a medição forçou:

- **Cada card leva o conjunto COMPLETO dos SEUS níveis.** A primeira leitura da minha
  instrução ("nenhum número duas vezes") produziria cards sem nível — o Samyr
  corrigiu. Nível que a leitura não calcula (stop do recuo à média) **some**: herdar
  o do outro card seria pior que omitir.
- **A nota do gráfico parou de listar nível.** Era a terceira cópia dos mesmos preços
  que o canvas já pinta. As BASES ("invalidação + folga de 0.5·ATR14") não sumiram:
  desceram pro card, coladas no número que justificam — que é onde ajudam a decidir.
- **Convergência é declarada nos dois lados.** Quando o alvo do 1-2-3 É a região de
  realização (`same_as_realize`), o mesmo número sai nos dois cards **dizendo que é o
  mesmo nível** — repetir sem avisar é que obrigaria o leitor a reconciliar.
- **Sem pictograma já nesta superfície** (DA-076): a leitura se identifica pela COR da
  borda (as mesmas do gráfico — azul/laranja do 1-2-3 por direção, verde do recuo) e
  pelo nome por extenso. Os outros ~300 emojis do front ficam pra task 025.
- **Os cards moram na coluna de contexto**, ao lado do gráfico no desktop e DEPOIS
  dele no telefone. No celular isso melhora o que a 020 tinha atacado: a tira do
  cabeçalho encolheu (só o mercado) e os níveis não disputam mais espaço antes do
  gráfico. A grade 2×2 do plano que a 020 declarou saiu junto — os níveis viraram
  linhas do card, e "R:R órfão numa segunda fileira" deixou de existir como estado.

**Anotado como PENDENTE (não regressão, dívida antiga):** o texto de invalidação que
o backend produz embute o próprio preço (`"o setup morre se perder 790,29 — …"`), então
na linha da invalidação o número aparece no valor E na frase que o explica. Cortar isso
é mexer no `price_structure._pattern_levels` e nas runs já persistidas, que carregam o
texto antigo. Fica pra uma rodada de backend.

## 6. Método NOVO — 1-2-3 Storm + Éden dos Traders (29/08) — ✅ FEITO (task 022)

Quarto setup do projeto, do Alexandre Wolwacz (Stormer). **Não é variação do 1-2-3**
que já existia: é outro padrão com a MESMA numeração significando coisas diferentes
(no de compra, o ponto 2 do nosso é o TOPO do repique e o do Storm é o FUNDO).
Registrado como **DA-081**.

O que entrou:

- **Detector próprio** sobre 3 CANDLES consecutivos (o outro continua sobre swings
  confirmados). Chip `Storm` na barra, rota estrutural ($0 de LLM), marcador próprio
  no resultado, chave de reúso própria.
- **Éden dos Traders como VETO** (MME 8 × MME 80 × posição do preço). Sem
  alinhamento a tela diz **NÃO OPERA** e o motivo; a **ARMADILHA** (preço acima da
  MME 8 e abaixo da MME 80) sai nomeada. Setup vetado **não ganha traço no gráfico**
  — o card mantém cada número.
- **Card próprio** no arranjo da 021/DA-077: com o Storm, o mesmo ativo mostra três
  leituras lado a lado (Storm · 1-2-3 · recuo à média), cada uma com os SEUS níveis.
- **MME 80 desenhada só no método Storm** (a coluna é calculada sempre, custo ~zero).

Decisões que a medição forçou:

- **Stop no ponto 2 EXATO, sem a folga de meio ATR do outro setup.** Medido na
  watchlist real: com a folga a mediana de R:R do Storm é 0,80 (21% ≥ 1); sem ela,
  1,13 (77% ≥ 1). Meio ATR14 é enorme perto da amplitude de TRÊS candles — carregar
  um número de um setup pro outro é o mesmo erro de carregar um nome.
- **Alvo ancorado no gatilho**, não no preço corrente: projetado do preço de agora
  ele fugiria junto com o preço e nunca seria atingido.

**Achado pro Samyr** (vale mais que a feature): o R:R mediano do Storm é ~6x o do
1-2-3 atual (1,13 × 0,19) — confirma que o R:R 0,13 do scan é problema ESTRUTURAL do
alvo. **Mas** o subconjunto que o Éden AUTORIZA tem R:R pior (0,71) que o Storm em
geral: o filtro melhora a probabilidade e piora a razão. As duas coisas não vêm
juntas.

**Aberto:** (a) pôr uma folga no stop do Storm, e qual; (b) o scan do Storm (task
023) deve listar só o que o Éden autoriza? Hoje ele veta 47 dos 60 pares (78%).

**Corrigido de passagem (era regressão minha):** com o quinto chip a fileira de
métodos estourava os 390px do telefone — ela passa a QUEBRAR (falta de largura vira
altura, nunca informação saindo da tela).

## 6b. Storm — segunda leitura (entrada no ponto 3) e Storm no SCAN — ✅ FEITO (task 023)

Regra dura do Samyr, textual: **"mas sem desfazer o Setup123"**. Cumprida com
contra-prova byte-a-byte (abaixo).

**Duas entradas do MESMO padrão.** A spec escreve "rompimento da máxima do ponto 2
(ou 3)". A 022 colapsava as duas na mais conservadora; agora o padrão não tem mais
um gatilho — cada LEITURA tem o seu, com o seu alvo (a amplitude lançada dele) e o
seu R:R (medido dele até o MESMO stop). Invalidação e stop são comuns: é o mesmo
padrão. O card mostra as duas, a ANTECIPADA primeiro (a que o preço alcança antes),
com o rótulo dizendo qual é qual. Gatilhos que a tela mostra iguais viram UMA leitura
declarada — repetir o mesmo número com dois nomes é a duplicata que a DA-077 proíbe.

**Storm no scan, em coluna própria.** Setup diferente com stop, alvo e R:R de outra
regra: célula própria (estado · entrada p2/p3 · R:R, com as duas leituras no title),
nunca somado às células do 1-2-3. O modo cards ganhou uma sub-linha com a etiqueta
STORM. A grade da 019 continua alinhada — dez colunas em vez de nove, e o ponto de
quebra da container query subiu de 700 para 800px (aritmético: soma dos pisos + gaps
+ padding).

**Track record separado por setup.** `scans.jsonl` carimba `setup` (`123`/`storm`) e,
no Storm, qual `entrada`. O painel devolve o agregado E a decomposição por setup, cada
uma com a sua base — misturar dois métodos num número só não descreve nenhum (task
008). Linha antiga sem carimbo é lida como `123`: o ledger é append-only. A CHAVE do
fechamento NÃO mudou de forma — pôr o setup nela desamarraria todos os fechamentos já
gravados.

**Tempo do scan — medido, e ficou MAIS RÁPIDO com o Storm dentro:**

- sem Storm, antes de tudo: **9,2s** frio · **6,5s** quente
- com Storm, sem otimizar: 10,4s frio · 8,9s quente (era degradação real)
- com Storm + cache curto de série preparada: **7,0s** frio · **3,0s** quente

O que pagou a conta não foi paralelismo novo: `_prep` (série + seis médias em pandas)
já era chamado DUAS vezes por ativo/frame numa única linha do scan — `build_actionable_plan`
prepara e `detect_price_structure`, logo abaixo, prepara de novo. Com o Storm seriam
três. Um cache de processo com TTL de 60s (maior que uma varredura inteira, menor que
o intervalo entre elas) resolve as três. Isolando só o custo do Storm com o mesmo
cache: 6,9s → 7,0s, ou seja, ele entrou de graça.

**Contra-prova do Setup123:** 60 registros (20 ativos × 3 frames), plano completo +
sha do gráfico, computados com o código ANTES e DEPOIS na MESMA janela de dado —
arquivos idênticos byte a byte (`sha256 978b032d…`).

**R:R das TRÊS leituras** (watchlist real, 20 ativos × 1d/4h/1h):

- 1-2-3 atual ...... mediana **0,19** · 5% com R:R ≥ 1 · nenhum ≥ 2
- Storm ponto 2 .... mediana **1,14** · 77% ≥ 1 · 2 pares ≥ 2
- Storm ponto 3 .... mediana **1,44** · 70% ≥ 1 · **16 pares ≥ 2**

A entrada no ponto 3 é a antecipada em 45 dos 60 pares. Ela tem a melhor mediana e
oito vezes mais setups com R:R ≥ 2 — é a aritmética prevista: mesmo stop, gatilho mais
perto, mesma amplitude de alvo. O custo é entrar antes da confirmação, e isso o
número não mede: quem mede é o track record, que agora separa os dois setups.

## 4k. Correção da 021 — cards com os NÍVEIS COMPLETOS, e a fileira que escala (task 024)

"Cada análise deve ter seu resultado separado em cards separados, com suas entradas,
alvos, invalidação, SL etc." A redação da 021 ("nenhum número aparece duas vezes"),
lida ao pé da letra, produzia o oposto: cards sem níveis.

**A distinção que vale, e já está no ar desde a 021/022/023:** proibido é o MESMO
número da MESMA análise em dois lugares; obrigatório é cada análise com os SEUS
níveis. Não é duplicação — são análises diferentes com números diferentes por
construção. Hoje cada card leva o conjunto completo dele:

- **1-2-3 Storm** — Éden (as duas médias) · stop = invalidação (ponto 2) · e, por
  ENTRADA (ponto 2 e ponto 3): gatilho · alvo por projeção da amplitude · R:R
- **Padrão 1-2-3** — gatilho · invalidação · stop (ponto 3 + folga de ATR) · alvo
  (swing anterior) · R:R
- **Recuo à média** — entrada na faixa da média · distância do preço · região de
  realização

Nível que a leitura não calcula SOME (o recuo não tem stop nem invalidação, e não
os herda do vizinho). Valor recusado vira o MOTIVO.

**O que a 024 mudou de fato: o LAYOUT.** Empilhados na coluna de 340px, os três cards
somavam ~870px de rolagem — a parede que o pedido proíbe. Eles saíram da coluna
estreita e viraram uma **fileira própria, largura inteira, abaixo do gráfico**, com
`repeat(auto-fit, minmax(300px, 1fr))`: **3 lado a lado em 1500px, 3 em 1280, 2 em
1100, 1 no telefone** — sem media query, porque a largura útil muda com a lateral
arrastada. Lado a lado eles ficam COMPARÁVEIS de relance, que é o ponto de mostrar
as três: mesmos rótulos na mesma altura, números diferentes.

O rodapé compartilhado (o frame) atravessa a fileira inteira, e parou de contar
leituras: um card pode carregar mais de uma dentro (o Storm tem duas entradas), então
contar cards diria um número que não é o de leituras.

**Contra-prova do setup123** (restrição do Samyr): 60 registros com plano completo +
sha do gráfico, computados com o código ANTES e DEPOIS na MESMA janela de dado —
idênticos byte a byte (`sha256 6dd21b63…`).

**Anotado, não corrigido:** no telefone o gráfico do Storm ficou denso — dez níveis
rotulados (os do 1-2-3, os do recuo e os cinco do Storm) numa área de plotagem de
390px. Nada é cortado e nada colide com a régua (o contrato da 020 continua de pé),
mas é muita etiqueta sobre a vela. A saída provável é desenhar só a leitura que o
Éden autoriza no telefone — decisão de produto, não de layout, então fica pro Samyr.

## 7. Sem emoji na webui (DA-076) — ✅ FEITO (task 025)

"Tira todos os emojis." A linha de corte já estava decidida na DA-076: sai todo
PICTOGRAMA; fica o símbolo TIPOGRÁFICO com função de interface (↻, →, ↑ ↓ ↔, ✕) —
controle e direção, não decoração.

**Levantamento por FAIXA UNICODE** (não lista à mão, que erra por omissão):

- antes desta rodada: **217** ocorrências nos três arquivos do front (app.js 172,
  index.html 26, style.css 19). O censo original do mainbot contava 333; as tasks
  021–024 já tinham derrubado ~116 ao reescrever cabeçalho, cards e barra.
- depois: **0** — nos três arquivos do front E nos módulos Python da webui, que
  também mandam texto pra tela (rótulo de etapa, mensagem de erro, seção de
  consistência do relatório). Tirar só do front deixaria metade do mosaico de pé.
- o que sobrou, e por quê: `→` (89) e `↻` (19) e `↑ ↓ ↔ ↗ ⇒ ✕ ✓ ▸ ▾` — controle e
  direção. `▸ ▾` são os marcadores de abrir/fechar dos `<details>`, entram por
  `content` de CSS.

**Onde o pictograma era o ÚNICO marcador, entrou COR + PALAVRA:**

- estado do setup (🎯/⏳/⚪) → a classe `.sc-state.ativo|aguardar_*|sem_*` já pintava
- direção do 1-2-3 (🟢/🔴) → a palavra "de compra"/"de venda" mais a cor da borda do
  card, a MESMA que o gráfico usa pra marcar o padrão
- passou/falhou (✅/❌) → `.mt-row.ok`/`.err`, `.cfg-status.ok`/`.err`, `.h-flag.done`/
  `.error` — todas já existiam
- atenção (⚠️) → o âmbar que já acompanhava o texto desde a 020
- severidade da checagem (🔴/🟡/🟢) → **classe nova** `.sev-alta|media|baixa`, porque
  ali a bolinha era mesmo o único sinal — e bolinha não se lê em leitor de tela
- etapas do confronto (○/⏳/✅/♻) → o ícone saiu inteiro; a cor da classe `is-*` e a
  palavra ("aguardando/rodando…/concluída/reusada do cache") já estavam lá

**Dois defeitos que o corte cego teria deixado passar,** e que só apareceram porque a
suíte roda o navegador: `dot` continuava referenciado em dois lugares depois que o
par `[label, dot]` virou `[label]` — e um `ReferenceError` no meio do `paintScan`
deixava a lista do scan VAZIA. É exatamente a armadilha anunciada: apagar o símbolo
sem olhar quem o lia.

**Prova:** `tests/test_webui_sem_emoji_e2e.py` — dois portões (estático por faixa
Unicode nos três arquivos; e o que o NAVEGADOR escreve, que pega o `content` de CSS
que o fonte esconde) mais quatro testes de que o estado continua distinguível por cor
e palavra.

## 8. Fronteira entre ESCREVER e PUBLICAR o front (30/08) — ✅ FEITO (task 20260830-001)

O front em desenvolvimento vazava pra tela do Samyr em tempo real: `_serve_static` lia
o diretório do REPO a cada requisição e o cache-buster por mtime impedia o navegador
de segurar a versão anterior. O instante em que um agente salvava `app.js` era o
instante em que ele via aquilo — sem commit, sem teste, sem deploy. Ele viu o Storm
meio-escrito colado ao Padrão e reportou como defeito de design; era obra em
andamento. Registrado como **DA-082**.

- O servidor lê de um diretório **PUBLICADO**, fora do repo.
- Publicar copia da **revisão COMMITADA** (`git archive HEAD`), não do working tree —
  a fronteira sem exceção: pra mudar a tela é preciso COMMITAR.
- Publicação **atômica**; falha deixa o publicado anterior intacto.
- **Deploy em dois caminhos:** o restart do serviço publica o HEAD, e
  `python -m tradingagents.webui.static_publish` publica sem restart (front-only).
- **Cache-buster passou a ser a REVISÃO, não o mtime** — `git archive` carimba os
  arquivos com a data do COMMIT, então duas publicações no mesmo segundo saíam com o
  mesmo `?v=`. Esse defeito foi encontrado PELO TESTE, não por leitura.
- **Modo ao vivo** (`TRADINGDEGENS_STATIC_LIVE=1`) pra desenvolver — explícito,
  desligado por padrão e barulhento no log.

## 9. Lista de observação: o veredito cobria o ativo (30/08) — ✅ FEITO (task 20260830-002)

"Além do veredito ficar cobrindo o nome e as informações do ativo."

A causa era a grade da linha: `minmax(0, 1fr) auto auto`. A faixa `auto` do veredito,
com texto `nowrap`, toma o tamanho do CONTEÚDO antes de a faixa `fr` receber o que
sobra — então "Aguardar rompimento" saía inteiro e o que sumia era o ticker (`AMI…`),
o nome (`A…`) e o preço. Invertido: **o ativo é a chave da linha.**

- **Duas faixas `fr` com mínimo 0** (1,5 pro ativo, 1 pro veredito): as duas encolhem,
  e quem trunca é o VEREDITO — com a frase completa no `title`.
- **O preço passou a ocupar a fileira inteira.** Na coluna 1 de uma lateral de 200px
  ele tinha 74px e "$465.58 ↓ 2.33%" pede 100 — saía cortado junto com o ticker. O
  marcador "pronto/erro", que é transitório, desceu pra uma fileira própria em vez de
  disputar espaço com o dado permanente.
- Medido do MÍNIMO do resizer (200px) ao teto (560px): **ativo cortado em ZERO
  larguras**; antes eram 4 itens cortados em 200px e 2 em 280px (o padrão).

Os emojis da lateral já tinham saído na task 025 (DA-076) — o `⏳`/`🎯` do estado do
setup viraram cor + palavra, e o teste desta rodada trava isso no canto onde cada
pictograma custava mais caro.

**Consequência da DA-082 que apareceu aqui e vale registrar:** com o servidor lendo o
front PUBLICADO, os e2e passariam a medir a versão commitada em vez do working tree —
um teste de CSS validaria a versão anterior e passaria por acidente. A suíte ganhou um
fixture autouse que aponta o `_STATIC_DIR` pro repo. É o inverso do que produção quer,
e é o certo aqui.

## 10. Linguagem visual da Quantfury (DA-078) aplicada (30/08) — ✅ FEITO (task 20260830-003)

"Modela bem a Quantfury, pq nosso design não está seguindo as regras que mandei." A
DA-070 e a DA-076 tinham sido cumpridas ao pé da letra e o resultado ainda não parecia
com a referência — regra vaga não segura implementação. A DA-078 destilou a referência
em regras verificáveis; aqui elas viraram código e portão de teste.

- **Geometria (regra 1):** 22 pílulas (`border-radius: 999px`) → **zero**. Raio máximo
  **2px** e o token único do projeto desceu de 5px pra 2px; os avulsos de 8/6/5/4/3px
  foram junto.
- **Cor (regra 3):** `--amber`, `--yellow`, `--yellow-dim`, `--yellow-border` e
  `--orange` **saíram da paleta** — não só de uso, da declaração. Verde = ganho/alta,
  vermelho = perda/baixa, o resto branco e cinza.
- **Seletor de segmento é TEXTO (regra 9):** timeframe do gráfico, timeframe da barra,
  método, modo de apresentação do scan e filtros de estado perderam caixa, borda e
  fundo. O ativo se distingue por **cor (verde) e peso**, como no print da referência.
- **Botão com caixa só pra AÇÃO (regra 10):** Analisar, Escanear, Comparar, Retomar
  mantêm superfície sólida — e há teste que não deixa isso se perder junto.
- **Chip de estado do scan** virou palavra colorida sem moldura: a linha já carrega a
  banda de cor do estado, então a caixa era moldura em cima de moldura (regra 8).

**O invariante, que é o que dá trabalho de verdade:** onde a cor era o único portador
de estado, entrou PALAVRA. O caso mais delicado era o R:R < 1 — o âmbar era tudo o que
distinguia 0,31 de 2,50. Agora a linha diz `risco > retorno (3.2x)` por escrito, com a
conta feita. "Aguardar rompimento", "NÃO OPERA", "sem alvo", "Divergem" e a severidade
da checagem já eram palavras; perderam a cor e continuaram dizendo.

**Achado de passagem, e vale pra todo dia:** a suíte tinha testes que carimbavam
`2026-08-29` como "hoje" na cotação. Eles passaram o dia inteiro e **quebraram sozinhos
à meia-noite** — nove falhas que pareciam do redesenho e eram do relógio. O carimbo
passou a acompanhar `timeutil.today()`.

**Numeração:** havia COLISÃO de DA — o mainbot registrou a linguagem visual como DA-078
e a execução como DA-080 no mesmo intervalo em que eu registrei o Storm (078) e a
fronteira de publicação (080). As minhas foram renumeradas pra **DA-081** e **DA-082**,
com nota no lugar antigo; as do Samyr ficaram onde estavam.

## 11. Regra 11 — o espaço vago fica onde sobra (30/08) — ✅ FEITO (task 20260830-004)

"Espaços mal aproveitados" — quarto print do mesmo defeito no mesmo dia (barra com
MODELOS cortado, linha do scan com rasgo, tira do cabeçalho estourando, e agora o
cabeçalho do resultado).

**Reagrupado:** a meta ("Data · Tipo · Método · Custo · Tempo · Concluído") ficava numa
linha e a cotação encostada à DIREITA noutra — o vão era a tela inteira menos os dois
conteúdos. Viraram uma fileira só (`.result-info`): o conteúdo flui e a sobra fica no
FIM; sem largura, quebra e a linha de baixo começa à ESQUERDA.

**Mantidos, com o motivo escrito no CSS** (auditoria ocorrência por ocorrência no topo
do arquivo): `.topbar`, `.result-head`, `.progress-head`, `.chart-head`, `.config-head`,
`.scan-head`, `.cmp-col-head`, `.sub-row-connected`, `.resume-bar`, `.lbp-foot` —
todos TÍTULO × AÇÃO, o caso canônico de dois grupos independentes; `.audit-steps-list`
e `.sc-head` — rótulo × valor, com quebra antes de truncar; e os `::after` dos
`<details>`, que são marcador de ponta e não têm segundo grupo.

**O que segura daqui pra frente não é o comentário** (lição do bump de cache, DA-073):
`tests/test_webui_espaco_regra11_e2e.py` varre o DOM RENDERIZADO em 1500/1280/390 e
reprova qualquer fileira flex que tenha, ao mesmo tempo, vão central > 60px E
descendente truncado. Trinta e uma fileiras examinadas por largura.

**Honestidade sobre o alcance:** a varredura não encontrou nenhuma ocorrência do par
(vão + corte) nem ANTES da mudança — as tasks 016/018/019/021 já tinham matado o lado
"corte" de cada uma das quatro superfícies. O que restava do print era o VÃO sozinho no
cabeçalho, e é ele que foi reagrupado. O teste fica como guarda de regressão, não como
caçador de bug — e está dito aqui pra não parecer que ele achou mais do que achou.

## 12. Nomes dos métodos: Setup123 e Storm123 (30/08) — ✅ FEITO (task 20260830-005)

"Coloca Setup123 e Storm123 pra eu identificar." Os dois métodos SÃO um 1-2-3 — a
diferença está em QUAL —, e "1-2-3" × "Storm" obrigava a lembrar de qual era qual.

O rótulo mudou em toda superfície: chip da barra, veredito do cabeçalho, linha de
método do resultado, título dos cards de análise, coluna e sub-linha do scan, título do
painel de scan e o title do botão que o abre.

**O identificador NÃO mudou.** `setup123` e `storm123` continuam sendo o que viaja na
API, no store, no `scans.jsonl` e nos registros já gravados — trocar isso quebraria
histórico, reúso e track record, e "sem desfazer o Setup123" é regra. A tradução mora
num lugar só (`methodLabel`), então não há rótulo escrito à mão espalhado.

**Varredura pedida:** nenhum lugar compara método por texto de tela (`=== "1-2-3"` e
afins) — se comparasse, esta renomeação teria mudado comportamento em silêncio. O
único `===` sobre rótulo que existe é entre dois rótulos vindos do MESMO backend (o
passo ativo do progresso), que é outra coisa.

Provado em `tests/test_webui_nome_de_tela_vs_identificador.py`: run gravada no formato
antigo continua sendo classificada como Setup123, linha antiga do ledger continua
contando, o reúso segue keyado pelo método, e o rótulo sai do VALOR.

---

## 13. A cor tem de concordar com o número, e o frame que não decidiu é exploratório (task 20260830-006)

Complemento da 20260829-020, a partir de quatro prints do MESMO ativo em 1h / D / 4h —
mesma ação, preço 218,40, análise de 28/08, veredito no 4h:

    1h  → alvo 219,35 · invalidação 210,53 · SL 207,00 · recuo MMS20 211,27
    D   → alvo 227,50 · invalidação 181,00 · SL 175,09 · recuo MMS20 208,95 · R:R 0,21
    4h  → alvo 219,35 · invalidação 181,32 · SL 176,83 · recuo MMS50 178,38

**O stop vai de 207,00 a 175,09.** São três trades diferentes na mesma tela.

**1. Frame que não produziu o veredito se declara EXPLORATÓRIO.** Tarja acima dos cards
(`estes níveis são recalculados no 1h e NÃO são o plano da decisão — o veredito desta
análise é no 4h`), carimbo `1h · exploratório` DENTRO do gráfico e cards em borda
tracejada. O carimbo "veredito no 4h" já existia, mas no topo da página — ele sai da
tela justamente quando se rola até o gráfico, que é onde a decisão se toma. **Nada de
informação some:** os níveis do frame exploratório continuam inteiros.

**2. A cor segue o NÚMERO.** `R:R < 1` nunca em verde (branco, com a conta escrita:
`R:R 0,21:1 · risco 4,8x o retorno`), e o chip ganhou fundo OPACO — com 0,85 de alfa a
faixa verde do alvo atravessava por baixo e o tingia. A faixa `não ativa agora` saiu do
verde de "pode ir" e passou a cinza: tracejado e opacidade eram acabamento DENTRO da
mesma cor, e a cor é o que se lê primeiro.

**3. R:R em todo frame, ou ausente COM o motivo.** `_risk_reward` devolvia `None` mudo
quando não havia alvo estrutural à frente da entrada — a linha sumia do card e o frame
ficava indistinguível de um sem setup. Agora vem `rr=None` com o motivo escrito (o
critério da task 006 original, que só valia para dois dos quatro casos), o risco que
existe continua medido, e a linha do R:R **nunca desaparece**.

**4. O carimbo da análise diz o que é:** `ANÁLISE 218,40 · ÚLTIMO CANDLE 4H · 28/08 17:30`.
A hora mudava por frame (é o último candle de cada um) e, sem rótulo, parecia dado
inconsistente.

**Dois defeitos nascidos DESTA task, corrigidos aqui:** o carimbo cresceu e colidiu com
a dica de zoom (a dica cede: quebra em duas linhas na metade direita); e os carimbos
eram desenhados ANTES das velas, que passavam por cima deles — agora são os últimos.
O texto do chip também degrada por medida no telefone em vez de atravessar a régua.

Provado em `tests/test_webui_frame_e_cor_e2e.py` (17 testes, 1500px e 390×844; o teste
de cor mede o CANAL, não um hex). Governança: **DA-085**.

---

## 14. Troca de frame atômica — o chip nunca aponta um frame com os níveis de outro (task 20260830-007, P0)

Três prints do MESMO ativo no mesmo minuto: chip **D** selecionado, carimbo do gráfico
**4h**, stop **497,59** — quando o stop do diário é **526,92**. Trinta pontos num nível
que se opera, exibidos como se fossem daquele frame.

**A causa:** `switchTimeframe` movia `_tf` e repintava o seletor NO CLIQUE; gráfico e
cards só trocavam ao fim do `await`. **A correção:** o realce fica no frame DESENHADO
e o clicado vira PENDENTE (sublinhado pontilhado + motivo no `title`), até os níveis
chegarem. O clique não se perde e a tela nunca afirma o que não mostra.

**Três da mesma família, corrigidos junto:**

- **Resposta superada não pinta** — selo por pedido (`_tfSeq`). Clicar D e logo 1h fazia
  a resposta do D chegar depois e pintar o diário por cima do 1h: incoerência PERMANENTE.
- **A cotação não é do frame** — `/api/chart` não devolve `live_price`, e passar
  `undefined` apagava "último fechamento 465,58" da tira ao trocar de timeframe.
- **O Storm sumia ao trocar de frame** — `/api/chart` montava o `actionable` SEM
  `storm`, então numa run do método Storm a leitura inteira (card do veto do Éden, as
  duas entradas, as linhas do gráfico) desaparecia. Era o que fazia os prints A e B, os
  DOIS do 4h, discordarem: um veio do render da run, o outro da rota.

**Sobre "dois stops empilhados":** não era estado misto — são as duas leituras do MESMO
plano do 4h (o Éden autoriza ali e veta no diário, por isso somem no print C). A
observação estava certa e levou ao defeito real acima, que era outro.

Provado em `tests/test_webui_troca_de_frame_atomica_e2e.py` (7 testes; a janela de
transição é REPRODUZIDA embrulhando `window.fetch`, e medida três vezes dentro dela).
Cada teste foi verificado contra o código antigo: 6 dos 7 falham lá. Governança: **DA-086**.

---

## 15. R:R sempre abaixo de 1 — não é o método, é o percurso (task 20260830-008)

Depois que o padrão aciona, a entrada de referência vira o PREÇO ATUAL (é o que resta
de trade) e o stop continua no ponto 3 — então **o R:R desaba conforme o trade
amadurece**. No print: stop 526,92 · alvo 460,21 · preço 465,58 → **0,09**. No gatilho
(517,35) o mesmo setup oferecia **5,97**, e o percurso já andou **91%**.

A tela mostrava só o 0,09. Quem lê conclui "o método dá trade ruim", quando o que
houve foi chegar tarde — conclusões opostas sobre o mesmo dado.

**Agora, com o padrão acionado:**

- `risco/retorno agora` **e** `no gatilho`, um debaixo do outro, com peso diferente —
  o de cima é o que se opera, os de baixo explicam por que ele está baixo;
- `percurso do setup: andou 91% · sobra 9%`, com o motivo escrito;
- o estado do card distingue de relance: `acionado · andou 91% do caminho`;
- o chip do gráfico explica pelo percurso em vez de repetir que o número é baixo.

**Padrão NÃO acionado continua com UMA linha:** ali a entrada É o gatilho, e um
segundo número seria o mesmo preço duas vezes (DA-077).

**A régua é medida, não faixa arbitrária:** `andado_pct` é a fração do caminho
gatilho→alvo. Pode passar de 100 (alvo batido) e ficar negativa (preço voltou atrás do
gatilho). É ela que ordena o scan dentro de `em_movimento` — quem tem mais movimento
pela frente vem antes —, então nenhum limiar novo foi inventado.

Vale igual pro Storm (mesmo decaimento). Provado em `tests/test_rr_percurso.py` (10) e
`tests/test_webui_rr_percurso_e2e.py` (8). Governança: **DA-087**.

---

## 16. Um gráfico, um método (task 20260830-009)

"Percebo tbm que mistura tudo em um gráfico só, Storm123, Setup123 e Padrão com
Erick." Eram três misturas empilhadas:

1. **médias** — as duas famílias sempre desenhadas: MMS 20/50/200 + EMA 8/21/50, mais
   a EMA 80 do Éden. Sete linhas onde o método usa três;
2. **níveis** — numa run do Storm, os do Storm E os do plano, daí os dois stops a 0,39
   um do outro sem dono;
3. **pontos numerados** — os círculos 1-2-3 vinham do detector de swings mesmo na run
   do Storm, cujo 1-2-3 é outro padrão. Mesma numeração, pontos diferentes.

**Agora:** o gráfico desenha a leitura do método aberto — níveis, médias, pontos e o
carimbo de R:R. As outras continuam inteiras nos cards; no gráfico entram por um
seletor de CAMADAS que só aparece quando há o que oferecer. Com duas famílias na tela,
todo rótulo se identifica (`Setup123 · stop (SL)` × `Storm123 · stop (SL)`); com uma
só, o rótulo fica limpo.

Por método: padrão e Setup123 → MMS 20/50/200 · Erick → EMA 8/21/50 · Storm123 →
EMA 8/80 (o par do Éden). A camada extra não vaza pra outra análise.

Provado em `tests/test_webui_um_grafico_um_metodo_e2e.py` (13). Governança: **DA-088**.

---

## 17. As camadas são do usuário (task 20260830-010)

"Eu deveria poder selecionar a camada do que eu quero ver" + "no time frame que eu
quiser". Revisa a 009 num ponto: lá a camada extra era zerada a cada análise; agora
ela **persiste na sessão**.

- **Abre** na camada do método — ninguém configura nada pra ver o próprio resultado.
- **Depois do primeiro toque** a escolha é dele e vale nas análises seguintes, em
  qualquer timeframe (`sessionStorage`, morre ao fechar a aba).
- **Dois grupos:** LEITURAS (Setup123 × Storm123, só as que existem no plano) e
  MÉDIAS (MMS do Padrão × EMA do Erick). A EMA 80 acompanha o Storm — é metade do
  filtro Éden.
- **As duas leituras juntas** é o valor (comparação), e nada fica anônimo: faixa,
  ponto numerado, legenda, chip de R:R e a etiqueta CURTA (a que o telefone desenha).
- **Dois chãos:** a preferência nunca deixa o gráfico VAZIO ao abrir uma análise, e a
  liberdade de frame não apaga qual é o frame do veredito (DA-085).

Provado em `tests/test_webui_camadas_e2e.py` (13). Governança: **DA-089**.

---

## 18. Card de execução + índice de confiabilidade (task 20260830-012)

O print do CRWD mostra nove faixas de três famílias e **nenhuma frase dizendo o que
FAZER com elas**. Os níveis já eram derivados; faltava a política.

O card responde, nesta ordem: **veredito** (entrar agora / aguardar recuo até <nível> /
passar, com a razão escrita) · **as ordens na sequência de digitar**, cada uma com a
base do seu preço · **invalidação** · **realizar** (grosso em T1, resíduo até T2) ·
**peso** (sempre relativo) · **proteção** (BE e trailing, desligados, com o porquê) ·
**confiabilidade** por setup.

- A entrada é sempre a LIMITE; no "aguardar", a ordem vai pra faixa de recuo.
- PASSAR **não imprime onde comprar** — o número é o que fica na cabeça de quem lê.
- A fração de cada alvo sai `a calibrar`: o corpus tem um único caso com número.
- BE e trailing desligados não é omissão — o método compra o recuo à média, e ligá-los
  ejetaria no pullback em que se adiciona. `sem evidência` de BE no corpus do Erick.
- **Gate de N:** com n<5 a tela DIZ "amostra insuficiente" em vez de exibir taxa; de 5
  a 19 a taxa sai sempre com o intervalo de Wilson; a expectativa (E[R]) vem antes.

Backend em `tradingagents/webui/execucao.py` + `/api/execucao`. Provado em
`tests/test_execucao.py` (22) e `tests/test_webui_exec_card_e2e.py` (11).
Governança: **DA-090**.

# Até quantos ativos cabem, e onde está o gargalo

**Data:** 2026-09-01 · **Task:** 20260831-042 · **Pergunta do Samyr:** *"até quantos ativos a gente
pode usar pra fazer essa atualização sem pegar [bloqueio] na API do provedor"*.

> **RESTRIÇÃO RESPEITADA:** o único IP é o de **produção**. Nada aqui foi obtido empurrando a fonte
> até o bloqueio — os números vêm de **consumo observado** (instrumentando o ponto onde a rede
> acontece) mais **limites documentados**. Onde a evidência é fraca, está dito.

## 1. Consumo REAL por ativo, medido

Instrumentei `yfinance.download` e `requests.get` — o ponto onde a chamada de rede de fato sai — e
rodei passadas com **cache frio** (diretório novo), que é o pior caso:

| cenário | chamadas de rede **por ativo** |
|---|---|
| **ação**, 3 frames (1d, 4h, 1h) | **2,0** |
| **ação**, 4 frames (+ semanal) | **2,0** |
| **cripto**, 4 frames | **3,0** (1 yfinance diário + 2 à exchange) |

**O SEMANAL É DE GRAÇA, e isto é medição, não estimativa:** 2,0 → 2,0. Ele é **reamostrado da mesma
série diária** que o frame `1d` já carrega (`load_ohlcv` → `_resample_weekly`), e `load_ohlcv` guarda
por símbolo em disco. O custo marginal é uma leitura de disco e a reamostragem: a passada de 5 ativos
foi de **6,5s para 6,4s**.

Com o cache **quente** (o dia já baixado) uma passada inteira custa **zero** chamadas de rede — o
diário só volta à fonte quando fecha um candle novo, e desde a DA-119 esse refresh é **127 bytes**
(era 118.713).

## 2. O gargalo NÃO é a varredura — é o poller de preço

A conta que importa é por **hora**, e aí a ordem de grandeza se inverte:

| consumidor | frequência | chamadas/ativo/hora |
|---|---|---|
| varredura estrutural | 1 por candle de 1h | **2 a 3** |
| **cotação ao vivo** (3ª linha da watchlist) | poller de 40s, cache de 45s no servidor | **~80** |

**O poller consome ~27× mais que a varredura.** Ele só roda com a aba em primeiro plano e só para os
ativos **visíveis** na lista — mas é ele que dita o teto, não o scan.

E é exatamente por isso que a **vigilância de nível** (DA-138) foi pendurada nele: ela compara a
cotação que o `/api/prices` **acabou de buscar** contra níveis já calculados. **Acréscimo: 0
chamadas/ativo/hora.**

## 3. Os limites, com o grau de confiança declarado

| provedor | limite | confiança |
|---|---|---|
| **Binance** (cripto, intradiário) | **1.200 weight/min por IP**; `/api/v3/klines` custa 1–2 de weight conforme o `limit` | **ALTA** — publicado na documentação oficial, com o weight por endpoint |
| **yfinance** (ações, e o diário de tudo) | **não existe limite oficial publicado** | **BAIXA** — o que há são relatos de `429` em uso intenso e o próprio `yf_retry` no nosso código, que existe porque o 429 acontece. **Qualquer número específico aqui seria boato**, e eu não vou apresentar boato como fato. |

## 4. A curva: quantos ativos cabem

**Cripto (Binance, 24/7)** — o único lado com teto conhecido:

* varredura: 2 chamadas de exchange/ativo/hora ⇒ desprezível contra 1.200 **por minuto**;
* poller: ~80/hora/ativo ⇒ **~1,3 chamadas/minuto por ativo**;
* a 1.200 weight/min e weight 2, o teto teórico é ~600 chamadas/min ⇒ **centenas de ativos**.

⇒ **Do lado cripto, a watchlist não é o que vai bater no limite.** O gargalo é outro (§5).

**Ação (yfinance, só pregão)** — sem teto publicado, então a recomendação é por **prudência**, não
por cálculo:

* varredura: 2/ativo/hora;
* poller: ~80/ativo/hora **enquanto a aba está aberta e o ativo visível**.

Com 60 ativos, os visíveis (uns 10–15 numa lateral) dão **800–1.200 chamadas/hora**, e a varredura
soma 120. É a mesma ordem de grandeza dos relatos de throttle — ou seja, **é aqui que se chega
perto**, e sem número oficial a única postura honesta é a margem larga.

## 5. Margem segura recomendada (não o teto)

**60 ativos na watchlist, com estas três condições:**

1. **manter o poller preso ao que está visível** (já é assim) — o custo cresce com a tela, não com a
   lista;
2. **não reduzir o TTL de 45s do preço** — dividi-lo por dois dobra o consumo dominante;
3. **cadência estrutural de 60 min** — como o Samyr decidiu. Voltar a 15 min multiplicaria a
   varredura por 4 **e** exigiria o frame de 15m para não devolver o mesmo padrão quatro vezes.

O que **não** recomendo sem medir de novo: passar de 60 ativos **junto com** abrir a lista inteira na
tela. Não é a lista que pesa; é quantos ficam visíveis.

## 6. O que ACONTECE hoje ao bater no limite — e este é um achado

`yf_retry` (`stockstats_utils.py:51`) trata `YFRateLimitError` com **3 tentativas e espera
exponencial: 2s, 4s, 8s**, e então **propaga**. Quem chama no scan (`_frame_row`) captura e devolve
`estado: "sem_dado"` com o motivo escrito.

**Então degrada COM aviso — não trava calado, e não inventa candle.** Bom.

**O achado:** cada chamada limitada **bloqueia o worker por até 14 segundos** antes de desistir. Com
`_SCAN_WORKERS = 4`, um limite generalizado transforma uma passada de ~7s em minutos, com quatro
threads dormindo. O comportamento é honesto, mas o **custo do fracasso é alto** — e o `sleep` é
síncrono dentro do worker.

**Recomendação (não implementada nesta task, precisa do seu aval):** um disjuntor por fonte — depois
de N `429` seguidos numa janela, a passada **para de tentar aquela fonte** e marca os frames
restantes como `sem_dado` na hora, em vez de cada um pagar seus 14s. Degradar rápido é melhor que
degradar devagar quando o resultado é o mesmo.

## 7. O que já funciona e não precisa refazer

**Ação para fora do pregão: já é assim.** `alvos_da_passada` usa o `marketState` da cotação (cripto
sempre; ação só com sessão ativa, incluindo pré e after) — não um calendário nosso. Confirmado no
código, não presumido.

## 8. Resumo em uma linha

O semanal saiu de graça; **o gargalo é o poller de preço, não a varredura**; do lado cripto há folga
de ordens de grandeza; do lado ação **não há limite publicado**, então a recomendação é **60 ativos
com o poller preso ao visível** — e o item a corrigir é o custo de 14s por chamada limitada, que
transforma um throttle pequeno numa passada longa.

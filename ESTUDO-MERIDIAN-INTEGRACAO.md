# Sinais pro MT5 via MERIDIAN — estudo de integração (não mais ponte nova)

**Data:** 2026-09-01 · **Task:** 20260901-038 (redireciona a 20260901-036) · **Contexto do
Samyr:** existe o projeto `samyrwendel/meridian` (privado, TypeScript, rodando local no
`samyr-srv`), com a integração MT5 demo JÁ CONSTRUÍDA.

> **Isto é um ESTUDO, não uma entrega de execução.** Cloneei o repositório privado
> (acesso via `gh`, já autorizado neste servidor) só para LER o código — nenhuma
> credencial de corretora foi pedida, nada foi instalado no `samyr-srv`, nenhuma ordem
> foi enviada, e o clone (`/tmp/meridian-study`) é temporário, fora deste repositório.
> Tudo abaixo foi verificado no CÓDIGO, não só no README — cada afirmação cita o
> arquivo. Este documento **substitui** a recomendação de arquitetura do
> `ESTUDO-MT5-SINAIS.md` (mantido por histórico, com um aviso no topo apontando pra
> cá): não há mais "qual caminho construir" — o caminho existe, o trabalho é integrar.
>
> **Atualizado pela task 20260901-039** (§9-§13): o Samyr precisou o requisito —
> Setup123 e Storm123 têm de ser ligáveis/desligáveis de forma independente, na
> mesma conta demo, e isso levanta magic number por estratégia, política de
> conflito e tamanho de posição comparável. Reclonei o repo (mesmo processo, só
> leitura) pra verificar mais fundo antes de responder.
>
> **Atualizado pela task 20260901-040** (§14-§16): DRY RUN vira o modo PADRÃO
> da escada de execução, antes de DEMO. `scripts/mt5_dry_run.py` (novo) já
> roda de verdade contra o scan real e REGISTRA o payload completo + o
> veredito de validação — rodei contra sinais reais desta watchlist, ver §15.

---

## ⚠️ O risco continua o mesmo, e continua em destaque

O track record atual mede **expectativa negativa** nos dois setups do TradingDegens:

| setup | n fechados | acerto | R:R médio | expectativa |
|---|---|---|---|---|
| Setup123 | 19 | 78,9% | 0,18 | **−0,068R** |
| Storm123 | 29 | 24,1% | 2,04 | **−0,348R** |

Isso não muda com o Meridian pronto. Ligar os sinais à execução — mesmo em demo — é
**esperado perder**, pela mesma razão de sempre. O ponto de uma conta demo é
descobrir isso com dinheiro de mentira, e o Meridian até MEDE performance de verdade
(snapshot do agente, nunca mockado) — o que torna esta integração uma FERRAMENTA DE
MEDIÇÃO legítima, não uma promessa de lucro.

---

## Resumo executivo

- **O Meridian NÃO é uma fila aberta.** `POST /api/accounts/{id}/orders` só aceita
  `strategyId` de uma lista FIXA de 5 entradas no código
  (`EXECUTABLE_STRATEGIES`, `app/api/accounts/_execution.ts`). Não existe hoje um
  `"tradingdegens-*"` nessa lista — **integrar exige uma alteração no código do
  Meridian** (uma linha no dicionário), não é plug-and-play por fora.
- **O enfileiramento exige a identidade de DONO do Meridian**, não um token de
  serviço — hoje só um humano logado no painel do Meridian consegue enfileirar uma
  ordem. **Isso é uma proteção, não uma lacuna**: o caminho mais simples e mais
  seguro é o Samyr clicando um botão, não um pipeline 100% automático — o que já
  entrega o "opt-in explícito" que o pedido exige, de graça.
- **Cobertura de símbolos, agora específica do XM** (a corretora que o código do
  Meridian usa — não mais "qualquer corretora" como no estudo anterior): confirmado
  por pesquisa que o XM oferece as mega-caps americanas ("Turbo Stocks": Apple,
  Microsoft, Nvidia, Google, Tesla, Amazon) e BTC + LINK entre as criptos — os dois
  ficam mais fortes que na estimativa genérica da task 036. ZEC continua sem
  evidência de estar na lista do XM.
- **Magic number é HARDCODED em 260811 pra QUALQUER estratégia** hoje
  (`connectors/xm_mt5_bridge.py:661`) — dar um magic PRÓPRIO ao TradingDegens exige
  mexer no agente Python, não só cadastrar a estratégia no servidor.
- **SL/TP no Meridian são em PONTOS, não preço.** O TradingDegens calcula preços
  absolutos (SL 95,00 / TP 110,00); o Meridian espera `stopLossPoints`/
  `takeProfitPoints` (distância do preço de entrada, em pontos MT5). Isso muda o
  que o TradingDegens precisa mandar.
- **Não achei bypass do bloqueio de conta real/concurso** — procurei
  especificamente por flag de debug ou caminho alternativo e não existe: o
  servidor (`executionGate`, linha 50-66 de `_execution.ts`) e o agente
  (`xm_mt5_bridge.py:599-600`) checam de forma INDEPENDENTE, exatamente como o
  README descreve. Esta é a única coisa que eu ativamente tentei quebrar e não
  quebrei.

---

## 1. O que o Meridian já tem — confirmado no código

| peça do README | onde está, no código | confirmado? |
|---|---|---|
| Agente MT5 instalado, dado nunca mockado | `connectors/xm_mt5_bridge.py` — lê `mt5.account_info()`/`mt5.positions_get()` direto do terminal | ✅ |
| Fila isolada e autenticada | `broker_order_commands` (D1) + `POST/GET /api/accounts/{id}/orders` e `/commands` | ✅, mas fechada por whitelist (ver §2) |
| Trava em duas camadas (servidor + agente) | `executionGate()` em `_execution.ts:50` (servidor) + `if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO` em `xm_mt5_bridge.py:599` (agente) | ✅, e são checks INDEPENDENTES — o agente não confia no servidor pra isso |
| Pyr-Cycle ATR enfileira perna de grid em demo | `app/api/accounts/[id]/orders/route.ts:65-118` (validação específica) + `_pyr-auto.ts` | ✅ |
| Magic number 260811 | `xm_mt5_bridge.py:661` | ✅, mas **hardcoded pra toda ordem**, não só o Pyr-Cycle (ver §4) |

---

## 2. O contrato da fila de ordens

**Duas rotas, dois tipos de autenticação — isto é o achado mais importante pra
arquitetura:**

| rota | quem chama | autenticação | o que faz |
|---|---|---|---|
| `POST /api/accounts/{id}/orders` | o NAVEGADOR do dono, dentro do painel Meridian | identidade de DONO (`requestOwnerId`, header `oai-authenticated-user-id` do proxy de auth do próprio deploy — não é um token de serviço) | valida e INSERE a linha em `broker_order_commands` com `status='queued'` |
| `GET/POST /api/accounts/{id}/commands` | o agente Python no `samyr-srv` | `Authorization: Bearer <MERIDIAN_AGENT_TOKEN>` (o token da conexão, hash comparado no servidor) | `GET` pega o próximo comando `queued`; `POST` reporta o resultado |

**Não existe hoje uma terceira rota de "token de serviço externo enfileira
direto".** Procurei (`service token`, `api_key`, `bearer` em `_lib.ts`/
`_execution.ts`) e não há nada — quem enfileira é sempre um DONO logado no
navegador. Pra o TradingDegens enfileirar sozinho (sem o Samyr clicar em nada),
o Meridian precisaria de uma rota NOVA com esse tipo de credencial — que é
exatamente o tipo de mudança que o pedido pede pra eu PROPOR, não fazer.

**Campos aceitos** (`POST /orders`, validados em `route.ts:48-64`):

```
strategyId        — precisa estar em EXECUTABLE_STRATEGIES (hoje: 5 fixos)
symbol             — 3-24 chars, [A-Z0-9._#-], maiúsculo
side               — "buy" | "sell"
volume             — número, mín. 0.01, máx. min(0.1, connection.max_volume)
stopLossPoints     — inteiro > 0, em PONTOS (não preço!)
takeProfitPoints   — inteiro > 0, em PONTOS (não preço!)
```

**Ordem avulsa ou só as pernas do Pyr-Cycle?** AMBAS são possíveis pelo código,
mas de jeitos diferentes: se `strategyId === "pyr-cycle-atr"`, a rota reexecuta
uma validação pesada contra o sinal interno vivo do laboratório (linha 65-118) e
IGNORA os SL/TP que vieram no corpo, recalculando-os do sinal. **Pra qualquer
outro `strategyId` da whitelist, esse bloco inteiro é pulado** — a ordem segue
com exatamente os campos que vieram no `POST`, sem revalidação de sinal. Ou
seja: um `"tradingdegens-123"` cadastrado usaria o caminho SIMPLES (ordem avulsa,
confiando nos SL/TP enviados) — o que é bom (menos acoplamento) e um risco (o
Meridian não confere se o sinal do TradingDegens ainda é válido no instante da
execução; essa responsabilidade fica inteira do lado do TradingDegens, ou de
quem aperta o botão).

**PONTOS, não preço.** No agente (`xm_mt5_bridge.py:650-669`), `stop_points`/
`take_points` são multiplicados pelo `point` do símbolo e SOMADOS/SUBTRAÍDOS do
preço de EXECUÇÃO (`tick.ask`/`tick.bid` no instante do envio) — não do gatilho
que o TradingDegens calculou. O TradingDegens teria que mandar
`abs(preço_do_nível − gatilho) / point_do_símbolo`, e aceitar que o SL/TP real
fica ancorado no preço de execução (que pode já ter andado desde o gatilho),
não no preço do plano.

---

## 3. Os símbolos batem? (agora específico do XM)

Diferente do estudo anterior (que não sabia a corretora), aqui o código APONTA
o XM (`README`, "XM / MetaTrader 5 multi-account connector"; o arquivo se chama
`xm_mt5_bridge.py`). A resolução de símbolo em si é **broker-agnóstica e
dinâmica** — `_resolve_symbol()` (`xm_mt5_bridge.py:68-79`) consulta
`mt5.symbols_get()` do terminal REALMENTE conectado, em tempo de execução, com
correspondência exata e depois por prefixo. Ela funcionaria com qualquer
corretora cujo terminal esteja logado ali — mas a pergunta prática é "o que a
conta XM do Samyr tem", porque é o terminal que ele já roda.

**Pesquisa (WebSearch, 01/09/2026), específica do XM:**

| ticker | confiança | por quê |
|---|---|---|
| AAPL, MSFT, NVDA, GOOGL, AMD | **alta** (subiu) | o material do XM cita "Turbo Stocks" nomeando Apple, Microsoft, Nvidia, Google, Tesla, Amazon como exemplos diretos — mais forte que a estimativa genérica anterior |
| INTC, IBM, AVGO | **média** | mega-caps mas não citadas nominalmente nas fontes encontradas; XM anuncia "mais de mil instrumentos" incluindo ações, então plausível |
| CRWD, TSM, MRVL, SNDK, MP, AAOI, BE, EOSE | **baixa** | mid/small-cap, sem menção — corretoras de CFD priorizam os nomes mais líquidos |
| SPCX | **desconhecida** | mesmo problema do estudo anterior — não identifiquei o ticker com segurança |
| BTC-USD | **alta** | confirmado nominalmente |
| LINK-USD | **alta** (subiu de "média") | Chainlink aparece NOMEADO na lista de criptos do XM encontrada na pesquisa |
| ZEC-USD | **baixa** | Zcash não apareceu em nenhuma lista de cripto do XM nas fontes consultadas |

**Ainda é estimativa por pesquisa, não uma leitura da conta real** — a única
forma de confirmar por completo é abrir a Observação de Mercado no terminal MT5
já conectado no `samyr-srv` (10 minutos, e não precisa de mim: é só olhar a
lista que já está aberta). Dado o quanto XM é um corretor multi-ativo grande
(nem de longe um corretor só-forex), a expectativa razoável é de cobertura BOA
pras mega-caps e cripto principal, fraca pro resto — coerente com a tabela.

---

## 4. Magic number — o achado que o README não deixava claro

O README diz "Magic Number `260811` identificando as ordens do Meridian" como
se fosse plural/geral. **No código, é mais estreito**: `xm_mt5_bridge.py:661`
carimba `"magic": 260811` em TODA ordem que o agente executa, não importa o
`strategyId` — Volatility Guard, Crypto Move, Pyr-Cycle ATR ou um futuro
TradingDegens sairiam todos com o MESMO magic number.

O que JÁ diferencia por estratégia é o `comment`:
`f"Meridian:{strategy_id or 'demo'}"` (linha 662) — e é isso que a rota de
posições do Pyr-Cycle usa pra filtrar (`orders/route.ts:110`,
`position.comment.startsWith("Meridian:pyr-cycle-atr")`). **Ou seja: hoje o
Meridian já sabe separar "de qual estratégia é esta ordem" pelo COMMENT, não
pelo magic** — um `"Meridian:tradingdegens-123"` já nasceria distinguível sem
tocar em nada além de registrar o `strategyId`.

**Se o Samyr quiser um MAGIC NUMBER próprio de verdade** (útil pra ver de
relance no histórico do MT5, que mostra magic mas não sempre o comment
inteiro), o agente precisa de uma pequena mudança: um mapa
`strategy_id -> magic_number` em vez do `260811` fixo. **Corrigido na §10**: um
magic só pro TradingDegens não bastava — Setup123 e Storm123 precisam de
magics DISTINTOS entre si também (requisito da task 20260901-039).

---

## 5. Onde roda cada parte, e como o sinal atravessa

```
SERVIDOR (Debian, TradingDegens)              samyr-srv (Windows)
─────────────────────────────────             ────────────────────────────────
scanner.scan_watchlist() já roda,              MT5 terminal (conta demo XM,
produz os em_gatilho hoje                       já logada — infraestrutura
                                                 do Samyr, não mexo)
                                                Meridian (Next.js) rodando
                                                 local, com o D1/fila
                                                Agente Python do Meridian
                                                 (connectors/xm_mt5_bridge.py),
                                                 já rodando, já fazendo poll
                                                 da fila a cada 15s

Caminho recomendado (opt-in por clique, zero mudança no agente):
1. Painel do TradingDegens mostra o em_gatilho (já mostra).
2. Samyr, OLHANDO o sinal, decide mandar — clica um botão NOVO no
   TradingDegens ("enviar pro Meridian").
3. O clique abre (ou chama) o painel do Meridian, JÁ LOGADO como dono,
   com strategyId/symbol/side/volume/stopLossPoints/takeProfitPoints
   pré-preenchidos a partir do sinal — Samyr CONFIRMA lá.
4. O POST /api/accounts/{id}/orders é o EXISTENTE, sem mudança nenhuma
   no servidor Meridian além de cadastrar o strategyId.
5. O agente (já rodando, sem mudança) executa como executa hoje.
```

Isso é DELIBERADAMENTE o caminho de MENOR mudança: nenhum código novo no
agente Python, nenhuma rota nova autenticada por token de serviço, nenhum
segredo novo pro TradingDegens guardar. O preço é que não é "hands-off" — é
opt-in por sinal, um clique de cada vez, que é exatamente o que o pedido exige
por padrão de qualquer jeito.

**Caminho alternativo (mais automação, mais mudança, mais risco):** o Meridian
ganha uma rota de serviço autenticada por token (não por sessão de dono) que o
TradingDegens chama sozinho quando um `em_gatilho` aparece — sem clique. Isso
exige: (a) código novo no Meridian (rota + verificação de token, e decidir se
ela pula a autenticação de dono ou reusa algo equivalente), (b) um segredo novo
compartilhado entre os dois servidores, (c) decidir se o TradingDegens filtra
por confiabilidade antes de mandar (ex.: só `n≥20` operável) pra não automatizar
o envio de um sinal com n=2. **Não recomendo começar por aqui** — é mais
superfície de erro pra resolver um problema (clicar um botão) que não é caro.

---

## 6. O que muda no TradingDegens

Nada de arquitetura pesada — é código de apresentação + uma conversão:

1. **Um botão no card de execução** (ou na faixa) pro `em_gatilho`: "enviar pro
   Meridian" — só aparece quando há um `em_gatilho` de verdade, e nunca dispara
   sozinho.
2. **Conversão de preço pra pontos**: `stopLossPoints = round(abs(preço_nível −
   gatilho) / point_do_símbolo)` — o `point` do símbolo não é um dado que o
   TradingDegens tem hoje (ele opera em preço, nunca em pontos MT5); precisaria
   vir de uma tabela pequena por símbolo (ex.: ações costumam ter `point=0.01`,
   mas confirmar por instrumento é mais seguro que assumir) ou ser perguntado
   ao Meridian/terminal.
3. **Nenhuma mudança no ledger nem no motor de scan** — o `em_gatilho` já
   carrega tudo que esse botão precisa (`trigger`, `sl`, `tp`, `direction`,
   `ticker`).

---

## 7. Recomendação de arquitetura

**Caminho do §5 (opt-in por clique, zero mudança no agente Python), com UMA
mudança no servidor Meridian**: cadastrar `"tradingdegens-123"` e
`"tradingdegens-storm"` (ou um `"tradingdegens"` só, com o método no comment)
em `EXECUTABLE_STRATEGIES`. Custo de manutenção: **baixo** — é a MESMA
plataforma que já roda, já testada em produção há tempo suficiente pra ter um
laboratório inteiro (Pyr-Cycle) em cima dela; o TradingDegens só aprende a
falar o formato dela (pontos, não preço) e não guarda credencial nenhuma de
corretora — a sessão de dono do Meridian é do Meridian, o agente é do Meridian.

**Não recomendo agora** a rota de serviço automática (fim do §5) — maior
superfície, maior custo de manutenção, e resolve um problema (um clique) que
não pede solução.

---

## 8. Decisões do Samyr

1. **Aprovar a mudança no Meridian** (adicionar o(s) `strategyId` do
   TradingDegens em `EXECUTABLE_STRATEGIES` — um repo dele, uma linha).
2. **Conferir a Observação de Mercado do terminal XM já aberto** — 10 minutos,
   substitui a estimativa da seção 3 por número real.
3. **Magic number por estratégia** (seção 4, corrigido na §10) — faixa
   `270101`/`270102` proposta, dele decidir.
4. **Qual caminho de envio**: o botão manual do §5 (recomendado) ou a rota
   automática (não recomendada agora, mas a decisão é dele se quiser mesmo
   assim).
5. **Quando ligar de verdade** — nada disto liga sozinho; o botão só existe
   quando ele pedir pra eu construir.

**Mais decisões, da task 20260901-039 (ver §9-§13):**

6. **Confirmar o `marginMode` da conta demo** (§11) — já vem no snapshot do
   agente (`connectors/xm_mt5_bridge.py:452-455`), só falta olhar; decide qual
   das duas políticas de conflito vale.
7. **Aprovar os DOIS `strategyId` e os DOIS magics** (Setup123 e Storm123,
   §9/§10) — mesma mudança de uma linha da decisão 1, só que duas linhas.

**Mais decisões, da task 20260901-040 (ver §14-§16):**

8. **Quando construir o gate dry_run/demo de verdade** (§14.2/14.3) — hoje é
   proposta; vira código quando o Samyr aprovar as decisões 1/6/7 acima e
   houver uma primeira estratégia pra realmente alternar de modo.
9. **Aprovar (ou não) a mudança no Meridian pro dry run com `order_check()`**
   (§15.1) — a única forma de "aceitaria: true/false" de verdade, em vez de
   `null`; é opcional, o dry run já presta contas sem ela, só com validação
   parcial declarada.

---

## 9. Ativação independente por estratégia — já existe, verificado no código

O requisito ("cada estratégia liga/desliga por si, a mesma conta") **já é como o
Meridian funciona hoje**, de propósito — não é o TradingDegens que precisa
construir isso, é o Meridian que já entrega:

- `broker_strategy_activations` (`db/schema.ts:63`, `drizzle/meta/0005_snapshot.json:430`)
  tem **CHAVE PRIMÁRIA COMPOSTA `(connection_id, strategy_id)`** — cada
  estratégia tem a SUA PRÓPRIA linha, estruturalmente independente das outras.
  Ligar `tradingdegens-storm123` não toca a linha de `tradingdegens-setup123`
  nem a de `pyr-cycle-atr`, pela própria forma da tabela.
- `enabled` tem **`default: false`** — qualquer estratégia nova nasce
  DESLIGADA. Não é uma convenção que eu preciso lembrar de seguir; é o
  schema que garante.
- `mode` tem **`default: 'confirm_each_order'`**, e a rota que liga a
  estratégia (`PATCH /api/accounts/{id}/strategies/{strategyId}`,
  `[id]/strategies/[strategyId]/route.ts:58`) CALCULA o modo assim:
  `strategyId === PYR_STRATEGY_ID ? PYR_AUTO_MODE : "confirm_each_order"` —
  **só o Pyr-Cycle pode entrar em modo automático; qualquer outro
  `strategyId`, incluindo os dois do TradingDegens, é FORÇADO a
  `confirm_each_order`** pelo próprio servidor, mesmo que alguém tente ligar
  diferente. Isso é o "opt-in explícito" do pedido, garantido pelo código do
  Meridian, não por uma promessa do TradingDegens.
- Ligar (`active: true`) só é aceito se `gate.demo && gate.online`
  (linha 54) — a trava de conta demo do §1 continua valendo ANTES de
  qualquer estratégia poder ser ativada, não só antes de cada ordem.

**Nada a construir aqui.** Registrar `tradingdegens-setup123` e
`tradingdegens-storm123` em `EXECUTABLE_STRATEGIES` (§2) já basta pra cada um
ganhar seu próprio toggle, seu próprio estado persistido, seu próprio "desligado
por padrão" — de graça, pela mesma tabela que o Pyr-Cycle usa.

---

## 10. Magic number por estratégia — a correção que a task 039 pediu

**O que eu tinha proposto (§4) estava incompleto**, e o achado no código
explica por quê: `xm_mt5_bridge.py:661` carimba `"magic": 260811` **fixo, pra
qualquer `strategy_id`** — não é um magic "do Meridian" que o TradingDegens
herdaria junto, é literalmente o número do Pyr-Cycle, hardcoded. Sem mudar
isso, Setup123 e Storm123 sairiam os DOIS com 260811 — nem entre eles, nem
contra o Pyr-Cycle, dava pra separar pelo magic sozinho (o `comment` ainda
separaria, mas o pedido é explícito: quer o magic também).

**A mudança necessária** (pequena, no agente Python): trocar o `260811` fixo
por uma tabela `strategy_id -> magic`, mantendo `260811` como o valor pra
`"pyr-cycle-atr"` (compatibilidade — não pode mudar o magic de ordens/posições
que já existem) e adicionando uma entrada por estratégia nova.

**Faixa proposta, deliberadamente longe de `260811`** (pra nunca confundir os
dois de relance no histórico do MT5 — `260811` vs `260812` diferem em um
dígito e convidam erro de leitura):

| strategy_id | magic |
|---|---|
| `pyr-cycle-atr` (Meridian, existente) | `260811` (não mexe) |
| `tradingdegens-setup123` | **`270101`** |
| `tradingdegens-storm123` | **`270102`** |
| *(reservado pra métodos futuros do TradingDegens)* | `270103`–`270110` |

Documentado aqui como a fonte da verdade da faixa — se o Samyr aprovar, é
este o número que entra no agente.

---

## 11. Conflito entre estratégias na mesma conta — política declarada

**O caso é real e vai acontecer**, como o Samyr mediu: no dia 01/09, AAOI
tinha Storm123 de VENDA e Setup123 de COMPRA no mesmo diário — dois sinais
vivos, direções opostas, mesmo símbolo.

### 11.1. O primeiro gate não é hedge — é a fila de UM SÓ POR VEZ

Achado que nem o README nem o estudo anterior cobriam: a fila de ordens só
aceita **UM comando pendente por CONEXÃO (conta), não por estratégia**:

```sql
-- orders/route.ts:119 e _pyr-auto.ts:67-70, o MESMO padrão nos dois
WHERE connection_id = ? AND status IN ('queued', 'processing')
```

Se Setup123 acabou de enfileirar uma ordem e ela ainda está `queued`/
`processing` (o agente faz polling a cada 15s — `PUSH_INTERVAL`), uma
tentativa de Storm123 enfileirar NA MESMA HORA recebe `order_already_pending`
(409) — não importa que sejam símbolos ou direções diferentes. Isso já
elimina a corrida de "as duas chegam exatamente juntas": elas nunca chegam
literalmente juntas na fila, sempre em sequência. O que falta declarar é o
que acontece DEPOIS que a fila libera — se o MT5 aceita a segunda ordem.

### 11.2. Isso depende do `marginMode` da conta — já reportado, falta olhar

O agente JÁ ENVIA o modo de margem da conta no snapshot
(`xm_mt5_bridge.py:452-455`):

```python
"marginMode": _enum_label(account.margin_mode, [
    ("ACCOUNT_MARGIN_MODE_RETAIL_NETTING", "retail_netting"),
    ("ACCOUNT_MARGIN_MODE_EXCHANGE", "exchange"),
    ("ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", "retail_hedging"),
])
```

**Eu NÃO tenho acesso à conta demo do Samyr pra ler esse campo** — só o
próprio agente, já rodando no `samyr-srv`, sabe o valor de verdade. É por
isso que o pedido pede "verificado na conta demo antes de assumir": não dá
pra eu confirmar de aqui, e não vou fingir que confirmei. O Samyr olha isso
no snapshot da conexão no painel do Meridian (ou em Ferramentas > Opções, na
aba Conta, do próprio MT5) — um minuto, não precisa de mim.

**A política, pros dois casos possíveis** (o Meridian já reporta qual é —
não preciso adivinhar, só ramificar em cima do valor real):

- **`retail_hedging`** (comum em corretoras internacionais tipo XM, mas
  NÃO confirmado pra esta conta): **hedge PERMITIDO.** As duas posições
  convivem — MT5 hedging suporta long e short simultâneos no mesmo símbolo
  como posições distintas, e com magics diferentes (§10) cada uma continua
  separável no histórico e no cálculo de resultado. É a comparação MAIS
  JUSTA: nenhuma estratégia perde a entrada por causa da outra ter chegado
  primeiro.
- **`retail_netting`** (comum em contas reguladas por certas jurisdições,
  ex. EUA): hedge é **IMPOSSÍVEL platôrmicamente** — a segunda ordem em
  direção oposta NETEIA contra a primeira (reduz ou inverte a posição em vez
  de abrir uma segunda). Aqui a política é **BLOQUEAR o segundo sinal**
  enquanto o primeiro estiver aberto, com o motivo declarado na tela
  ("Storm123 sinalizou venda em AAOI, mas Setup123 já tem uma posição de
  compra aberta na mesma conta em modo netting — ordem bloqueada pra não
  misturar os dois resultados"). **Este não é um comportamento novo que eu
  estou inventando**: é o MESMO padrão que o Pyr-Cycle já usa consigo mesmo
  — `xm_mt5_bridge.py:275-277`, `elif opposite_side: status = "hold"; reason
  = f"Direcao {side} aguarda a saida de {len} perna(s) oposta(s); hedge
  bloqueado"`. O Meridian já tem a convenção; o TradingDegens só a repete
  pro par Setup123×Storm123.

**Por que não "o conflito impede as duas" (a terceira opção que o pedido
listava):** bloquear os DOIS sinais jogaria fora até o que já estava
funcionando (ex.: Setup123 já executando havia dias) só porque Storm123
mudou de ideia depois — pior que as outras duas opções nos dois regimes de
conta.

---

## 12. Tamanho de posição equivalente entre estratégias

Mesma régua do paper trading interno (DA-154/155): banca fixa (US\$100 por
padrão, configurável) por OPERAÇÃO, nunca um lote fixo — `lote = banca /
(preço_de_entrada × unidade_do_lote)`, computado **para cada sinal,
independentemente**, usando o preço de entrada DAQUELE sinal.

Isso já é como o `scripts/mt5_sinais_dry_run.py` (task 036) calcula — a
mesma fórmula vale pros dois `strategyId` sem mudança nenhuma; o que muda é
só QUAL preço de entrada entra na conta (o gatilho de cada método, que já é
diferente por natureza — Setup123 e Storm123 raramente compartilham o mesmo
gatilho pro mesmo símbolo). Sem isso, a estratégia com o gatilho mais caro
(menos unidades por US\$100) pareceria "menor" só pela aritmética, não pelo
método — e a comparação deixaria de ser justa.

---

## 13. Como o track record da demo separa por estratégia

A pergunta que fecha o requisito: se as duas rodarem juntas, dá pra saber
DEPOIS quem ganhou o quê? Sim, por DUAS chaves independentes que já
convivem em cada posição/ordem, sem precisar de nada novo:

1. **O `comment`** (`"Meridian:tradingdegens-setup123"` /
   `"Meridian:tradingdegens-storm123"`) — já é como o Pyr-Cycle se filtra
   hoje (`orders/route.ts:110`), funciona em QUALQUER modo de margem.
2. **O `magic`** (`270101`/`270102`, §10) — mais rápido de ler direto no
   histórico do MT5 sem abrir o comentário de cada posição.

O TradingDegens lê o histórico de NEGÓCIOS FECHADOS do snapshot do agente —
`recentDeals`/`historySummary`, já enviados a cada 15s, construídos por
`_history()` (`xm_mt5_bridge.py:357-386`) a partir de `mt5.history_deals_get`
(90 dias por padrão), com `magic`/`comment` por negócio (o mesmo par que as
posições abertas também carregam) — e agrupa por essas duas chaves pra produzir o
MESMO tipo de resumo que o paper trading interno já produz (PnL, acerto,
curva de equity) — só que agora com execução real de demo por trás, em vez
do ledger idealizado. Isso é trabalho de APRESENTAÇÃO no TradingDegens
(ler o snapshot, separar por magic, mostrar ao lado do paper interno pra
comparação) — não exige NENHUMA mudança adicional no Meridian além do que
já está proposto (§2, §9, §10).

**Um limite a declarar**: `recentDeals` só traz os **últimos 100 negócios
dos últimos 90 dias** (`_history(days=90)`, `recent[:100]`) — pra uma
comparação que dure mais que isso, o TradingDegens precisa GRAVAR o que já
leu (o mesmo padrão do `scans.jsonl` append-only que o paper interno já
usa), não confiar em reconsultar o snapshot pra sempre.

---

## 14. DRY RUN — o modo padrão da escada (task 20260901-040)

**A escada tem três degraus, e só dois existem de verdade:**

1. **DRY RUN (padrão)** — o sinal percorre o caminho INTEIRO até o formato de
   ordem do Meridian (símbolo, lado, lote, SL/TP, magic, comment) e é
   REGISTRADO, nunca enviado.
2. **DEMO** — a ordem É enviada, pela fila autenticada do §2, com as travas
   do §1 rejeitando conta real/concurso.
3. **REAL** — não existe. As travas do Meridian (servidor + agente,
   independentes, §1) já bloqueiam — não há nada a construir nem a "não
   construir", é ausência estrutural.

### 14.1. Por que dry run primeiro, e por que ele PRESTA CONTAS

Um dry run que só imprime "enviaria X" na tela e some não vale nada — o
valor está em ter um REGISTRO que alguém pode olhar depois e comparar com o
que a demo realmente fez. Por isso o formato do registro é o MESMO shape do
payload que iria pro Meridian (§2) MAIS o veredito de validação, nunca só um
dos dois.

### 14.2. Onde o estado (dry_run/demo) mora — proposta, não implementada

Hoje NÃO existe nenhuma ponte TradingDegens↔Meridian rodando (§038/039 são
estudo, nada foi ligado) — então "o estado persistido" ainda não tem onde
morar de verdade. A proposta, seguindo o MESMO molde que o paper trading
interno já usa (`PaperWalletStore`, DA-155 — um arquivo pequeno, uma chave
por unidade, sem duplicar o que já é fonte de verdade em outro lugar):

```
~/.tradingagents/logs/webui/mt5_modo.json
{
  "tradingdegens-setup123": {"modo": "dry_run", "atualizado_em": "..."},
  "tradingdegens-storm123": {"modo": "dry_run", "atualizado_em": "..."}
}
```

Padrão `dry_run` pra QUALQUER estratégia ausente do arquivo (o mesmo
princípio do `enabled: false` por padrão que o Meridian já usa em
`broker_strategy_activations`, §9) — nunca o oposto. Passar uma estratégia
pra `"demo"` é uma ação EXPLÍCITA (um endpoint/botão "ativar demo pra
Setup123"), nunca uma consequência de outra coisa. **Isto é código pra
construir quando o Samyr aprovar a integração (§8) — não construí agora
porque não há, ainda, nenhuma chamada real ao Meridian pra este estado
guardar.**

### 14.3. O gate, quando existir

```
sinal em_gatilho aparece
  → modo da estratégia (arquivo acima) == "dry_run"?
      SIM → scripts/mt5_dry_run.py (ou o mesmo código, chamado da webui):
            registra payload + validação, PARA aqui. Nunca chama o Meridian.
      NÃO (== "demo") → tudo que o dry run faria, MAIS o POST real pro
            Meridian (§038 §5, o botão que abre a sessão de dono) — o dry
            run não é pulado, é a MESMA tradução seguida de um passo a mais.
```

Isso responde "nunca pule do dry run pra demo por conta própria": não há
NENHUM caminho de código em que um sinal chega à fila do Meridian sem o
Samyr ter movido aquela estratégia especificamente pra `"demo"` antes —
e mesmo assim, o §9 garante que a ORDEM em si ainda pede confirmação
(`mode: "confirm_each_order"`, forçado pelo servidor Meridian).

---

## 15. O que dá pra validar DESTE lado, e o que precisa do agente

`scripts/mt5_dry_run.py` (novo, testado contra o scan REAL — não uma
fixture) só confere o que é ARITMÉTICA pura do TradingDegens:

| checado aqui (Debian, sem MT5) | não checado aqui — precisa do agente MT5 (Windows) |
|---|---|
| o símbolo tem mapeamento (tabela do §3, por enquanto estimada) | o símbolo EXISTE de verdade na corretora conectada |
| a direção é compra/venda válida | o lote respeita mínimo/máximo/passo do símbolo (`info.volume_min/max/step`) |
| gatilho, SL e TP estão todos presentes | a distância de SL/TP respeita `trade_stops_level` |
| o lote calculado (banca/entrada) é positivo | o mercado está aberto / o símbolo não está suspenso agora |
| — | a conta demo está online e o Algo Trading está ligado |

Rodei o script contra o scan real hoje (2026-09-02) e ele registrou 3
sinais de verdade (AAOI venda/Setup123, IBM venda/Storm123 em dois frames)
com `"aceitaria": null` — nunca `true`, porque a aprovação de verdade só o
`order_check()` do agente pode dar. Testei também o caminho de REJEIÇÃO
(símbolo sem mapeamento, ex. SPCX): `"aceitaria": false`, com o motivo
escrito — a aritmética já reprova antes de precisar da corretora, e o script
não finge que passaria.

### 15.1. Proposta pro Meridian: dry run DE VERDADE, com o agente

O jeito de fechar a lacuna "não checado aqui" é o Meridian ganhar um MODO no
comando da fila (`broker_order_commands.status`, ou um campo novo tipo
`dry_run: boolean`) que faz `execute_demo_order()` (`xm_mt5_bridge.py:590`)
parar LOGO DEPOIS do `mt5.order_check(order)` (linha 671-674) — que já
existe, já roda ANTES de qualquer `order_send()`, e já valida margem, lote e
distância de stop contra a corretora de verdade, sem executar nada — e
reportar o `check.retcode`/`check.comment` de volta pelo mesmo
`report_command()` que já existe, em vez de seguir pro `order_send()`
(linha 675). **Isto é proposta, não implementação**: mexe no repositório do
Meridian, e o pedido é claro que mudanças lá esperam aprovação do Samyr
(§8) antes de eu tocar em qualquer coisa.

---

## 16. O ganho: dry run × demo é a medida do atrito de execução

Com os dois registrados pelo MESMO par de chaves (`strategy_id` +
`ticker`+`frame`+`ts`, igual ao `_chave()` do `mt5_dry_run.py`), dá pra
comparar, sinal a sinal: o que o dry run disse que ENVIARIA (SL/TP/lote
calculados do plano) contra o que a demo realmente executou (preço de
entrada real, que já andou entre o gatilho e o `order_send`; slippage;
spread pago). Essa diferença É o custo de execução que nem o ledger interno
nem o dry run sozinho conseguem medir — só a demo, comparada contra o dry
run que a precede.

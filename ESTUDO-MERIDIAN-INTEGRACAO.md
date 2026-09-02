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
`strategy_id -> magic_number` em vez do `260811` fixo. Proponho reservar
**`260812`** pro TradingDegens (sequencial ao existente, sem colidir) — decisão
do Samyr, não uma escolha que eu travo sozinho.

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
3. **Magic number próprio ou não** (seção 4) — `260812` proposto, dele decidir.
4. **Qual caminho de envio**: o botão manual do §5 (recomendado) ou a rota
   automática (não recomendada agora, mas a decisão é dele se quiser mesmo
   assim).
5. **Quando ligar de verdade** — nada disto liga sozinho; o botão só existe
   quando ele pedir pra eu construir.

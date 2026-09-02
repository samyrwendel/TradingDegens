# Sistema de sinais pro MT5 (conta demo) — estudo de arquitetura

**Data:** 2026-09-01 · **Task:** 20260901-036 · **Pedido do Samyr:** *"quero um sistema de
sinais para usar no MT5 com uma conta demo"*.

> **Isto é um ESTUDO, não uma entrega de execução.** Nenhum código de produção foi alterado,
> nenhuma credencial de corretora foi pedida, nada foi instalado no PC do Samyr (`samyr-srv`).
> A única coisa nova no repositório é este documento e um script de prova de conceito que roda
> **inteiramente neste servidor**, sem tocar em MT5, Windows ou corretora nenhuma — ver seção 6.

---

## ⚠️ O risco que precisa estar dito antes de qualquer arquitetura

O track record medido agora (`/api/scan/verdicts`, ledger real) diz que os dois métodos têm
**expectativa negativa**:

| setup | n fechados | acerto | R:R médio | expectativa |
|---|---|---|---|---|
| Setup123 | 19 | 78,9% | 0,18 | **−0,068R** |
| Storm123 | 29 | 24,1% | 2,04 | **−0,348R** |

Ligar isto a uma execução — mesmo em conta **demo**, mesmo com dinheiro de mentira — é **esperado
perder** no saldo simulado do MT5, pela mesma razão que já perde no paper trading interno
(DA-154/155). Isso **não invalida o projeto**: é exatamente o que uma conta demo serve para medir
— o método com o atrito de execução de verdade (spread, slippage, horário de pregão), não só o
ledger idealizado. Mas ninguém pode ler "está integrado ao MT5" como "está pronto pra dinheiro
real". Não está — os dois métodos, hoje, perdem dinheiro na conta que já mede sem atrito nenhum.

---

## Resumo executivo

- **Cobertura de símbolos: 40% de confiança alta, 60% somando "alta+média"** — o restante depende
  de QUAL corretora, podendo subir bem mais numa corretora de catálogo amplo (seção 1). Isto
  **não inviabiliza** o projeto, mas define o tamanho da watchlist que sobrevive à tradução pro
  MT5 sem confirmação manual.
- **Caminho recomendado:** pacote Python `MetaTrader5` rodando NO `samyr-srv` (Windows), puxando
  sinais do TradingDegens por HTTP — não um Expert Advisor em MQL5 (seção 2).
- **Achado concreto:** com banca fixa de US\$100 (a mesma do paper trading), **cripto de alto
  preço pode cair abaixo do lote mínimo típico da corretora** — ver o exemplo do BTC na seção 4.
  Isso pede ou banca maior pra cripto, ou lote fracionário, dependendo da corretora.
- **Nada roda automaticamente.** O estudo propõe a tradução do sinal; disparar ordem — mesmo em
  demo — fica atrás de um interruptor explícito que o Samyr liga, nunca ligado por padrão.

---

## 1. Os símbolos batem?

O produto varre uma watchlist de **20 ativos** (17 ações americanas + 3 cripto). MT5 não negocia
essas ações e criptos DIRETAMENTE — quem oferece são **corretoras**, via **CFD**, e cada corretora
decide o próprio catálogo e a própria convenção de nome de símbolo (não existe um padrão único —
confirmado pela documentação da comunidade MQL5: sufixos e nomes variam por corretora, e um EA que
espera `"XAUUSD"` quebra numa corretora que usa `"XAUUSDm"`). **Sem escolher uma corretora e abrir
uma conta (nem que seja demo), não dá pra confirmar símbolo por símbolo** — o que segue é uma
ESTIMATIVA por familiaridade de mercado, declarada com o nível de confiança de cada linha.

| ticker | empresa/ativo | confiança de cobertura | por quê |
|---|---|---|---|
| AAPL, MSFT, NVDA, AMD, INTC, IBM, GOOGL | mega-caps | **alta** | quase toda corretora com CFD de ação americana tem os nomes mais líquidos do S&P 500 |
| AVGO, CRWD, TSM | large/mid-cap conhecidas | **média** | populares, mas TSM é ADR estrangeira (nem toda corretora replica ADR) e CRWD/AVGO não são tão universais quanto as "sete magníficas" |
| MRVL, SNDK, MP, AAOI, BE, EOSE | mid/small-cap | **baixa** | corretoras de varejo tendem a cobrir centenas, não milhares, de ações — os nomes menos líquidos costumam ficar de fora |
| SPCX | não identificado com segurança | **desconhecida** | não confirmei a que empresa este ticker se refere a partir do conhecimento disponível — precisa de confirmação humana antes de mapear |
| BTC-USD | Bitcoin | **alta** | cripto CFD quase universal em corretoras MT5 |
| LINK-USD | Chainlink | **média** | corretoras com catálogo cripto mais amplo (10+ pares) costumam ter; as básicas (só BTC/ETH) não |
| ZEC-USD | Zcash | **baixa** | pesquisa (WebSearch, 01/09/2026) não encontrou Zcash nas listas de cripto CFD dos MT5 brokers mais citados (HFM, Pepperstone, Tickmill, MultiBank) — moedas de privacidade são incomuns em CFD por causa de restrição regulatória em várias praças |

**Leitura honesta do número:** contando só "alta" como cobertura garantida, são **8 de 20 (40%)**.
Contando "alta"+"média", **12 de 20 (60%)**. Uma corretora com catálogo de ação AMPLO (algumas
oferecem 500-1000+ CFDs de ação) pode cobrir quase tudo; uma corretora enxuta (só majors + cripto)
cobre pouco mais dos mega-caps. **A única forma de saber o número real é abrir a conta demo da
corretora escolhida e conferir a Observação de Mercado (Market Watch) do terminal** — isso é
trabalho de 10 minutos DEPOIS que o Samyr decidir a corretora (seção 5), não algo que este estudo
consegue substituir sem a conta.

---

## 2. Caminhos de integração — comparados

| caminho | onde roda | como o sinal chega | custo de manutenção | linguagem |
|---|---|---|---|---|
| **(a) pacote Python `MetaTrader5`** | processo Python no `samyr-srv`, ao lado do terminal MT5 aberto | polling HTTP num endpoint do TradingDegens (ou leitura do `/api/scan/salvo`, já público) | **baixo** — mesma linguagem do resto do projeto, reusa a lógica de posição já escrita (DA-154) | Python |
| **(b) Expert Advisor MQL5** | dentro do próprio terminal MT5 | `WebRequest()` do MQL5 pra um endpoint HTTP, com a URL cadastrada na lista branca do terminal | **alto** — MQL5 é uma linguagem à parte, sem as bibliotecas que o resto do projeto usa (JSON, testes automatizados no molde do `pytest` atual); a lógica de tradução do sinal (posição, SL/TP, gate de confiabilidade) teria de ser REESCRITA em MQL5 e mantida em paralelo à versão Python — exatamente o padrão "duas contas divergem" que este próprio projeto já corrigiu internamente mais de uma vez (ex.: DA-125, `ciclo_de_vida` como régua única) | MQL5 |
| **(c) ponte por arquivo/socket** | um script no `samyr-srv` escreve/lê um arquivo que o EA consome, ou abre um socket local | arquivo sincronizado (SMB via Tailscale, ou um poller HTTP que grava local) | **médio**, mas sem vantagem clara sobre (a) — ainda precisa de um processo rodando no Windows pra buscar o dado da rede; só troca "chamar a API direto" por "escrever um arquivo que outro processo lê" | Python + MQL5 |

### Recomendação: **(a)**

O pacote oficial `MetaTrader5` (PyPI) só roda em Python **para Windows** — confirmado por
pesquisa: a documentação da comunidade MQL5 e o próprio pacote dizem que ele **não funciona em
Linux nativamente** (existe uma ponte via Wine, `mt5linux`, mas ela soma uma camada de emulação
frágil — WINE + RPyC — só pra rodar Python de Windows dentro de Linux, quando `samyr-srv` **já é
Windows** e está online na Tailscale agora). Rodar (a) direto no `samyr-srv` elimina essa camada:
o Python ali já é nativo.

(a) ganha de (b)/(c) porque:

1. **Reusa código.** A tradução sinal→ordem (posição, SL/TP, gate de N) já existe em Python
   (`scanner._pnl_paper_trade`, `execucao.confiabilidade`) — em (a) essa lógica pode ser
   IMPORTADA ou espelhada em Python; em (b) ela nasceria reescrita do zero em MQL5, e as duas
   versões podem divergir sem ninguém perceber (o defeito que a DA-125 já corrigiu uma vez para
   o ciclo de vida do padrão).
2. **Testável no mesmo molde.** O resto do projeto testa em `pytest`, offline, com `monkeypatch`
   nos "seams" (ver `tests/test_webui_scanner.py`). Um script Python no Windows pode seguir a
   MESMA disciplina; um EA em MQL5 não tem um framework de teste comparável neste projeto.
3. **Observabilidade.** Logar, alertar, e integrar com o resto da stack (ex.: `scripts/tg_alertas.py`,
   já existente) é natural em Python; em MQL5 seria outra integração à parte.

---

## 3. O que roda onde

```
SERVIDOR (Debian, este repo)                    WINDOWS (samyr-srv, 100.95.182.88, Tailscale)
──────────────────────────────                  ─────────────────────────────────────────────
scanner.scan_watchlist()  ─┐                     terminal MT5 (o Samyr instala e loga
  já roda, produz            │                    a conta DEMO — decisão dele, seção 5)
  os em_gatilho hoje         │
                             │
/api/scan/salvo             │  HTTP (Tailscale,   script Python NOVO ("ponte"), rodando
  já existe, público  ───────┼─  já conectado)  →  como tarefa agendada/serviço:
                             │                       1. faz polling do /api/scan/salvo
[opcional, se aprovado:      │                       2. traduz sinal → símbolo MT5 + lote
 endpoint dedicado           │                          (a MESMA lógica do script da seção 6,
 /api/scan/mt5/sinais,        │                          amadurecida)
 já filtrado pro que          │                       3. SÓ SE o interruptor de execução
 tem mapeamento confiável]    │                          estiver LIGADO (opt-in, nunca
                             ┘                           padrão): mt5.order_send() na
                                                          conta DEMO
```

Nada do lado do servidor precisa de credencial de corretora — ele só PUBLICA o sinal (já publica,
via `/api/scan/salvo`, público e $0). A credencial da corretora fica **inteiramente no
`samyr-srv`**, nas mãos do Samyr, nunca neste repositório nem neste servidor.

---

## 4. Tamanho de posição: US\$100 fixo → lote MT5

O paper trading interno (DA-154) usa **banca fixa de US\$100 por operação**: quantidade =
`banca / preço_de_entrada`. O MT5 pede a posição em **lotes**, e "1 lote" **não tem tamanho
universal** — depende do instrumento e da corretora:

- **Ações CFD:** normalmente 1 lote = 1 ação (mas algumas corretoras usam 1 lote = 100 ações,
  convenção herdada de forex/futuros — confirmar na corretora escolhida).
- **Cripto CFD:** varia MUITO. Um valor comum citado no mercado (não medido numa corretora real)
  é lote mínimo de **0,01**, e "1 lote" às vezes representa 1 unidade da moeda (1 BTC), às vezes
  um contrato de tamanho fixo diferente.

**O achado concreto, rodando o script da seção 6 contra um preço real de BTC (~US\$77.000,
01/09/2026):**

```
lote calculado = 100 / 77000 = 0,0013
```

Se o lote mínimo da corretora for 0,01 (o valor comumente citado), **US\$100 fixos NÃO compram
sequer o lote mínimo em BTC** — a ordem seria rejeitada ou arredondada pra cima, entregando uma
posição de ~US\$770 (0,01 × 77000), não US\$100. Isso quebra a comparabilidade que a banca fixa
existe pra garantir (a mesma régua da DA-154: "posição fixa, não risco fixo" — aqui o problema é
que a posição fixa pretendida nem CABE no lote mínimo).

**Duas saídas, e a decisão é do Samyr:**

1. Banca MAIOR só pra cripto no MT5 (ex.: US\$1.000 em vez de US\$100), aceitando que a
   comparação com o paper interno fica menos direta pra esses 3 ativos; ou
2. Confirmar, na corretora escolhida, se ela permite lote fracionário abaixo de 0,01 (algumas
   permitem, outras não) — só dá pra saber com a conta aberta.

Pra ações, o achado foi o oposto: os exemplos reais do script (seção 6) deram lotes de **0,43 a
0,98** — dentro de qualquer lote mínimo plausível (a maioria das corretoras aceita lote
fracionário de ação a partir de 0,01).

---

## 5. Decisões que só o Samyr pode tomar

1. **Qual corretora / conta demo?** Determina: quais dos 20 símbolos existem de verdade (seção
   1), a convenção de nome exata, o lote mínimo real (seção 4), o horário de pregão (CFD de ação
   segue o horário do mercado subjacente — Nasdaq/NYSE, 9h30–16h ET — mas a corretora pode abrir
   uma janela estendida ou não), e a alavancagem disponível (não bloqueia o tamanho de posição —
   US\$100 de margem cabe em qualquer alavancagem razoável — mas define o RISCO se algum dia
   isto migrar de demo pra real).
2. **Instalar o pacote `MetaTrader5` e o script-ponte no `samyr-srv`.** Precisa do OK dele antes
   de qualquer instalação — nada foi tocado na máquina dele.
3. **O interruptor de execução.** Mesmo em demo, disparar ordem é opt-in explícito — o Samyr
   decide QUANDO ligar, depois de ver os sinais traduzidos rodando em modo simulação por um
   tempo (o script da seção 6 é exatamente esse modo).
4. **Banca de cripto** (US\$100 padrão ou maior — seção 4).

---

## 6. Prova de conceito — a tradução, sem tocar em nada arriscado

`scripts/mt5_sinais_dry_run.py` (novo, neste repositório) faz a parte que **não depende** de
corretora nem de Windows pra existir: lê o scan já salvo (`/api/scan/salvo`, público, $0), acha os
`em_gatilho` de agora, e IMPRIME a ordem MT5 equivalente — símbolo mapeado (com a confiança
declarada), direção, entrada, SL, TP e o lote calculado pela banca fixa. **Não abre conexão com
MT5. Não instala nada. Não envia ordem.**

Rodado agora, contra o scan real:

```
$ uv run python scripts/mt5_sinais_dry_run.py
scan lido de http://127.0.0.1:8781/api/scan/salvo — gerado_em 2026-09-01T21:01:03-04:00
MODO: SIMULAÇÃO. Nenhuma ordem é enviada. Nenhuma conexão MT5 é aberta.

{
  "ticker": "AAOI", "frame": "4h", "dono_do_sinal": "123",
  "mt5_simbolo": "AAOI", "confianca_mapeamento": "baixa",
  "direcao": "venda", "entrada_gatilho": 102.1, "sl": 121.72, "tp": 91.5,
  "banca_alvo_usd": 100.0, "lote_calculado": 0.9794,
  "abaixo_do_lote_minimo_tipico": false
}

{
  "ticker": "IBM", "frame": "1d", "dono_do_sinal": "storm",
  "mt5_simbolo": "IBM", "confianca_mapeamento": "alta",
  "direcao": "venda", "entrada_gatilho": 231.45, "sl": 240.81, "tp": 219.6,
  "banca_alvo_usd": 100.0, "lote_calculado": 0.4321,
  "abaixo_do_lote_minimo_tipico": false
}
```

(Um terceiro sinal, IBM no 4h, saiu igual ao de cima com TP diferente — omitido aqui, está no
output completo do script.)

Isto prova o CONCEITO de ponta a ponta — sinal real → forma que uma ordem MT5 precisa — sem
cruzar nenhuma das linhas que o pedido marcou como fora do escopo desta entrega (nada de
instalação, nada de credencial, nada de execução).

**Próximo passo, só depois da decisão da seção 5:** trocar o mapeamento estimado por um
conferido na corretora real, e portar a mesma lógica pro script Python que roda no `samyr-srv` —
aí sim usando o pacote `MetaTrader5` pra LER a conta demo (posições, saldo), ainda em modo
simulação (sem `order_send`) até o Samyr ligar o interruptor.

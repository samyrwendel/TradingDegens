# Diagnóstico da suíte — onde está o tempo, e o que cortá-lo custa

**Data:** 2026-09-01 · **Task:** 20260901-005 · **Nenhum teste removido nesta rodada.**
Máquina: 4 CPUs. Tudo abaixo foi MEDIDO nesta máquina, não estimado.

## O número que importa

| conjunto | testes | serial | com `-n 4` |
|---|---|---|---|
| **suíte cheia** | 2478 | **1108s** (18m28) | **332s** (5m32) |
| rápidos (`-m "not integration"`) | 1982 | **81s** (1m21) | — já é rápido |
| e2e (`-m integration`) | 496 | ~1027s | **309s** (5m09) |

**Ganho do paralelo: 3,34×, com ZERO teste quebrado.** Rodei a suíte inteira com `-n 4`:
2475 passed, 3 skipped, 0 failed. Nada compartilha estado de forma destrutiva porque
`sobe_servidor` faz bind na porta **0** (efêmera) — cada teste sobe o seu servidor numa porta
que o SO escolhe, então não há colisão de porta a isolar.

## 1) Onde está o tempo

**Não está em poucos testes lentos.** As 30 mais demoradas somam ~180s dos 1108s — **16%**.
O resto é **496 e2e × ~2,07s cada**, e o que domina esses 2s é **subir o Chromium**, não o que
o teste verifica.

Top 5, e o que elas dizem:

| tempo | teste | leitura |
|---|---|---|
| 30,54s | `test_a_meta_e_a_cotacao_ficam_na_MESMA_fileira_sem_buraco` | **defeito de espera**, ver §4 |
| 12,07s | `test_vertical_resizer_drags_and_persists` | arrasta e recarrega: legítimo |
| 11,24s | `test_history_has_no_dividers_and_scrolls` | rolagem real: legítimo |
| 7,70s | `test_resizer_hidden_on_mobile` | dois viewports |
| 6,93s | `test_chart_card_setup_active_has_no_green_border` | pixel |

**Conclusão:** não há gordura concentrada pra cortar. O tempo é o custo de 496 navegadores.

## 2) Paralelizar — medido, e é o maior ganho

`pytest-xdist` instalado (`-n 4`, o número de CPUs). **1108s → 332s.**
Nenhum teste quebrou. Também medido por módulo: `test_webui_maximizar_grafico_e2e` foi de
**22,05s para 11,36s**.

**Recomendação:** adotar `-n auto` como padrão. É o único item da lista que devolve 13 minutos
por execução **sem tocar em teste nenhum**.

## 3) Separar por velocidade — a faixa rápida JÁ EXISTE

Não é preciso criar marcador: os 496 e2e **já** estão marcados com `integration`
(`pytestmark = pytest.mark.integration` no topo de cada módulo).

* **faixa rápida** — `pytest -m "not integration"` → **81s** para 1982 testes (80% da suíte);
* **arquivo tocado** — o caso real de hoje (`test_ciclo_de_vida_do_padrao` + `test_cronologia_do_padrao`):
  **0,90s** para 43 testes;
* **suíte cheia antes do deploy** — `pytest -n auto` → **332s**.

Isto é exatamente a disciplina da DA-109: a faixa rápida roda a cada edição, a cheia uma vez
antes de publicar.

## 4) O achado que não estava na lista: `networkidle`

**O teste mais lento da suíte é lento por causa da espera, não do que verifica** — e foi ele que
falhou nesta execução, por timeout de 30s em `page.goto(base, wait_until="networkidle")`.

`networkidle` espera 500ms sem tráfego. A página tem um **poller de preço a cada 5s**, então a
espera só termina se o `goto` cair numa janela silenciosa. Sob carga, não cai: vira timeout — que
se lê como "a tela quebrou" quando o defeito é do teste.

**84 ocorrências em 46 arquivos.** Troquei isso no meu próprio módulo hoje
(`wait_until="domcontentloaded"` + `wait_for_selector` do que de fato importa) e o módulo saiu de
timeouts intermitentes para **11,7s estáveis**.

**É a proposta de melhor relação custo/benefício depois do paralelo**: corta tempo E mata uma
família inteira de falsos alarmes. Não remove teste nenhum — troca a condição de espera.

## 5) E2E que podia ser unidade — a expectativa NÃO se confirma

Classifiquei os 68 módulos que sobem navegador. Resultado:

* **2 testes** (1 arquivo) não leem nada de renderização;
* **35 testes** (6 arquivos) leem *quase* só dado — mas o que eles leem é
  `dataset.pat123` / `dataset.rotulos123`, que é **telemetria do que o canvas PINTOU**.

Converter esses em unidade reintroduziria exatamente a cegueira que o projeto já documentou
("desenhado ≠ visível"): a telemetria existe *porque* afirmar que o desenho foi pedido não prova
que ele saiu na tela. **Não recomendo converter**; o ganho seria ~35 × 2s = 70s, contra perder a
única prova de que o pixel existe.

## 6) Teste acoplado à implementação — pequeno, não sistêmico

**14 ocorrências**: 8 literais hexadecimais (`"#2ecc71"`) em 5 arquivos e 6 `rgb(...)` em 4.
Numa suíte de 2478 testes, é ruído — não é o padrão que a hipótese sugeria. Vale corrigir por
oportunidade (quando o arquivo for tocado), não em mutirão.

## Estimativa final

| momento | comando | hoje | proposto |
|---|---|---|---|
| durante a edição | arquivo tocado | 0,9s–20s | igual |
| antes de commitar | `-m "not integration"` | 81s | 81s |
| antes do deploy | suíte cheia | **1108s** | **332s** (`-n auto`) |
| idem, com §4 aplicado | suíte cheia | — | **~300s estimado** |

**O corte real é o paralelo: 13 minutos por execução.** Hoje rodei a suíte cheia oito vezes —
seriam ~1h45 devolvidas.

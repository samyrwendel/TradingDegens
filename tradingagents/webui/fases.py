"""A FASE do setup — UM eixo temporal para a tela inteira (task 20260831-021).

O Samyr leu *"Ativo"* como *"em movimento para o alvo"*. Está invertido: `ativo`
quer dizer que o preço está **tocando a zona de entrada AGORA** — é o COMEÇO —, e
quem descreve o meio é o `em_movimento` do scan. O dono do produto supor errado é
a prova do defeito, e o defeito não é a palavra solta: são **taxonomias
convivendo sem tradução**.

Eram três, e a terceira é minha (DA-117):

* **lateral e card** — `ativo` · `aguardar_pullback` · `aguardar_rompimento` ·
  `sem_setup` · `sem_dado` (herdadas do RECUO À MÉDIA);
* **scan** — `em_gatilho` · `em_movimento` · `invalidou` · `formando` (do 1-2-3);
* **sinais** — `entrada` · `a_caminho` · `passou` · `conflito` (da oportunidade).

**Elas NÃO são redundantes — e é por isso que não se fundem numa lista só.** Cada
uma descreve um sujeito diferente: o PLANO da run, a LEITURA de um frame, e a
OPORTUNIDADE agregada. Colapsá-las jogaria fora distinção que o dado tem (o
`aguardar_pullback` espera um recuo; o `formando` espera um rompimento — coisas
diferentes de se esperar).

**O que elas compartilham é o EIXO TEMPORAL, e é ele que passa a ser único.**
Cinco fases, cinco palavras, as MESMAS em toda superfície (a quinta, ``encerrado``,
entrou na DA-125, quando se descobriu que um trade que já tinha chegado ao alvo
saía rotulado "invalidado" — veredito invertido em relação ao dinheiro):

======================  ===================================================
``agora``               o preço está no ponto de entrar — é a hora de agir
``esperando``           o gatilho ainda não veio
``andou``               acionou e o preço já passou da entrada
``encerrado``           o trade terminou — chegou ao alvo ou ao stop (DA-125)
``morreu``              a premissa rompeu; não há trade
======================  ===================================================

Mais ``sem_leitura``, que não é fase: é ausência de leitura, e chamá-la de fase
faria "não sei" parecer um estado do trade.

**O MECANISMO vira qualificador, não sinônimo.** A tela escreve a fase primeiro e
o mecanismo em seguida — *"AGUARDANDO · recuo à média"*, *"AGUARDANDO · rompimento
do ponto 2"*. É a tradução explícita que o eixo pedia: mesma fase, mecanismos
diferentes, e ninguém precisa deduzir qual é qual.

**A palavra "Ativo" sai de vez.** Ela é o único rótulo do produto cuja leitura
natural em português ("está ativo", "está rodando") aponta para a fase ERRADA.
Trocada por **"NA ENTRADA"**, que não tem como ser lida como "já andou".

**Custo do defeito, que é o motivo de isto ser regra e não gosto:** confundir
"hora de entrar" com "já andou" é entrar TARDE — o que produz exatamente os R:R
0,06 medidos na task 012 e a mediana de 0,23 no gatilho do 1-2-3 (DA-117).

**Este módulo é a AUTORIDADE.** O JavaScript espelha a tabela e um teste solda os
dois: sem ele, a próxima palavra nova nasce só de um lado — que é como as três
taxonomias apareceram.
"""
from __future__ import annotations

# As fases, na ordem do TEMPO. A ordem importa: é ela que a tela usa para empilhar
# as seções, e uma ordem por gosto faria "já andou" aparecer antes de "na entrada"
# em alguma superfície. `encerrado` vem depois de `andou` e ANTES de `morreu`,
# porque é isso que ele é: o trade chegou ao fim — e um trade encerrado não se
# invalida depois (DA-125).
ORDEM = ("agora", "esperando", "andou", "encerrado", "morreu", "sem_leitura")

FASE_PT = {
    "agora": "NA ENTRADA",
    "esperando": "AGUARDANDO",
    "andou": "JÁ ANDOU",
    "encerrado": "ENCERRADO",
    "morreu": "INVALIDADO",
    "sem_leitura": "SEM LEITURA",
}

# O que cada fase quer dizer, em uma frase. Vai para o ``title`` e para o
# cabeçalho de seção: rótulo sozinho não ensina o eixo a quem chega agora.
FASE_AJUDA = {
    "agora": "o preço está no ponto de entrar — é a hora de agir",
    "esperando": "o gatilho ainda não veio",
    "andou": "acionou e o preço já passou da entrada",
    "encerrado": "o trade terminou — chegou ao alvo ou ao stop",
    "morreu": "a premissa rompeu — não há trade",
    "sem_leitura": "não há leitura para este ativo neste frame",
}

# ---------------------------------------------------------------- as traduções --
# Cada dicionário mapeia UMA taxonomia existente para a fase. Eles não somem: o
# estado detalhado continua no dado e na tela como qualificador.

DO_SETUP_STATE = {
    "ativo": "agora",
    "aguardar_pullback": "esperando",
    "aguardar_rompimento": "esperando",
    "sem_setup": "sem_leitura",
    "sem_dado": "sem_leitura",
    "intradiario_indisponivel": "sem_leitura",
}

DO_SCAN_ESTADO = {
    "em_gatilho": "agora",
    "formando": "esperando",
    "em_movimento": "andou",
    "concluido": "encerrado",
    "invalidou": "morreu",
    "sem_setup": "sem_leitura",
    "sem_dado": "sem_leitura",
    # Estados que só o Storm produz: o veto do Éden e a zona neutra não são fases
    # do trade — são o filtro dizendo "não opera" e "opera, com aviso".
    "vetado": "sem_leitura",
    "zona_neutra": "agora",
}

DA_OPORTUNIDADE = {
    "entrada": "agora",
    "a_caminho": "esperando",
    "passou": "andou",
    # CONFLITO não é fase: é a leitura estando dividida. Ele não entra no eixo
    # temporal porque não há um momento do trade a apontar — há dois lados.
    "conflito": None,
}

# O MECANISMO — o que se está esperando, quando a fase é ``esperando``, e de qual
# leitura veio a entrada nas demais. É o qualificador que a fase não substitui.
MECANISMO_PT = {
    "aguardar_pullback": "recuo à média",
    "aguardar_rompimento": "rompimento do ponto 2",
    "formando": "padrão formando",
    "ativo": "recuo à média",
    "em_gatilho": "gatilho rompido",
    "em_movimento": "gatilho ficou para trás",
    "concluido": "trade encerrado",
    "invalidou": "premissa rompida",
}


def _traduz(tabela: dict, estado, padrao: str | None = "sem_leitura"):
    if estado is None:
        return padrao
    return tabela.get(str(estado), padrao)


def de_setup_state(setup_state) -> str | None:
    """A fase do ``setup_state`` do plano (lateral e card da análise)."""
    return _traduz(DO_SETUP_STATE, setup_state)


def de_scan_estado(estado) -> str | None:
    """A fase do estado de uma linha do scan (por ativo e frame)."""
    return _traduz(DO_SCAN_ESTADO, estado)


def da_oportunidade(estado) -> str | None:
    """A fase de uma oportunidade (DA-117). ``conflito`` devolve ``None``."""
    return _traduz(DA_OPORTUNIDADE, estado, padrao=None)


def rotulo(fase) -> str:
    """O rótulo pt-BR da fase, ou string vazia quando não há fase."""
    return FASE_PT.get(str(fase), "") if fase else ""


def mecanismo(estado) -> str:
    """O qualificador (o QUE se espera / de onde veio), ou string vazia."""
    return MECANISMO_PT.get(str(estado), "") if estado else ""

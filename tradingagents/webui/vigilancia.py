"""VIGILÂNCIA DE NÍVEL — o preço cruza; o fechamento decide (DA-138).

O buraco que o Samyr achou: *"imagina que deu um setup num certo horário, só que
durante a próxima hora ele conseguiu invalidar o setup. A gente só vai ver 45
minutos depois"*.

**A invalidação estrutural NÃO tem buraco** e isso é importante para não se
"consertar" o que não está quebrado: ela é medida por FECHAMENTO
(``_primeira_barra_alem`` procura *a primeira barra que fecha além do nível*), de
propósito. Se a régua é o fechamento do candle de 1h, varrer a cada fechamento de
1h não atrasa nada — a invalidação **não existe** antes de o candle fechar.

**O stop e o alvo, sim.** O stop é executado no PREÇO, intrabar: se o preço perfura
o stop aos 05 minutos da hora, o trade morreu ali. E o alvo pode ser tocado e
devolvido dentro da mesma hora — foi exatamente o LINK-USD da DA-125 (alvo às 15:00,
colapso às 23:00).

**A separação, que é o coração desta decisão:**

* **VARREDURA ESTRUTURAL** — cara (séries de quatro frames, recálculo de padrão,
  Éden, níveis). Uma por candle fechado, como o Samyr decidiu.
* **VIGILÂNCIA DE NÍVEL** — barata: a cotação corrente comparada contra níveis **já
  calculados**. Zero recálculo, zero série, zero detector.

**A vigilância AVISA; o fechamento DECIDE.** "stop perfurado às 15:20 (preço)" é um
fato de natureza diferente de "padrão invalidado no fechamento das 16:00", e os dois
convivem na tela **declarados**. Esta função **não** produz estado de padrão, não
altera veredito e não escreve no ledger: ela devolve fatos com hora, e quem os
mostra diz de onde vieram. Deixá-la virar uma segunda fonte de verdade — capaz de
discordar da primeira — é o único jeito de esta ideia dar errado.
"""

from __future__ import annotations

from typing import Any

# Os níveis vigiados, e a natureza de cada um. O GATILHO também entra: ele é o que
# transforma "formando" em "acionado", e é intrabar como os outros.
_NIVEIS = (
    ("stop", "sl", "o stop foi perfurado pelo preço"),
    ("alvo", "tp", "o alvo foi tocado pelo preço"),
    ("gatilho", "trigger", "o gatilho foi tocado pelo preço"),
)

# Fases em que ainda existe trade a vigiar. Um padrão encerrado ou invalidado não
# tem nível a cruzar — o preço passando por ali de novo não é evento dele (DA-129).
_VIVAS = ("em_gatilho", "em_movimento", "formando", "zona_neutra")


def _cruzou(nivel: float, anterior: float | None, agora: float, compra: bool,
            tipo: str) -> bool:
    """O preço ALCANÇOU o nível — pela direção que o torna um evento.

    Sem cotação anterior, o critério é o alcance (o preço está do lado de lá); com
    ela, exige-se a TRAVESSIA, que é o que evita repetir o mesmo aviso a cada
    janela de 30s enquanto o preço fica parado além do nível.
    """
    if tipo == "stop":
        alcancou = agora <= nivel if compra else agora >= nivel
    else:   # alvo e gatilho: alcançar é ir na direção do trade
        alcancou = agora >= nivel if compra else agora <= nivel
    if not alcancou:
        return False
    if anterior is None:
        return True
    antes = (anterior <= nivel if compra else anterior >= nivel) if tipo == "stop" \
        else (anterior >= nivel if compra else anterior <= nivel)
    return not antes          # só quando ATRAVESSOU nesta janela


def cruzamentos(frames: list[dict[str, Any]], preco: float | None,
                anterior: float | None = None, *, quando: str = "") -> list[dict]:
    """Os níveis que a cotação corrente cruzou, por frame e por método.

    ``frames`` são as linhas do scan **já calculadas** — nada aqui recalcula
    estrutura. Devolve lista vazia quando não há preço, o que é a leitura honesta:
    sem cotação não há o que vigiar.
    """
    if preco is None or not frames:
        return []
    fora = []
    for linha in frames:
        for metodo, dados in (("Setup123", linha),
                              ("Storm123", linha.get("storm") or {})):
            if not dados or dados.get("estado") not in _VIVAS:
                continue
            compra = dados.get("direction") != "venda"
            for tipo, chave, frase in _NIVEIS:
                nivel = dados.get(chave)
                if nivel is None:
                    continue
                try:
                    nivel = float(nivel)
                except (TypeError, ValueError):
                    continue
                if _cruzou(nivel, anterior, float(preco), compra, tipo):
                    fora.append({
                        "frame": linha.get("frame"), "metodo": metodo,
                        "nivel": tipo, "preco_nivel": round(nivel, 4),
                        "preco": round(float(preco), 4), "quando": quando,
                        "direcao": "compra" if compra else "venda",
                        # A FRASE VIAJA PRONTA: a tela não reescreve o que isto
                        # significa, e "(preço)" é o que separa este fato do
                        # veredito estrutural, que sai do FECHAMENTO.
                        "texto": f"{frase} em {round(float(preco), 4)}",
                        "fonte": "preço",
                    })
    return fora

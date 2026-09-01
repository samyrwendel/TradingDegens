"""O CICLO DE VIDA nos registros ANTIGOS, com a MESMA régua (DA-130).

O histórico é persistido inteiro e **não se reescreve** — é a disciplina que deixa
reabrir a análise de ontem e ver a tela daquele dia. Mas isso tem um custo que só
apareceu quando o veredito foi corrigido: **as runs gravadas antes da DA-125 guardam
o veredito invertido**. A run do LINK-USD que originou toda esta série
(``20260830-232525-ca31d7``) continua no disco com ``invalidado: True`` e sem
``desfecho`` — reabri-la mostrava "INVALIDADO" oito horas depois de o trade ter
atingido o alvo, exatamente a tela de que o Samyr reclamou.

Duas saídas ruins e uma boa:

* **reescrever o registro** — quebra o append-only, e uma análise não é um número
  que se conserta: ela é o que o sistema disse naquele dia;
* **recalcular no front** — seria uma SEGUNDA implementação da régua, em JS, que é
  precisamente como o 1-2-3 e o Storm passaram a discordar (DA-126);
* **derivar na LEITURA, com a régua de sempre** — o registro no disco fica intacto,
  e quem lê recebe o veredito correto. É esta.

Roda só quando o padrão **não tem** ``ciclo`` (registro anterior à DA-129) e há
candles guardados para conferir. Sem candles, sem gatilho ou sem nível, devolve o
que estava lá: nunca inventa um desfecho que a série não mostra.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_FMT = "%Y-%m-%d %H:%M"
_FMT_DIA = "%Y-%m-%d"


def _serie(candles: list[dict[str, Any]]):
    """As velas guardadas viram o DataFrame que a régua consome."""
    import pandas as pd
    if not candles:
        return None, _FMT
    datas = pd.to_datetime([c.get("d") for c in candles], errors="coerce")
    if datas.isna().any():
        return None, _FMT
    # O carimbo das velas diárias não tem hora; a régua compara STRINGS de data, e
    # um formato com hora zerada ordenaria igual mas escreveria "2026-08-30 00:00"
    # onde o resto da tela escreve "2026-08-30".
    fmt = _FMT if any(" " in str(c.get("d") or "") for c in candles) else _FMT_DIA
    return pd.DataFrame({
        "Date": datas,
        "High": [c.get("h") for c in candles],
        "Low": [c.get("l") for c in candles],
        "Close": [c.get("c") for c in candles],
    }), fmt


def _aplica(pat: dict[str, Any], df, fmt: str, nivel, trigger, alvo, stop) -> bool:
    from tradingagents.dataflows.price_structure import (
        _morte_e_desfecho,
        ciclo_de_vida,
    )
    p3 = ((pat.get("p3") or {}).get("date"))
    if not p3 or trigger is None:
        return False
    idx = df.index[df["Date"].dt.strftime(fmt) == str(p3)]
    if not len(idx):
        return False
    em, desfecho, acionado = _morte_e_desfecho(
        df, int(idx[-1]), float(nivel) if nivel is not None else None,
        pat.get("direction") != "venda", fmt, float(trigger),
        float(alvo) if alvo is not None else None,
        float(stop) if stop is not None else None)
    pat["invalidado"] = em is not None and desfecho is None
    pat["invalidado_em"] = em
    pat["desfecho"] = desfecho
    pat["encerrado"] = desfecho is not None
    pat["acionado_em"] = acionado
    pat["ciclo"] = ciclo_de_vida(acionado_em=acionado, invalidado_em=em,
                                 desfecho=desfecho)
    return True


_DERIVADOS = ("invalidado", "invalidado_em", "desfecho", "encerrado",
              "acionado_em", "ciclo")


def _espelha(origem: dict[str, Any], destino: Any) -> None:
    """Copia o veredito derivado para a OUTRA cópia do mesmo padrão.

    Só quando é reconhecidamente o mesmo padrão (mesmo ponto 3 e mesmo gatilho) e o
    destino ainda não tem ciclo: um marcador de outro padrão não herda veredito.
    """
    if not isinstance(destino, dict) or destino.get("ciclo"):
        return
    if (destino.get("p3") or {}).get("date") != (origem.get("p3") or {}).get("date"):
        return
    if destino.get("trigger") != origem.get("trigger"):
        return
    for k in _DERIVADOS:
        destino[k] = origem.get(k)


def completa_ciclo(result: Any) -> Any:
    """Devolve ``result`` com o ``ciclo`` dos padrões preenchido, quando dá.

    No-op para qualquer coisa que não seja um dicionário com padrão e velas, então é
    seguro deixar em todo caminho de leitura.
    """
    if not isinstance(result, dict):
        return result
    a = result.get("actionable")
    if not isinstance(a, dict):
        return result
    candles = ((result.get("price_chart") or {}).get("candles")) or []
    df = fmt = None
    try:
        # 1-2-3: os níveis moram no próprio plano.
        pat = a.get("pattern")
        if isinstance(pat, dict) and not pat.get("ciclo"):
            df, fmt = _serie(candles)
            if df is not None:
                _aplica(pat, df, fmt, (a.get("invalidation") or {}).get("price"),
                        pat.get("trigger"), (a.get("target") or {}).get("price"),
                        (a.get("stop") or {}).get("price"))
            # O MESMO PADRÃO VIVE EM DOIS LUGARES do registro: no plano e nos
            # marcadores do gráfico — e é do marcador que a tela PINTA. Derivar só no
            # plano deixava o card dizendo "ENCERRADO NO ALVO" e a vela pintando o
            # cinza de invalidado, que é a contradição desta série inteira. Os campos
            # são COPIADOS (não recalculados): é o mesmo 1-2-3, e duas apurações do
            # mesmo fato é como os métodos começaram a divergir (DA-126).
            _espelha(pat, ((result.get("price_chart") or {}).get("markers") or {})
                     .get("pattern_123"))
        # STORM: o alvo vive DENTRO da leitura, e a que decide é a de gatilho mais
        # próximo do preço — a mesma que a linha do scan publica (DA-126).
        st = a.get("storm")
        spat = st.get("pattern") if isinstance(st, dict) else None
        if isinstance(spat, dict) and not spat.get("ciclo"):
            if df is None:
                df, fmt = _serie(candles)
            leituras = st.get("leituras") or []
            preco = result.get("as_of_price") or a.get("price")
            if df is not None and leituras:
                from tradingagents.dataflows.price_structure import (
                    _leitura_de_referencia,
                )
                L = _leitura_de_referencia(leituras, preco) or {}
                _aplica(spat, df, fmt, (st.get("invalidation") or {}).get("price"),
                        L.get("trigger"), (L.get("target") or {}).get("price"),
                        (st.get("stop") or {}).get("price"))
    except Exception as exc:  # noqa: BLE001 — leitura de histórico nunca derruba a tela
        logger.info("ciclo do registro antigo não pôde ser derivado: %s", exc)
    return result

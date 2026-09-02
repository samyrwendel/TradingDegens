"""Carteira-ESPELHO em PAPER que CLONA as entradas e saídas do Erick (task
20260902-055) — replica MECANICAMENTE o que ele faz, não modela o raciocínio dele.

**O que este módulo NÃO faz.** Não lê método, não gera parecer, não decide nada.
Ele consome as MUDANÇAS que :func:`alertas_tg.mudancas` já detecta (entrou / saiu /
aumentou / reduziu, por ticker) e transforma cada uma numa OPERAÇÃO de paper na
nossa carteira virtual. Nenhuma execução de dinheiro real — só ledger.

**A REGRA INEGOCIÁVEL (e o Samyr pagou caro por ela).** O preço de entrada do clone
é o preço REAL no instante em que NÓS detectamos a mudança — via
:func:`live_price.fetch_live_price` —, NUNCA o ``precoMedio`` dele, NUNCA o preço do
dia em que ele entrou. Usar o preço dele faria o clone "lucrar" o movimento que já
tinha acontecido antes de sabermos: é exatamente o bug que as tasks 035/044/047
desenterraram (o "+6" virou +9 quando a entrada passou a ser o preço de verdade). O
``precoMedio`` dele é gravado no ledger SÓ como campo de auditoria — a prova de que
NÃO é ele que entra na conta.

**Replica o PESO, não a quantidade.** Capital diferente: a quantidade dele não
transporta. O clone mira o ``peso_agora`` — o pct do capital que aquela posição vale
DEPOIS da mudança — aplicado a um bankroll nominal fixo (:data:`CLONE_CAPITAL`). O
RETORNO em % é invariante à escala do bankroll; o número só serve pra pesar as
posições entre si.

**A DEFASAGEM é gravada em cada operação.** A detecção é periódica (o alerta roda de
hora em hora, e o timer diário às 09:15), então existe um atraso entre o que ele faz
e o que a gente vê. Cada op carrega a data da mudança DELE (grosseira — ``entrada``
por posição nova, ``atualizado`` do snapshot para os ajustes) contra a data/preço da
NOSSA entrada, e a defasagem em dias. É esse campo que separa "seguir o Erick" de
"ter feito o que o Erick fez". Sem ele o clone mente a favor.

O saldo é sempre DERIVADO do ledger append-only (mesma disciplina da carteira
virtual do scan, DA-155): :func:`replay` relê as operações do zero; nunca há um saldo
persistido à parte pra divergir. E o resumo declara os TRÊS estados (DA-157): tem
número / não tem mudança ainda / detectou mas sem cotação — "amostra insuficiente"
nunca é "0%" disfarçado.
"""

from __future__ import annotations

import contextlib
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Bankroll NOMINAL do clone. NÃO é o dinheiro dele nem o nosso — é só o escalador que
# transforma "peso (pct do capital)" em tamanho de posição. O retorno em % que o
# resumo reporta é invariante a este número; ele existe só pra as posições terem
# peso relativo entre si. 70000 casa com o `aporteInicial` do snapshot dele por
# conveniência de leitura, não porque o valor importe.
CLONE_CAPITAL = 70000.0

# Só-DM, conteúdo de assinatura: o ledger é dado nosso (operações de paper), mora no
# runtime como o resto do estado dos alertas — nunca no repo.
_MESES = {"jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
          "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12}


def _base_dir() -> Path:
    return Path(os.environ.get("CLONE_ERICK_DIR")
                or (Path.home() / ".tradingagents" / "clone-erick"))


def ledger_path() -> Path:
    return _base_dir() / "operacoes.jsonl"


# ── datas & defasagem ──────────────────────────────────────────────────────────
def _data_epoch(epoch: float | None) -> date | None:
    if not epoch:
        return None
    with contextlib.suppress(Exception):
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).date()
    return None


def _parse_data_dele(txt: str | None) -> tuple[date | None, str]:
    """A data (grosseira) da mudança DELE e a granularidade dela.

    Ele escreve "jul/2026" (mês) na ``entrada`` e "27/08/2026" (dia) no
    ``atualizado``. A granularidade viaja junto porque é honestidade: uma defasagem
    calculada contra um mês inteiro não é a mesma coisa que contra um dia.
    """
    s = (txt or "").strip().lower()
    if not s or s == "-":
        return None, "ausente"
    # dd/mm/aaaa
    with contextlib.suppress(Exception):
        if s.count("/") == 2 and s.replace("/", "").isdigit():
            d, m, a = (int(x) for x in s.split("/"))
            return date(a, m, d), "dia"
    # mmm/aaaa (mês por extenso abreviado)
    if "/" in s:
        mes_txt, _, ano_txt = s.partition("/")
        mes = _MESES.get(mes_txt[:3])
        if mes and ano_txt.isdigit():
            return date(int(ano_txt), mes, 1), "mes"
    return None, "indecifravel"


def _defasagem(deteccao: date | None, dele: date | None) -> int | None:
    if deteccao is None or dele is None:
        return None
    return (deteccao - dele).days


# ── preço REAL na detecção ──────────────────────────────────────────────────────
def _preco_real(ticker: str, classe: str | None) -> dict[str, Any] | None:
    """Cotação REAL de agora — o único preço que o clone tem direito de usar.

    Cripto precisa do sufixo ``-USD`` pra fonte entender; ação passa o ticker. NUNCA
    usa o ``simbolo`` do snapshot dele (ex.: "BINANCE:BTCUSDT", que a fonte de
    cotação não conhece) nem, jamais, o ``precoMedio`` dele.
    """
    from tradingagents.dataflows.live_price import fetch_live_price

    sym = ticker
    if str(classe or "").strip().lower().startswith("crip"):
        sym = f"{ticker}-USD"
    return fetch_live_price(sym)


PrecoFn = Callable[[str, str | None], "dict[str, Any] | None"]


# ── uma mudança detectada → uma operação de clone ───────────────────────────────
def op_de_mudanca(m: dict[str, Any], atual: dict[str, Any] | None,
                  preco_info: dict[str, Any] | None) -> dict[str, Any]:
    """Constrói a operação de paper a partir de UMA mudança detectada + o preço REAL.

    Função pura (sem I/O) de propósito: é aqui que a regra "o preço é o REAL da
    detecção, nunca o ``precoMedio`` dele" vive, e é aqui que o teste-dente morde.
    ``preco_info`` é o retorno de :func:`live_price.fetch_live_price` (ou ``None``).
    """
    ticker = str(m.get("ticker") or "").upper()
    classe = m.get("classe") or ""
    tipo = m.get("tipo")
    lido_em = (atual or {}).get("lido_em")
    ativos = ((atual or {}).get("carteira") or {}).get("ativos") or []
    dele = next((a for a in ativos
                 if str(a.get("ticker") or "").upper() == ticker), {})
    atualizado = ((atual or {}).get("carteira") or {}).get("atualizado")

    preco = None
    if preco_info is not None:
        try:
            preco = float(preco_info.get("price"))
        except (TypeError, ValueError):
            preco = None
        if preco is not None and preco <= 0:
            preco = None

    # Data da mudança DELE: pra uma posição nova, a `entrada` dela é o sinal mais
    # específico; pros ajustes (aumentou/reduziu/saiu) não há data por-operação, então
    # o `atualizado` do snapshot é o melhor proxy do "quando ele mexeu".
    if tipo == "entrou":
        base_txt, base_nome = dele.get("entrada"), "entrada"
    else:
        base_txt, base_nome = atualizado, "atualizado"
    d_dele, granularidade = _parse_data_dele(base_txt)
    d_det = _data_epoch(lido_em)

    incluido = preco is not None
    return {
        "ts": (datetime.fromtimestamp(float(lido_em), tz=timezone.utc).isoformat()
               if lido_em else datetime.now(timezone.utc).isoformat()),
        "ts_epoch": lido_em,
        "ticker": ticker,
        "classe": classe,
        "tipo": tipo,
        # O ALVO do clone: o peso (pct do capital) DEPOIS da mudança. É o que se
        # replica — a quantidade dele não transporta (capital diferente).
        "peso_alvo": float(m.get("peso_agora") or 0.0),
        # O PREÇO que o clone usa: REAL, da detecção. NUNCA o precoMedio dele.
        "preco": preco,
        "preco_sessao": (preco_info or {}).get("sessao"),
        "preco_rotulo": (preco_info or {}).get("rotulo"),
        "incluido": incluido,
        "motivo_exclusao": None if incluido else "sem cotação real na detecção",
        # ── DEFASAGEM (o que separa seguir de ter feito) ──
        "dele_entrada": dele.get("entrada"),
        "dele_atualizado": atualizado,
        "defasagem_base": base_nome,
        "defasagem_granularidade": granularidade,
        "defasagem_dias": _defasagem(d_det, d_dele),
        # ── AUDITORIA: o preço DELE fica gravado SÓ pra provar que não é ele que
        # entra na conta. Se um dia alguém trocar `preco` por isto, o teste-dente
        # (test_clone_erick) quebra. ──
        "dele_precoMedio": dele.get("precoMedio"),
        "qtd_antes_dele": m.get("qtd_antes"),
        "qtd_agora_dele": m.get("qtd_agora"),
    }


# ── gravação (append-only) ──────────────────────────────────────────────────────
def registrar(mudou: list[dict[str, Any]], atual: dict[str, Any] | None, *,
              preco_fn: PrecoFn | None = None,
              path: str | os.PathLike | None = None) -> list[dict[str, Any]]:
    """Transforma cada mudança detectada numa operação e ANEXA ao ledger.

    O caixa nunca vira operação (não é posição — é o residual de toda compra/venda,
    mesma regra do ``formata_carteira``). ``preco_fn(ticker, classe)`` é injetável
    pra o teste poder cravar um preço REAL diferente do ``precoMedio`` dele; o
    default busca a cotação viva. Devolve as operações gravadas.
    """
    if not mudou:
        return []
    preco_fn = preco_fn or _preco_real
    alvo = Path(path) if path else ledger_path()
    alvo.parent.mkdir(parents=True, exist_ok=True)
    ops: list[dict[str, Any]] = []
    for m in mudou:
        if str(m.get("classe") or "").strip().lower() == "caixa":
            continue
        info = None
        with contextlib.suppress(Exception):
            info = preco_fn(str(m.get("ticker") or "").upper(), m.get("classe"))
        ops.append(op_de_mudanca(m, atual, info))
    if ops:
        with alvo.open("a", encoding="utf-8") as fh:
            for op in ops:
                fh.write(json.dumps(op, ensure_ascii=False) + "\n")
    return ops


def carrega_ledger(path: str | os.PathLike | None = None) -> list[dict[str, Any]]:
    alvo = Path(path) if path else ledger_path()
    if not alvo.exists():
        return []
    out: list[dict[str, Any]] = []
    for linha in alvo.read_text(encoding="utf-8").splitlines():
        with contextlib.suppress(ValueError):
            if linha.strip():
                out.append(json.loads(linha))
    return out


# ── saldo DERIVADO do ledger (replay) ───────────────────────────────────────────
def replay(ops: list[dict[str, Any]],
           precos_atuais: dict[str, float] | None = None) -> dict[str, Any]:
    """Relê o ledger do zero e devolve o estado da carteira-espelho.

    Dimensiona cada posição pelo PESO ALVO aplicado ao bankroll nominal
    (:data:`CLONE_CAPITAL`) e realiza PnL sempre contra os NOSSOS preços reais de
    entrada — nunca contra o preço dele. ``precos_atuais`` (ticker→preço) marca as
    posições abertas a mercado pro não-realizado; ausente, marca a custo (0 de
    não-realizado, honesto: sem cotação não se afirma lucro).
    """
    precos_atuais = precos_atuais or {}
    cap = CLONE_CAPITAL
    cash = cap
    pos: dict[str, dict[str, float]] = {}
    realizado = 0.0
    n_incl = 0
    for op in sorted(ops, key=lambda o: str(o.get("ts") or "")):
        if not op.get("incluido"):
            continue
        preco = op.get("preco")
        try:
            preco = float(preco)
        except (TypeError, ValueError):
            continue
        if preco <= 0:
            continue
        n_incl += 1
        t = str(op.get("ticker") or "").upper()
        tipo = op.get("tipo")
        w = float(op.get("peso_alvo") or 0.0)
        cur = pos.get(t) or {"units": 0.0, "custo": 0.0}
        if tipo == "entrou":
            alvo_val = w * cap
            cur = {"units": alvo_val / preco, "custo": alvo_val}
            cash -= alvo_val
            pos[t] = cur
        elif tipo in ("aumentou", "reduziu"):
            alvo_val = w * cap
            delta_val = alvo_val - cur["units"] * preco
            if delta_val >= 0:                      # compra pra chegar no peso alvo
                cur["units"] += delta_val / preco
                cur["custo"] += delta_val
                cash -= delta_val
            else:                                    # venda parcial
                venda_units = -delta_val / preco
                avg = cur["custo"] / cur["units"] if cur["units"] > 1e-12 else preco
                realizado += venda_units * (preco - avg)
                cur["units"] -= venda_units
                cur["custo"] -= venda_units * avg
                cash += venda_units * preco
            pos[t] = cur
        elif tipo == "saiu":
            cur = pos.pop(t, {"units": 0.0, "custo": 0.0})
            if cur["units"] > 1e-12:
                avg = cur["custo"] / cur["units"]
                realizado += cur["units"] * (preco - avg)
                cash += cur["units"] * preco
    nao_realizado = 0.0
    abertos: dict[str, dict[str, Any]] = {}
    valor_posicoes = 0.0
    for t, cur in pos.items():
        if cur["units"] <= 1e-9:
            continue
        avg = cur["custo"] / cur["units"]
        pa = precos_atuais.get(t)
        marca = pa if pa else avg                    # sem cotação → marca a custo
        valor_posicoes += cur["units"] * marca
        if pa:
            nao_realizado += cur["units"] * (pa - avg)
        abertos[t] = {"units": cur["units"], "preco_medio_clone": avg,
                      "preco_atual": pa}
    equity = cash + valor_posicoes
    return {
        "capital_nominal": cap,
        "cash": cash,
        "realizado": realizado,
        "nao_realizado": nao_realizado,
        "equity": equity,
        "retorno_pct": (equity - cap) / cap if cap else 0.0,
        "n_ops_incluidas": n_incl,
        "posicoes_abertas": abertos,
    }


# ── resumo com os TRÊS estados (DA-157) ─────────────────────────────────────────
def resumo(ops: list[dict[str, Any]],
           precos_atuais: dict[str, float] | None = None) -> dict[str, Any]:
    """Quanto o clone renderia até hoje — ou por que ainda não dá pra dizer.

    Nunca devolve "0%" travestido de "sem dado" (DA-157): distingue "não detectou
    mudança ainda" de "detectou mas sem cotação real" de "tem número".
    """
    if not ops:
        return {"estado": "amostra_insuficiente",
                "motivo": "nenhuma mudança detectada ainda — o clone só abre "
                          "posição quando a carteira dele muda entre duas leituras"}
    incluidas = [o for o in ops if o.get("incluido") and o.get("preco")]
    if not incluidas:
        return {"estado": "amostra_insuficiente",
                "motivo": f"{len(ops)} mudança(s) detectada(s), mas nenhuma com "
                          "cotação real no instante da detecção — sem preço nosso "
                          "não se afirma retorno"}
    r = replay(ops, precos_atuais)
    return {"estado": "ok", **r,
            "n_ops_total": len(ops),
            "n_ops_sem_preco": len(ops) - len(incluidas)}

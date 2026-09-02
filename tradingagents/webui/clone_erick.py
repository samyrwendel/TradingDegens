"""Carteira-ESPELHO em PAPER que CLONA as PRÓXIMAS entradas e saídas do Erick
(tasks 20260902-055/056) — replica MECANICAMENTE o que ele faz, não modela o
raciocínio dele.

**Nasce VAZIA e só segue o futuro (task 056).** O Samyr fechou o desenho: uma
carteira do zero, com um capital que ELE vai definir, que segue SÓ as próximas
entradas e saídas. Consequência dura: NÃO se replicam as posições que ele já tem
hoje (nada de abrir MSFT e o resto do snapshot atual), e NÃO há backfill nem
reconstrução de histórico — o track record do clone começa do zero e cresce com os
eventos reais. Isso não pede marcação nova: é exatamente a semântica que
:func:`alertas_tg.mudancas` já tem ("sem leitura anterior devolve ``[]`` — a
PRIMEIRA leitura não é mudança"). O clone guarda a SUA PRÓPRIA baseline; a primeira
leitura depois de ligado é só a baseline (zero operação), e daí pra frente cada
mudança vira operação.

**Por que esse desenho é mais honesto.** Começando do zero e só com evento futuro, o
clone não tem como creditar movimento que já aconteceu — que é justamente a família
de bug que dominou o dia (tasks 035/044/047, o "+6" que virou +9 quando a entrada
passou a ser o preço de verdade).

**O CAPITAL é PARÂMETRO, sem default inventado (task 056).** Enquanto o Samyr não
disser o valor, o clone fica ARMADO e PARADO — e diz isso, em vez de estrear com um
número chutado (não é o aporte de 70k dele). Configurado via
:func:`configurar_capital` (ou o env ``CLONE_ERICK_CAPITAL``), configurar (re)arma a
baseline: a ativação recomeça a história ali.

**A REGRA INEGOCIÁVEL do preço.** O preço de entrada do clone é o preço REAL do
instante em que NÓS detectamos a mudança — via :func:`live_price.fetch_live_price`
—, NUNCA o ``precoMedio`` dele, NUNCA o preço do dia em que ele entrou. O
``precoMedio`` dele é gravado no ledger SÓ como auditoria — a prova de que NÃO é ele
que entra na conta. Teste-dente em ``test_webui_clone_erick``.

**Replica o PESO, não a quantidade.** Capital diferente: a quantidade dele não
transporta. O clone mira o ``peso_agora`` (pct do capital DEPOIS da mudança)
aplicado ao capital CONFIGURADO. O retorno em % é invariante à escala do capital.

**A DEFASAGEM é gravada em cada operação, na cadência REAL.** A detecção roda a cada
HORA (task 053 — não mais o diário 09:15), então o atraso máximo entre o que ele faz
e o que a gente vê caiu de ~24h pra ~1h. Cada op grava o instante EXATO da nossa
detecção (``ts``, com hora) contra a data (grosseira) da mudança dele; nenhum piso de
24h/09:15 é assumido. É esse campo que separa "seguir o Erick" de "ter feito o que o
Erick fez".

O saldo é sempre DERIVADO do ledger append-only (disciplina da carteira virtual do
scan, DA-155): :func:`replay` relê as operações do zero. O resumo declara os estados
(DA-157): armado / sem mudança ainda / detectou sem cotação / tem número —
"amostra insuficiente" nunca é "0%" disfarçado, e "armado" nunca é "rendeu 0".
"""

from __future__ import annotations

import contextlib
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tradingagents.webui import alertas_tg as A

# Env pelo qual o serviço pode injetar o capital (o Samyr define o valor). NÃO há
# default: capital ausente = clone armado e parado.
_CAPITAL_ENV = "CLONE_ERICK_CAPITAL"

_MESES = {"jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
          "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12}


def _base_dir(dir: str | os.PathLike | None = None) -> Path:
    return Path(dir) if dir else Path(os.environ.get("CLONE_ERICK_DIR")
                or (Path.home() / ".tradingagents" / "clone-erick"))


def ledger_path(dir: str | os.PathLike | None = None) -> Path:
    return _base_dir(dir) / "operacoes.jsonl"


def _estado_path(dir: str | os.PathLike | None = None) -> Path:
    return _base_dir(dir) / "estado.json"


# ── estado do clone: capital + baseline (a carteira dele na ativação) ───────────
def _carrega_estado(dir: str | os.PathLike | None = None) -> dict[str, Any]:
    p = _estado_path(dir)
    if not p.exists():
        return {}
    with contextlib.suppress(OSError, ValueError):
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    return {}


def _grava_estado(est: dict[str, Any], dir: str | os.PathLike | None = None) -> None:
    p = _estado_path(dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(est, ensure_ascii=False), encoding="utf-8")


def _capital_de(est: dict[str, Any]) -> float | None:
    """O capital CONFIGURADO — do estado, ou do env como semente; nunca um default.
    Valor não-positivo ou ilegível é tratado como ausente (armado)."""
    cru = est.get("capital")
    if cru is None:
        cru = os.environ.get(_CAPITAL_ENV)
    try:
        v = float(cru)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def configurar_capital(valor: float, *, dir: str | os.PathLike | None = None) -> float:
    """Define o capital do clone e (RE)ARMA a baseline — ligar o clone recomeça a
    história ali: a próxima leitura vira a baseline (zero operação) e só o que mudar
    DEPOIS conta. ``valor`` tem de ser positivo."""
    v = float(valor)
    if v <= 0:
        raise ValueError("capital do clone tem de ser positivo")
    est = _carrega_estado(dir)
    est.update({"capital": v, "baseline": None,
                "ativado_em": datetime.now(timezone.utc).isoformat()})
    _grava_estado(est, dir)
    return v


def estado(dir: str | os.PathLike | None = None) -> dict[str, Any]:
    """Resumo do estado operacional do clone, sem inventar número."""
    est = _carrega_estado(dir)
    cap = _capital_de(est)
    return {
        "estado": "ativo" if cap is not None else "armado",
        "capital": cap,
        "ativado_em": est.get("ativado_em"),
        "baseline_definida": bool(est.get("baseline")),
    }


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
    contra um mês inteiro não é a mesma coisa que contra um dia. (A nossa detecção,
    por outro lado, é EXATA — a cadência horária dá o instante, gravado em ``ts``.)
    """
    s = (txt or "").strip().lower()
    if not s or s == "-":
        return None, "ausente"
    with contextlib.suppress(Exception):
        if s.count("/") == 2 and s.replace("/", "").isdigit():
            d, m, a = (int(x) for x in s.split("/"))
            return date(a, m, d), "dia"
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


PrecoFn = Callable[[str, "str | None"], "dict[str, Any] | None"]


# ── uma mudança detectada → uma operação de clone ───────────────────────────────
def op_de_mudanca(m: dict[str, Any], atual: dict[str, Any] | None,
                  preco_info: dict[str, Any] | None) -> dict[str, Any]:
    """Constrói a operação de paper a partir de UMA mudança detectada + o preço REAL.

    Função pura (sem I/O): é aqui que a regra "o preço é o REAL da detecção, nunca o
    ``precoMedio`` dele" vive, e é aqui que o teste-dente morde. ``preco_info`` é o
    retorno de :func:`live_price.fetch_live_price` (ou ``None``).
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

    # Data da mudança DELE: pra posição nova, a `entrada` dela é o sinal mais
    # específico; pros ajustes (aumentou/reduziu/saiu) não há data por-operação,
    # então o `atualizado` do snapshot é o melhor proxy do "quando ele mexeu".
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
        # ── DEFASAGEM (o que separa seguir de ter feito) — detecção EXATA (ts,
        # cadência horária) contra a data grosseira dele. ──
        "dele_entrada": dele.get("entrada"),
        "dele_atualizado": atualizado,
        "defasagem_base": base_nome,
        "defasagem_granularidade": granularidade,
        "defasagem_dias": _defasagem(d_det, d_dele),
        # ── AUDITORIA: o preço DELE fica gravado SÓ pra provar que não é ele que
        # entra na conta. Se um dia alguém trocar `preco` por isto, o teste-dente
        # quebra. ──
        "dele_precoMedio": dele.get("precoMedio"),
        "qtd_antes_dele": m.get("qtd_antes"),
        "qtd_agora_dele": m.get("qtd_agora"),
    }


# ── gravação (append-only) ──────────────────────────────────────────────────────
def registrar(mudou: list[dict[str, Any]], atual: dict[str, Any] | None, *,
              preco_fn: PrecoFn | None = None,
              path: str | os.PathLike | None = None) -> list[dict[str, Any]]:
    """Transforma cada mudança detectada numa operação e ANEXA ao ledger (baixo
    nível — não decide ativação; quem gatilha é :func:`observar`).

    O caixa nunca vira operação (não é posição — é o residual de toda compra/venda).
    ``preco_fn(ticker, classe)`` é injetável pra o teste cravar um preço REAL
    diferente do ``precoMedio`` dele; o default busca a cotação viva.
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


# ── o gatilho: observar uma leitura e clonar o que mudou DESDE a ativação ────────
def observar(atual: dict[str, Any] | None, *, preco_fn: PrecoFn | None = None,
             dir: str | os.PathLike | None = None) -> dict[str, Any]:
    """Consome UMA leitura da carteira dele e devolve o que o clone fez com ela.

    Três caminhos, nesta ordem:
      • **ARMADO** (sem capital): não faz nada — nem baseline. O clone só liga
        quando o Samyr definir o capital; até lá é "armado, aguardando capital".
      • **PRIMEIRA leitura pós-ativação** (capital definido, sem baseline): grava a
        baseline e NÃO opera — é aqui que "nasce vazia, não replica o que ele já
        tem" acontece, reusando a semântica do ``mudancas`` (primeira leitura = []).
      • **Leituras seguintes**: ``mudancas(baseline, atual)`` → operações no ledger,
        e a baseline avança pra ``atual``.
    """
    if not atual or atual.get("degradado"):
        # leitura ruim/requentada não move o clone (mesma disciplina do alerta).
        return {"estado": estado(dir)["estado"], "ops": [],
                "motivo": "leitura ausente ou degradada"}
    est = _carrega_estado(dir)
    cap = _capital_de(est)
    if cap is None:
        return {"estado": "armado", "ops": [],
                "motivo": "clone armado, aguardando o capital que o Samyr vai "
                          "definir — nenhuma operação até lá"}
    carteira_atual = atual.get("carteira")
    if not est.get("baseline"):
        est["baseline"] = carteira_atual
        est.setdefault("ativado_em", datetime.now(timezone.utc).isoformat())
        _grava_estado(est, dir)
        return {"estado": "ativo", "ops": [],
                "nota": "baseline da ativação gravada — a carteira nasce vazia, as "
                        "posições atuais dele NÃO são replicadas"}
    mudou = A.mudancas({"carteira": est.get("baseline")}, atual)
    ops = registrar(mudou, atual, preco_fn=preco_fn, path=ledger_path(dir))
    est["baseline"] = carteira_atual
    _grava_estado(est, dir)
    return {"estado": "ativo", "ops": ops}


# ── saldo DERIVADO do ledger (replay) ───────────────────────────────────────────
def replay(ops: list[dict[str, Any]], capital: float,
           precos_atuais: dict[str, float] | None = None) -> dict[str, Any]:
    """Relê o ledger do zero e devolve o estado da carteira-espelho, dado o
    ``capital`` CONFIGURADO (sem default: quem chama tem de saber o valor).

    Dimensiona cada posição pelo PESO ALVO aplicado ao capital e realiza PnL sempre
    contra os NOSSOS preços reais de entrada — nunca contra o preço dele.
    ``precos_atuais`` (ticker→preço) marca as abertas a mercado; ausente, marca a
    custo (0 de não-realizado, honesto: sem cotação não se afirma lucro).
    """
    precos_atuais = precos_atuais or {}
    cap = float(capital)
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
        "capital": cap,
        "cash": cash,
        "realizado": realizado,
        "nao_realizado": nao_realizado,
        "equity": equity,
        "retorno_pct": (equity - cap) / cap if cap else 0.0,
        "n_ops_incluidas": n_incl,
        "posicoes_abertas": abertos,
    }


# ── resumo com os estados honestos (DA-157 + o estado ARMADO da task 056) ───────
def resumo(ops: list[dict[str, Any]], capital: float | None,
           precos_atuais: dict[str, float] | None = None) -> dict[str, Any]:
    """Quanto o clone rendeu (do zero, com os eventos reais) — ou por que ainda não
    dá pra dizer. Nunca "0%" travestido de "sem dado" (DA-157), e "armado" nunca é
    "rendeu 0" (task 056)."""
    if capital is None:
        return {"estado": "armado",
                "motivo": "clone armado, aguardando o capital do Samyr — sem "
                          "capital não há carteira nem retorno a reportar"}
    if not ops:
        return {"estado": "amostra_insuficiente",
                "motivo": "clone ativo, mas nenhuma mudança detectada desde a "
                          "ativação — o track record começa do zero e cresce com "
                          "os eventos reais"}
    incluidas = [o for o in ops if o.get("incluido") and o.get("preco")]
    if not incluidas:
        return {"estado": "amostra_insuficiente",
                "motivo": f"{len(ops)} mudança(s) detectada(s), mas nenhuma com "
                          "cotação real no instante da detecção — sem preço nosso "
                          "não se afirma retorno"}
    r = replay(ops, capital, precos_atuais)
    return {"estado": "ok", **r,
            "n_ops_total": len(ops),
            "n_ops_sem_preco": len(ops) - len(incluidas)}

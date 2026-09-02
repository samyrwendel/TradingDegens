#!/usr/bin/env python3
"""Envia os dois alertas do Telegram — e OBEDECE a separação de destino (DA-149).

DESLIGADO POR PADRÃO, os dois. Nada sai daqui sem uma variável de ambiente dizendo
para onde — e, no caso da carteira, o destino ainda passa pela recusa mecânica de
grupo (:func:`alertas_tg.destino_valido`). Um mecanismo de distribuição que nasce
ligado é um mecanismo que estreia sem ninguém ter visto a amostra.

    ALERTA_CARTEIRA_CHAT   DM do dono (id POSITIVO). Grupo é recusado.
    ALERTA_SINAIS_CHAT     DM ou grupo (id negativo). Vazio = desligado.
    ALERTA_SINAIS_TOPICO   opcional: id do tópico dentro do grupo.

Uso:  tg_alertas.py carteira      (a cada 1h — task 20260902-053, ordem do Samyr)
      tg_alertas.py sinais        (após a passada da agenda, por candle fechado)

O envio reusa o `tg-outbox-send.sh` que já existe: uma fila só, um lugar só que
fala com o Telegram.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradingagents.webui import alertas_tg as A  # noqa: E402

_ESTADO = Path.home() / ".tradingagents" / "cache"
_ULTIMA_CARTEIRA = _ESTADO / "erick-carteira-alertada.json"
_FALHA_ALERTADA = _ESTADO / "erick-carteira-falha-alertada.json"
_FALHA_LIMITE_H = 24.0
_SINAIS_ENVIADOS = _ESTADO / "sinais-enviados.jsonl"
_SCAN_SALVO = Path.home() / ".tradingagents" / "logs" / "webui" / "last_scan.json"
_ENVIAR = Path.home() / "claude-tg-tmux" / "scripts" / "tg-outbox-send.sh"


def _envia(chat: str, texto: str, topico: str = "") -> bool:
    if not texto.strip():
        return False
    cmd = ["bash", str(_ENVIAR), str(chat)]
    if topico:
        # A flag vem ANTES do texto e é consumida pelo sender (DA-149) — se ela
        # virasse argumento de texto, a mensagem enviada seria a própria flag.
        # Sem tópico, a mensagem cai no "Geral" do grupo, que é o default certo.
        cmd.append(f"--topic={topico}")
    cmd.append("-")           # o texto vem por stdin, sempre
    try:
        subprocess.run(cmd, input=texto, text=True, check=True, timeout=30)
        return True
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"[tg-alertas] envio falhou: {type(exc).__name__}", file=sys.stderr)
        return False


def _carrega(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _grava(p: Path, dados) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")


def carteira() -> int:
    chat = (os.environ.get("ALERTA_CARTEIRA_CHAT") or "").strip()
    if not chat:
        return 0                                  # desligado: silêncio, não erro
    ok, motivo = A.destino_valido(A.FONTE_CARTEIRA, chat)
    if not ok:
        # RECUSA BARULHENTA, ao contrário do "desligado": alguém configurou um
        # destino proibido, e um silêncio aqui deixaria a violação passar por
        # "não chegou alerta hoje".
        print(f"[tg-alertas] {motivo}", file=sys.stderr)
        return 2
    from tradingagents.dataflows import erick_carteira as ec

    atual = ec.carteira()
    if atual is None:
        return 0                                  # sem credencial: feature não existe

    if atual.get("degradado"):
        # Leitura ao vivo falhou e isto é o último dado bom, requentado. Não é
        # "mudou" (é o MESMO snapshot de sempre) — e um alerta a cada hora aqui
        # seria justamente o ruído que o silêncio-por-padrão existe pra evitar.
        # Só vira alerta se a fonte ficar tempo REAL sem responder (>24h): aí é
        # problema de verdade (site fora do ar, credencial vencida), não hiato
        # normal entre duas leituras.
        idade = atual.get("idade_horas") or 0.0
        if idade > _FALHA_LIMITE_H:
            lido_em = atual.get("lido_em")
            ja_alertado = (_carrega(_FALHA_ALERTADA) or {}).get("lido_em") == lido_em
            if not ja_alertado:
                aviso = (f"⚠️ CARTEIRA DO ERICK sem leitura nova há {idade:.0f}h — "
                         f"a fonte pode estar fora do ar ou a credencial venceu.")
                if _envia(chat, aviso):
                    _grava(_FALHA_ALERTADA, {"lido_em": lido_em})
        return 0
    if _FALHA_ALERTADA.exists():
        # Voltou a ler ao vivo: o próximo episódio de falha começa a contar do
        # zero, não continua um relógio de uma falha antiga já superada.
        with contextlib.suppress(OSError):
            _FALHA_ALERTADA.unlink()

    anterior = _carrega(_ULTIMA_CARTEIRA)
    mudou = A.mudancas(anterior, atual)
    # CLONE EM PAPER (tasks 20260902-055/056): a carteira-espelho OBSERVA a mesma
    # leitura viva. Ela mantém a PRÓPRIA baseline (nasce vazia na ativação, só segue
    # o futuro) e fica ARMADA até o Samyr definir o capital — por isso recebe `atual`
    # e decide sozinha, não o `mudou` daqui. Defensivo: o alerta é o produto; o clone
    # é registrador passivo e uma falha dele jamais pode derrubar o alerta.
    from tradingagents.webui import clone_erick as _clone

    with contextlib.suppress(Exception):
        _clone.observar(atual)
    # O ESTADO É GRAVADO SEMPRE, mesmo sem mudança: senão a primeira leitura de
    # amanhã compararia contra a de anteontem e reanunciaria o que já foi dito.
    _grava(_ULTIMA_CARTEIRA, {"carteira": atual.get("carteira")})
    texto = A.formata_carteira(mudou, atual, anterior)
    if not texto:
        return 0                                  # nada mudou → silêncio
    return 0 if _envia(chat, texto) else 1


def _ja_enviados() -> set[str]:
    if not _SINAIS_ENVIADOS.exists():
        return set()
    out = set()
    for linha in _SINAIS_ENVIADOS.read_text(encoding="utf-8").splitlines():
        try:
            out.add(json.loads(linha)["chave"])
        except (ValueError, KeyError):
            continue
    return out


def sinais() -> int:
    chat = (os.environ.get("ALERTA_SINAIS_CHAT") or "").strip()
    if not chat:
        return 0
    ok, motivo = A.destino_valido(A.FONTE_SINAIS, chat)
    if not ok:
        print(f"[tg-alertas] {motivo}", file=sys.stderr)
        return 2
    scan = _carrega(_SCAN_SALVO)
    if not scan:
        return 0
    novos = [s for s in A.sinais_dignos(scan) if s["chave"] not in _ja_enviados()]
    if not novos:
        return 0
    texto = A.formata_sinais(novos, quando=str(scan.get("gerado_em") or "")[:16])
    if not _envia(chat, texto, os.environ.get("ALERTA_SINAIS_TOPICO", "").strip()):
        return 1
    # TODO SINAL ENVIADO FICA REGISTRADO — é o que permite medir depois quantos
    # viraram acerto. Sem o registro, "os sinais funcionam?" volta a ser opinião.
    _SINAIS_ENVIADOS.parent.mkdir(parents=True, exist_ok=True)
    with _SINAIS_ENVIADOS.open("a", encoding="utf-8") as f:
        for s in novos:
            f.write(json.dumps({**s, "enviado_em": time.time(), "chat": chat},
                               ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    modo = sys.argv[1] if len(sys.argv) > 1 else ""
    if modo == "carteira":
        raise SystemExit(carteira())
    if modo == "sinais":
        raise SystemExit(sinais())
    print(__doc__)
    raise SystemExit(64)

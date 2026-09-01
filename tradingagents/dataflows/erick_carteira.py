"""A carteira REAL do Erick Sekiama, lida do site dele — SÓ PRO DONO (DA-148).

**Por que esta fonte é diferente de todas as outras do produto.** Todo o resto que o
TradingDegens lê é público (yfinance, exchange, FRED): qualquer visitante poderia
buscar o mesmo dado. Isto não — é conteúdo de assinatura paga, e a própria tela de
login do site diz "Acesso exclusivo para alunos". O Samyr é aluno e pode consumir o
que comprou; o visitante do produto dele, não. Por isso a rota que serve este módulo
é a única do produto que vive atrás do portão de DONO **sem alternativa de BYOK**:
trazer chave própria de LLM não compra assinatura de terceiro.

**A credencial é dado pessoal e não mora aqui.** O e-mail de compra vem de
``ERICK_CARTEIRA_EMAIL`` no ambiente do serviço (mesma disciplina do
``TRADINGDEGENS_OWNER_TOKEN``). Sem ele configurado a feature simplesmente NÃO
EXISTE — ``carteira()`` devolve ``None`` e a tela não mostra nada. Falha silenciosa
e limpa: um stack trace aqui exporia o endereço de alguém.

**Cadência civilizada.** É servidor de outra pessoa, e o dado é atualizado À MÃO
(o payload traz um campo ``atualizado`` com a data). Uma leitura por dia basta;
entre uma e outra o cache local responde. Bater de minuto em minuto seria abusar de
infraestrutura alheia pra reler um número que não mudou.

**Dado velho nunca se disfarça de novo** (mesma disciplina da DA-114): o cache
carrega o instante da leitura, e quem exibe recebe ``lido_em`` + ``idade_horas``
pra dizer na tela de quando é. Falha de acesso NÃO apaga a tela — devolve o último
lido, marcado como ``degradado``, porque um painel vazio se leria como "ele zerou a
carteira", que é uma afirmação, não uma ausência de dado.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BASE = "https://ericksekiama.com.br/carteira"
_LOGIN = f"{BASE}/login.php"
_API = f"{BASE}/api.php?acao=ler"
_HISTORICO = f"{BASE}/historico.json"

# Uma leitura por dia. O número não é conservadorismo genérico: o campo
# `atualizado` do payload é preenchido à mão pelo autor, e reler mais que isso é
# gastar servidor alheio pra receber o mesmo byte.
_TTL_S = 24 * 3600
_TIMEOUT_S = 12

_CACHE = Path.home() / ".tradingagents" / "cache" / "erick-carteira.json"


def _email() -> str:
    return (os.environ.get("ERICK_CARTEIRA_EMAIL") or "").strip()


def configurado() -> bool:
    """A feature existe nesta instância? Sem credencial, ela não existe."""
    return bool(_email())


def _ler_cache() -> dict[str, Any] | None:
    try:
        return json.loads(_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _grava_cache(payload: dict[str, Any]) -> None:
    try:
        _CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        # 0600: é conteúdo de assinatura de terceiro em disco compartilhado.
        os.chmod(tmp, 0o600)
        tmp.replace(_CACHE)
    except OSError as exc:
        logger.info("cache da carteira do Erick não gravado: %s", exc)


def _busca() -> dict[str, Any]:
    """A leitura crua: login por e-mail → cookie → API + histórico público.

    Só é chamada quando o cache venceu. Levanta em qualquer falha — quem chama
    decide degradar (e degrada pro último lido, nunca pra tela vazia).
    """
    import requests

    s = requests.Session()
    s.headers["User-Agent"] = "TradingDegens/1.0 (leitura da carteira do aluno)"
    # O login é por e-mail, sem senha — é assim que o site funciona. O e-mail NUNCA
    # é logado: `logger` deste módulo não recebe a credencial em lugar nenhum.
    r = s.post(_LOGIN, data={"email": _email()}, timeout=_TIMEOUT_S, allow_redirects=False)
    if r.status_code not in (200, 302):
        raise RuntimeError(f"login recusado ({r.status_code})")
    r = s.get(_API, timeout=_TIMEOUT_S)
    r.raise_for_status()
    dados = r.json()
    if not isinstance(dados, dict) or not dados.get("ativos"):
        raise RuntimeError("payload sem 'ativos' — formato do site mudou?")
    # O histórico é PÚBLICO e é um pedido separado: se ele falhar, a carteira ainda
    # vale (a curva é enriquecimento, não o dado principal).
    try:
        h = s.get(_HISTORICO, timeout=_TIMEOUT_S)
        h.raise_for_status()
        historico = h.json()
    except Exception as exc:  # noqa: BLE001
        logger.info("histórico de patrimônio indisponível: %s", exc)
        historico = None
    return {"carteira": dados, "historico": historico, "lido_em": time.time()}


def carteira(*, force: bool = False) -> dict[str, Any] | None:
    """O payload pronto pra tela, ou ``None`` quando a feature não existe aqui.

    ``degradado`` diz que a leitura de agora falhou e o que se está vendo é o último
    lido — a tela mostra a data e o leitor decide se ainda serve.
    """
    if not configurado():
        return None
    cache = _ler_cache()
    fresco = bool(cache) and (time.time() - float(cache.get("lido_em") or 0)) < _TTL_S
    if cache and fresco and not force:
        return _com_idade(cache, degradado=False)
    try:
        novo = _busca()
    except Exception as exc:  # noqa: BLE001 — fonte de terceiro nunca derruba a tela
        logger.info("leitura da carteira do Erick falhou: %s", type(exc).__name__)
        if cache:
            return _com_idade(cache, degradado=True)
        return None
    _grava_cache(novo)
    return _com_idade(novo, degradado=False)


def _com_idade(payload: dict[str, Any], *, degradado: bool) -> dict[str, Any]:
    lido = float(payload.get("lido_em") or 0)
    idade_h = max(0.0, (time.time() - lido) / 3600) if lido else None
    return {
        "carteira": payload.get("carteira"),
        "historico": payload.get("historico"),
        "lido_em": lido or None,
        "idade_horas": round(idade_h, 1) if idade_h is not None else None,
        "degradado": degradado,
        "fonte": BASE,
    }


def composicao(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Ativos com a PARTICIPAÇÃO calculada — inclusive o caixa.

    A participação sai do valor a PREÇO MÉDIO, não do preço de hoje: é a foto que o
    próprio autor publica, e misturar a cotação de agora aqui produziria um número
    que não bate com nenhum dos dois. A oscilação contra o preço de agora é outra
    coluna, e a tela a busca separado (`live_prices`).
    """
    ativos = ((payload or {}).get("carteira") or {}).get("ativos") or []
    linhas = []
    total = 0.0
    for a in ativos:
        try:
            qtd = float(a.get("qtd") or 0)
            pm = float(a.get("precoMedio") or 0)
        except (TypeError, ValueError):
            qtd = pm = 0.0
        valor = qtd * pm
        total += valor
        linhas.append({**a, "valor": valor})
    for L in linhas:
        L["participacao"] = (L["valor"] / total) if total else None
    return linhas

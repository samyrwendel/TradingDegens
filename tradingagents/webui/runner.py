"""Drives the TradingAgents engine for the web UI — one analysis per run.

This module owns no analysis logic. It builds a
:class:`~tradingagents.graph.trading_graph.TradingAgentsGraph`, attaches the
usage + progress callbacks, calls ``graph.propagate(...)`` on a worker thread,
and exposes a pollable status snapshot (progress, live cost, and — when done —
the extracted result). Each run gets its own graph instance so its token/cost
tracking and progress are isolated even when two people on the Tailscale network
run analyses at once.
"""

from __future__ import annotations

import copy
import logging
import os
import threading
import time
import traceback
import uuid
from datetime import datetime
from typing import Any

from langchain_core.callbacks import UsageMetadataCallbackHandler

from tradingagents.agents.utils.rating import RATING_PT
from tradingagents.dataflows import data_notices
from tradingagents.llm_clients.model_format import id_format_meta, normalize_model_id
from tradingagents.webui import ask as ask_module, timeutil
from tradingagents.webui.compare import (
    build_column,
    confront_pair_valid,
    detect_method,
    deterministic_meta,
)
from tradingagents.webui.contradiction_checker import (
    check_contradictions,
    format_verdict_caveat,
)
from tradingagents.webui.degraded import normalize_degraded, normalize_result
from tradingagents.webui.errors import (
    NEED_KEY_CODE,
    NEED_KEY_MESSAGE,
    humanize_provider_error,
)
from tradingagents.webui.pricing import cost_breakdown
from tradingagents.webui.progress import (
    CancelCallbackHandler,
    ProgressCallbackHandler,
    ProgressTracker,
    RunCancelled,
    ThinkingCallbackHandler,
    ThinkingTracker,
    stage_for_node,
)
from tradingagents.webui.report_sanitizer import sanitize_result
from tradingagents.webui.resume_store import ActiveRunStore
from tradingagents.webui.scanner import (
    ScanLog,
    _setup_da_entrada,
    scan_verdicts,
    scan_watchlist,
)
from tradingagents.webui.store import HistoryStore, WatchlistStore

# Default analyst order; crypto drops fundamentals (no balance sheet for a coin).
_ANALYST_ORDER = ("market", "social", "news", "fundamentals")

# The chart's timeframe selector, ordered widest→narrowest (semanal · diário · 4h
# · 1h · 15m). The weekly frame is resampled in memory from the daily series; the
# intraday ladder is a real keyless candle for BOTH asset types now — crypto from
# the exchange (native), equity from yfinance (15m/1h native, 4h resampled per
# session). A frame with no source for a given symbol/date degrades honestly
# on demand ("intradiário indisponível") rather than inventing a bar, so the
# buttons are operable for every asset. Daily (``_DEFAULT_TIMEFRAME``) stays the
# frame every analysis is first computed on — the selector re-derives the rest.
_CRYPTO_TIMEFRAMES = ("1w", "1d", "4h", "1h", "15m")
_STOCK_TIMEFRAMES = ("1w", "1d", "4h", "1h", "15m")
_DEFAULT_TIMEFRAME = "1d"
# TTL do cache de preço LIVE da watchlist: "vivo" o bastante sem martelar a fonte.
_PRICE_TTL = 45.0

# Janela de reúso de dia-corrente (herda DA-058): uma análise de HOJE só é
# reaproveitada enquanto o dado que ela leu ainda vale — passado o TTL, a barra
# live pode ter refrescado (candle parcial vira final) e o julgamento fica
# obsoleto, então recomputa. Data histórica é imutável e reusa sempre. O default
# espelha ``OHLCV_CACHE_TTL_SECONDS`` da DA-058 (resolvido em runtime, fail-soft).
_SAME_DAY_REUSE_TTL_DEFAULT = 900.0
# Quantos registros do histórico varrer procurando um reúso íntegro.
_REUSE_SCAN_LIMIT = 50

# Janela do memo do scan: tempo em que um segundo pedido reaproveita a varredura
# recém-terminada em vez de refazê-la. Curto de propósito — "Escanear" tem que
# continuar parecendo fresco; isto só absorve o reclique e o pedido concorrente.
_SCAN_MEMO_TTL = 5.0

# Teto da pergunta do Q&A ancorado (/api/ask): corta enrolação, segura o custo.
_MAX_QUESTION_CHARS = 500

# "Testar modelo" (/api/test-model): prompt mínimo (poucos tokens, custo desprezível)
# só pra confirmar que o modelo responde e medir a latência — NÃO é análise. E teto
# do trecho da resposta exibido, pra não jogar um texto grande na UI de config.
_MODEL_TEST_PROMPT = "Responda apenas: ok"
_MODEL_TEST_SAMPLE_CHARS = 120

# Provedores que usam a credencial/assinatura do DONO (não uma chave BYOK do usuário):
# ``claude-cli`` roda pela assinatura Claude via proxy local ($0/token). São OWNER-ONLY
# — nem um BYOK falso destrava; só o dono logado (o servidor marca allow_server_key).
# Defesa em profundidade: mesmo que a UI mostre, o servidor barra aqui.
_OWNER_ONLY_PROVIDERS = frozenset({"claude-cli", "claude_cli", "claude-subscription"})
_OWNER_ONLY_MESSAGE = "Este provedor usa a assinatura do dono — entre como dono pra usá-lo."
_OWNER_ONLY_CODE = "owner_only"


def _owner_only_blocked(config: dict, overrides: dict) -> bool:
    """True quando um provedor owner-only é pedido sem autorização de dono.

    ``allow_server_key`` é ``True`` só pro dono logado (o servidor marca por
    requisição). Independe de haver ``llm_api_key`` (BYOK falso não destrava)."""
    provider = (config.get("llm_provider") or "").lower()
    return (
        provider in _OWNER_ONLY_PROVIDERS
        and (overrides or {}).get("allow_server_key") is not True
    )


# Rótulos pt-BR dos dois níveis de LLM (cross-provider, task 027).
_LEVEL_PT = {"deep": "Modelo Pesado", "quick": "Modelo Rápido"}


def levels_credential_error(config: dict, overrides: dict) -> tuple[str | None, str | None]:
    """Valida a credencial de CADA nível (RÁPIDO/PESADO) ANTES de rodar.

    Cross-provider (task 027): cada nível pode apontar pra um provedor diferente e
    cada um precisa da SUA credencial — um nível apontando pra provedor sem
    credencial vira erro claro ANTES de rodar (não cai na env, não roda pela metade).
    Retorna ``(code, message)`` do primeiro nível bloqueado, ou ``(None, None)``.

    Regra por nível: owner-only (claude-cli/assinatura) exige dono
    (``allow_server_key``); provedor com env-var de chave exige BYOK do provedor-base
    OU a env do servidor (só dono). Provedor sem chave (ollama/bedrock) passa.
    Complementa ``_owner_only_blocked`` (que só olha o ``llm_provider`` base) cobrindo
    o caso de um NÍVEL owner-only enquanto o outro não é.
    """
    from tradingagents.graph.trading_graph import resolve_level_specs
    from tradingagents.llm_clients.api_key_env import get_api_key_env

    specs = resolve_level_specs(config, config.get("llm_api_key"))
    allow_server_key = (overrides or {}).get("allow_server_key")

    # owner-only primeiro (mais restritivo): a assinatura é do dono — exige dono
    # explícito, mesma regra do ``_owner_only_blocked`` (mas por-nível, cobrindo o
    # caso de UM nível ser claude-cli enquanto o outro não).
    for lvl in ("deep", "quick"):
        if specs[lvl]["provider"] in _OWNER_ONLY_PROVIDERS and allow_server_key is not True:
            return _OWNER_ONLY_CODE, f"{_LEVEL_PT[lvl]}: {_OWNER_ONLY_MESSAGE}"

    # Checagem proativa "sem credencial ANTES de rodar" (env-key): só dispara pro
    # DONO (``allow_server_key is True``), que roda pela env do servidor — aí um nível
    # sem chave erra antes em vez de cair meio-rodado. O caminho público já é barrado
    # pelo gate de chave; o interno/confiável (flag ausente) cai na env como antes.
    if allow_server_key is True:
        for lvl in ("deep", "quick"):
            provider = specs[lvl]["provider"]
            if provider in _OWNER_ONLY_PROVIDERS:
                continue  # a auth é do proxy da assinatura, sem env-key
            env_var = get_api_key_env(provider)
            if not env_var:
                continue  # provedor sem chave (ollama/bedrock/openai_compatible keyless)
            has_byok = bool(specs[lvl]["api_key"])
            has_server = bool(os.environ.get(env_var))
            if not (has_byok or has_server):
                return (
                    NEED_KEY_CODE,
                    f"{_LEVEL_PT[lvl]} ({provider}): sem credencial para este nível — "
                    "configure a chave do provedor ou entre como dono.",
                )
    return None, None


logger = logging.getLogger(__name__)


def _clean_error(exc: Exception, provider: str, secret: str | None) -> str:
    """Erro cru de LLM → mensagem humana em pt-BR, SEM stack nem chave.

    O texto técnico (redigido da chave) vai só pro log do servidor; a UI recebe a
    frase acionável quando o erro é reconhecido (429 sem crédito, 401 chave
    inválida, rate limit, timeout), ou um fallback curto ``Tipo: msg`` (redigido)
    quando não é. Nunca inclui traceback nem a chave do usuário."""
    raw = _redact_secret(f"{type(exc).__name__}: {exc}", secret)
    human = humanize_provider_error(raw, provider)
    return human["message"] if human else raw


def _error_code(exc: Exception, provider: str, secret: str | None) -> str | None:
    """``code`` estável do erro (no_credit/invalid_key/rate_limit/unavailable) pra
    UI escolher o call-to-action, ou ``None`` se não reconhecido."""
    raw = _redact_secret(f"{type(exc).__name__}: {exc}", secret)
    human = humanize_provider_error(raw, provider)
    return human["code"] if human else None


# --- BYOK: chave de API por-usuário (provider/model/base_url/api_key por-run) ----
# A chave vive no navegador do usuário e chega por requisição; o servidor a usa só
# em memória pra montar o client daquela run e NUNCA grava/loga/persiste (ver
# _persist / store: o record é montado por campos nomeados, sem a config/chave). O
# fallback (sem chave do usuário) é a env do servidor. Sem estado global: cada run
# monta sua própria config efetiva; o TradingAgentsGraph tira a chave da config
# antes do set_config global, então dois usuários não misturam chave.

def _redact_secret(text: str, secret: str | None) -> str:
    """Troca toda ocorrência de ``secret`` em ``text`` por ``***`` (segurança BYOK).

    Aplicada a qualquer erro/trace ANTES de sair do processo, pra um SDK de
    provedor que ecoe a chave numa exceção jamais vazá-la num run record, no
    histórico ou numa resposta de API."""
    if not secret:
        return text or ""
    return (text or "").replace(secret, "***")


def _provider_default_models(provider: str) -> tuple[str | None, str | None]:
    """(deep, quick) padrão do provedor pelo catálogo de modelos.

    Usado quando o usuário troca de provedor mas deixa o modelo em branco: manter
    o modelo OpenAI do servidor numa chave Anthropic só daria 404. Retorna
    ``(None, None)`` pra provedores custom-only (ollama/openrouter/
    openai_compatible) — ali o usuário precisa nomear o modelo."""
    try:
        from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS
    except Exception:
        return None, None
    opts = MODEL_OPTIONS.get((provider or "").lower())
    if not isinstance(opts, dict):
        return None, None
    deep_list = opts.get("deep") or []
    quick_list = opts.get("quick") or []
    deep = deep_list[0][1] if deep_list else None
    quick = quick_list[0][1] if quick_list else None
    return deep, quick


def _provider_catalog_models(provider: str) -> list[dict[str, str]]:
    """Modelos do catálogo do provedor como ``[{id, name}]`` (rápido ∪ pesado,
    dedup, ordem estável). Alimenta o dropdown de modelo do BYOK DIRETO do catálogo
    curado — sem depender de um endpoint /models ao vivo. Essencial pra provedores
    SEM listagem (claude-cli assinatura) e como default instantâneo pros demais, pra
    trocar de provedor JÁ refletir os modelos daquele provedor (task 014). O sentinela
    ``custom`` (id livre do CLI) não vira opção de dropdown. Retorna [] pros custom-only
    (ollama/openrouter/openai_compatible) — ali o usuário nomeia/lista o modelo."""
    try:
        from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS
    except Exception:  # noqa: BLE001
        return []
    opts = MODEL_OPTIONS.get((provider or "").lower())
    if not isinstance(opts, dict):
        return []
    seen: dict[str, dict[str, str]] = {}
    for mode in ("quick", "deep"):
        for label, value in (opts.get(mode) or []):
            if value == "custom" or value in seen:
                continue
            # o label do catálogo é "Nome - descrição"; o nome curto é o antes do " - "
            name = label.split(" - ", 1)[0].strip() if label else value
            seen[value] = {"id": value, "name": name}
    return list(seen.values())


def apply_llm_overrides(base_config: dict[str, Any],
                        overrides: dict[str, Any] | None) -> dict[str, Any]:
    """Config efetiva = base_config + overrides BYOK da requisição (dict novo).

    Chaves reconhecidas (todas opcionais): ``provider``, ``deep_model``,
    ``quick_model``, ``base_url``, ``api_key``. Vazio/ausente cai no config do
    servidor (usuário sem chave roda na env do servidor — a chave que o grafo lê
    quando ``llm_api_key`` está ausente). Trocar de provedor sem nomear modelo
    puxa o padrão do catálogo daquele provedor, pra o modelo casar com a chave.
    O dict retornado nunca é persistido; a ``api_key`` fica só em memória."""
    config = dict(base_config)
    ov = overrides or {}
    provider = (ov.get("provider") or "").strip().lower()
    if provider:
        prev_provider = (config.get("llm_provider") or "").lower()
        config["llm_provider"] = provider
        if provider != prev_provider:
            deep, quick = _provider_default_models(provider)
            if deep:
                config["deep_think_llm"] = deep
            if quick:
                config["quick_think_llm"] = quick
    if ov.get("deep_model"):
        config["deep_think_llm"] = ov["deep_model"]
    if ov.get("quick_model"):
        config["quick_think_llm"] = ov["quick_model"]
    if ov.get("base_url"):
        config["backend_url"] = ov["base_url"]
    if ov.get("api_key"):
        # Consumida pelo TradingAgentsGraph (que a tira do config antes do global)
        # e pelo _answer_llm; jamais entra no record persistido.
        config["llm_api_key"] = ov["api_key"]

    # Modo AVANÇADO (task 027): provedor+modelo POR-NÍVEL, cross-provider. RÁPIDO
    # (quick) e PESADO (deep) podem rodar provedores diferentes (ex.: Rápido=openai
    # server-key, Pesado=claude-cli assinatura $0/token). O provedor-base vira o do
    # PESADO (a chave BYOK, se houver, pertence a ele; o outro nível resolve a sua).
    if ov.get("advanced"):
        dp = (ov.get("deep_provider") or "").strip().lower()
        qp = (ov.get("quick_provider") or "").strip().lower()
        if dp:
            config["deep_think_provider"] = dp
            config["llm_provider"] = dp
        if qp:
            config["quick_think_provider"] = qp
        # Modelos por-nível já lidos acima (deep_model/quick_model). Sem modelo
        # explícito num nível → puxa o padrão do catálogo daquele provedor pra casar.
        if dp:
            d_def, _ = _provider_default_models(dp)
            if not ov.get("deep_model") and d_def:
                config["deep_think_llm"] = d_def
        if qp:
            _, q_def = _provider_default_models(qp)
            if not ov.get("quick_model") and q_def:
                config["quick_think_llm"] = q_def
        # Endpoint POR NÍVEL (task 017): lido por resolve_level_specs
        # (``{nivel}_backend_url``), que já cai no ``backend_url`` base quando ausente.
        # Sem isso, o endpoint de um self-host num nível ia parar no client do outro.
        for okey, cfg_key in (("deep_base_url", "deep_backend_url"),
                              ("quick_base_url", "quick_backend_url")):
            if ov.get(okey):
                config[cfg_key] = ov[okey]

    # NORMALIZAÇÃO DE FORMATO (task 016): o id de modelo não é portável entre
    # provedores — OpenRouter usa ``vendor/modelo``, a API Anthropic (e a assinatura
    # claude-cli) só entende o id PURO. Um id salvo no formato do provedor ANTERIOR
    # chegava intacto até o client e virava 404 ("model: anthropic/claude-opus-5"
    # no claude-cli). Aqui, com o provedor de CADA nível já resolvido, o id vira o
    # formato daquele provedor. Rede de proteção final: vale pro simples, pro
    # por-nível, pra run E pro "Testar modelo" (ambos passam por aqui).
    # ``strict=False`` de propósito: só o FORMATO é corrigido. Quem reseta um modelo
    # de outra família é a troca de provedor na UI (que sabe que o id é resto do
    # provedor anterior); aqui um id fora do catálogo pode ser um fine-tune/deploy
    # próprio do usuário — trocá-lo pelo default seria ignorar a escolha dele.
    base_provider = (config.get("llm_provider") or "").strip().lower()
    for mode, prov_key, model_key in (("deep", "deep_think_provider", "deep_think_llm"),
                                      ("quick", "quick_think_provider", "quick_think_llm")):
        lvl_provider = (config.get(prov_key) or base_provider or "").strip().lower()
        current = config.get(model_key)
        if lvl_provider and current:
            config[model_key] = normalize_model_id(lvl_provider, str(current), mode,
                                                   strict=False)
    return config


def select_analysts_for_asset(asset_type: str, include_erick: bool = False) -> list[str]:
    """Analyst wire-keys to run for an asset type (crypto has no fundamentals).

    ``include_erick`` appends the on-demand Erick-method analyst at the end of the
    analyst chain (Modo Erick). Default off — the Padrão analysis is untouched.
    """
    if asset_type == "crypto":
        base = [a for a in _ANALYST_ORDER if a != "fundamentals"]
    else:
        base = list(_ANALYST_ORDER)
    if include_erick:
        base.append("erick")
    return base


def timeframes_for_asset(asset_type: str) -> list[str]:
    """Operable chart timeframes for an asset type — the full intraday ladder for
    both crypto and equity (each frame has a real keyless source; a symbol/date with
    no candle degrades honestly on demand). The single source of truth the UI
    selector and the ``/api/chart`` validation both read, so they never disagree."""
    return list(_CRYPTO_TIMEFRAMES if asset_type == "crypto" else _STOCK_TIMEFRAMES)


def _resolve_same_day_ttl() -> float:
    """Same-day reuse window in seconds — the DA-058 OHLCV cache TTL, so run-reuse
    and data-cache freshness agree by construction. Fail-soft to the default when
    the heavy dataflows module can't be imported (keeps unit tests decoupled)."""
    try:
        from tradingagents.dataflows.stockstats_utils import OHLCV_CACHE_TTL_SECONDS
        return float(OHLCV_CACHE_TTL_SECONDS)
    except Exception:  # noqa: BLE001
        return _SAME_DAY_REUSE_TTL_DEFAULT


def _pipeline_version() -> str:
    """Installed pipeline version for the audit footer (fail-soft → 'unknown')."""
    try:
        from importlib.metadata import version

        return version("tradingagents")
    except Exception:  # noqa: BLE001
        return "unknown"


# Reading AXES (item 8) — each module operates on a different axis/horizon, so a
# "semanal em alta + diário em baixa + reduzir + aguardar" spread is NOT a
# contradiction, it is layers. Declaring the axis on every parecer kills the false
# contradiction. Deterministic taxonomy: (eixo, horizonte) keyed by module.
_MODULE_AXES: dict[str, tuple[str, str]] = {
    "veredito": ("posição", "3-6 meses"),
    "juiz": ("posição", "3-6 meses"),
    "tecnico": ("estrutural", "swing (semanas)"),
    "erick": ("tático", "intradia–swing"),
    "trader": ("tático", "swing"),
}


def module_axes() -> dict[str, dict[str, str]]:
    """Per-module reading axis + horizon (deterministic; item 8)."""
    return {k: {"eixo": e, "horizonte": h} for k, (e, h) in _MODULE_AXES.items()}


def extract_result(final_state: dict[str, Any], signal: str) -> dict[str, Any]:
    """Pull the display fields out of a completed run's final state.

    The two theses (bull/bear) are the headline: the brief measures the debate
    text as more useful than the verdict itself, so they are surfaced whole and
    side by side. Every field defaults to empty string — a degraded source shows
    "indisponível" in the report text the engine already wrote; nothing is
    fabricated here.
    """
    debate = final_state.get("investment_debate_state") or {}
    risk = final_state.get("risk_debate_state") or {}
    return {
        "verdict": signal,
        # THE single canonical decision of the run (bug: four modules — técnico,
        # Erick, juiz, trader — each stated a "final" action, so an automated
        # consumer could not tell which one is binding). It is the risk/portfolio
        # decision that already becomes ``signal``, exposed here as the pt-BR enum
        # (COMPRAR/AUMENTAR/MANTER/REDUZIR/VENDER). The module texts are READINGS
        # that feed this; only this field (and the verdict badge) is the veredito.
        "final_decision": RATING_PT.get(signal, signal),
        # Sources that degraded — the UI NAMES each one and shows why. Normalized
        # here because the state can still carry the pre-fix free-text note when a
        # run is resumed from an older checkpoint (task 20260828-003).
        "degraded": normalize_degraded(final_state.get("degraded_sources")),
        "final_trade_decision": final_state.get("final_trade_decision", "") or "",
        "bull": debate.get("bull_history", "") or "",
        "bear": debate.get("bear_history", "") or "",
        "research_manager": debate.get("judge_decision", "") or "",
        "investment_plan": final_state.get("investment_plan", "") or "",
        "trader_plan": final_state.get("trader_investment_plan", "") or "",
        "risk_decision": risk.get("judge_decision", "") or "",
        "market_report": final_state.get("market_report", "") or "",
        "sentiment_report": final_state.get("sentiment_report", "") or "",
        "news_report": final_state.get("news_report", "") or "",
        "fundamentals_report": final_state.get("fundamentals_report", "") or "",
        # On-demand "Modo Erick": empty string unless the erick analyst ran.
        "erick_report": final_state.get("erick_report", "") or "",
        # Natureza da queda classificada (fonte única — o meta-juiz/UI leem o CAMPO,
        # não a prosa). {} num run Padrão ou quando a classificação ficou indisponível.
        "drop_nature": final_state.get("erick_drop_nature") or {},
        # Filled by the runner for crypto from the engine's deterministic
        # derivatives data path (named source, "unavailable" not fabricated).
        "derivatives_report": "",
        # Filled by the runner for every asset: candles + moving averages +
        # detected setup markers for the chart (deterministic, cached series).
        "price_chart": {},
        # Filled by the runner for every asset: operable levels beside the verdict
        # (price @ analysis, horizon, timeframe, buy/realize/pullback zones), all
        # from the same cached series — "sem nível definido", never a fake number.
        "actionable": {},
        # THE single frozen reference price (date-guarded daily close); the runner
        # fills it from ``actionable`` so cover/UI/consumers share one price.
        "as_of_price": None,
        # Audit footer (run_id + collection timestamp + pipeline version + models per
        # tier); the runner fills it on completion so a run is reproducible/auditable.
        "audit": {},
        # Reading axes per module (eixo+horizonte); filled by the runner (item 8).
        "axes": {},
        # Pre-publication contradiction findings (item 7); filled by the runner.
        "contradictions": [],
        # Inconsistências detectadas ANTES do juiz (task 016): os insumos batidos
        # contra as âncoras já na hora da decisão (o juiz recebeu os DADOS VERIFICADOS
        # e decidiu com eles). Quando não-vazio, o veredito sai carimbado.
        "pre_judge_findings": list(final_state.get("pre_judge_findings") or []),
        # Aviso curto ao lado do veredito quando os insumos tinham inconsistência na
        # hora da decisão (task 016); preenchido na finalização.
        "verdict_caveat": "",
    }


def fetch_derivatives_report(ticker: str, date: str) -> str:
    """Deterministic funding / OI / liquidations report straight from the vendor.

    The market analyst's prose can drop the source name; this pulls the same data
    the engine fetched (cached, so free) so the UI can always show funding, open
    interest and liquidations *with the source named* — and "unavailable" instead
    of a fabricated number when a feed is down. Fail-open: returns "" on error so
    a vendor hiccup never blocks the result.
    """
    try:
        from tradingagents.dataflows.interface import route_to_vendor
        return route_to_vendor("get_crypto_derivatives", ticker, date) or ""
    except Exception:
        return ""


def fetch_price_chart(ticker: str, date: str, timeframe: str = _DEFAULT_TIMEFRAME,
                      method: str = "padrao") -> dict[str, Any]:
    """Candle + moving-average + setup-marker payload for the UI chart.

    ``timeframe`` selects the frame (daily default, or ``"4h"``/``"1h"``/``"15m"``
    intraday for crypto — real keyless exchange candles). ``method`` picks the
    average family the setup MARKERS key on (Padrão MMS / Erick EMA 8/21) so the two
    confront columns draw different structures. Reuses the same cached, date-guarded
    series the detector runs on, so it is free and cannot see a future candle.
    Fail-open: returns an empty payload on any error so a chart hiccup never blocks
    the analysis result.
    """
    try:
        from tradingagents.dataflows.price_structure import build_price_chart
        return build_price_chart(ticker, date, timeframe=timeframe, method=method)
    except Exception:
        return {}


def fetch_actionable_plan(ticker: str, date: str, timeframe: str = _DEFAULT_TIMEFRAME,
                          method: str = "padrao") -> dict[str, Any]:
    """Operable levels for the verdict header (price @ analysis, horizon,
    timeframe, buy/realize/pullback zones).

    ``timeframe`` selects the frame (daily default, or an intraday frame for
    crypto). ``method`` picks the average family the recuo/zones key on (Padrão MMS
    / Erick EMA 8/21). Reuses the same cached, date-guarded series and the detector's
    own structure, so it is free and cannot see a future candle. Fail-open: returns
    ``{}`` on any error so this enrichment never blocks the analysis result.
    """
    try:
        from tradingagents.dataflows.price_structure import build_actionable_plan_dict
        return build_actionable_plan_dict(ticker, date, timeframe=timeframe, method=method)
    except Exception:
        return {}


# MÉTODOS ESTRUTURAIS ($0 de LLM): leem a série e devolvem níveis, sem agente
# nenhum. São métodos SEPARADOS, não flags um do outro — o 1-2-3 deste projeto e o
# 1-2-3 Storm usam a mesma numeração para pontos DIFERENTES (ver DA-081), e a única
# coisa que eles de fato compartilham é não custar nada.
_METODOS_ESTRUTURAIS = ("setup123", "storm123")


def fetch_storm_plan(ticker: str, date: str,
                     timeframe: str = _DEFAULT_TIMEFRAME) -> dict[str, Any]:
    """Plano do 1-2-3 STORM (Stormer) + filtro Éden — leitura estrutural, $0 de LLM.

    Setup PRÓPRIO, não uma variação do 1-2-3 deste módulo: o ponto 2 é o fundo (não
    o topo do repique), o stop fica abaixo dele e o alvo é a PROJEÇÃO DA AMPLITUDE
    dos 3 candles. Lê a mesma série cacheada e date-guarded, então é grátis e não
    enxerga candle futuro. Fail-open: ``{}`` em qualquer erro.
    """
    try:
        from tradingagents.dataflows.price_structure import build_storm_plan_dict
        return build_storm_plan_dict(ticker, date, timeframe=timeframe)
    except Exception:
        return {}


def fetch_symbol_search(term: str, limit: int = 8) -> list[dict[str, Any]]:
    """Autocomplete candidates for a name-or-ticker term (fail-open -> [])."""
    try:
        from tradingagents.dataflows.symbol_search import search_symbols
        return search_symbols(term, limit=limit)
    except Exception:
        return []


def fetch_symbol_names(symbols: list[str]) -> dict[str, str]:
    """Batch symbol -> display name for the history chips (fail-open -> {})."""
    try:
        from tradingagents.dataflows.symbol_search import resolve_names
        return resolve_names(symbols)
    except Exception:
        return {}


def resolve_ticker_query(term: str) -> str | None:
    """A typed name-or-ticker -> the symbol to analyse (fail-open).

    A plain ticker (``looks_like_symbol``) passes through with NO network call and is
    never hijacked; a name ("Microsoft"/"Bitcoin") is resolved to its symbol via
    Yahoo search. ``None`` when the term is already a symbol, or Yahoo is down.
    """
    try:
        from tradingagents.dataflows.symbol_search import (
            looks_like_symbol,
            resolve_query_to_symbol,
        )
        if not term or looks_like_symbol(term):
            return None
        return resolve_query_to_symbol(term)
    except Exception:
        return None


class _Run:
    """In-memory state for a single analysis run."""

    def __init__(self, run_id: str, ticker: str, date: str, asset_type: str,
                 selected_analysts: list[str], timeframe: str = _DEFAULT_TIMEFRAME,
                 overrides: dict[str, Any] | None = None):
        self.run_id = run_id
        self.ticker = ticker
        self.date = date
        self.asset_type = asset_type
        self.selected_analysts = selected_analysts
        # BYOK: overrides de LLM da requisição (provider/model/base_url/api_key).
        # SÓ em memória, nunca persistido — o record em _persist é montado por
        # campos nomeados, sem tocar nisto.
        self.overrides = overrides or {}
        # Reference frame the market analyst reads for THIS run (task 012). Stamped
        # on the verdict and used as the chart's opening frame.
        self.timeframe = timeframe or _DEFAULT_TIMEFRAME
        # Method label (padrao/erick), reliable from the analyst selection — feeds
        # the resume descriptor and the cross-run reuse key.
        self.method = "erick" if "erick" in (selected_analysts or []) else "padrao"
        self.status = "running"           # running | done | error | cancelled
        # PARAR/PAUSAR (task 026): o cancelamento é cooperativo — o CancelCallbackHandler
        # levanta RunCancelled quando este Event é setado, abortando o grafo no próximo
        # limite. ``pause_keep_resume`` diz ao worker se MANTÉM (Pausar) ou APAGA (Parar)
        # o descritor de retomada da 022.
        self.cancel_event = threading.Event()
        self.pause_keep_resume = False
        # Reúso honesto (DA-058): True quando ESTE run devolveu, íntegras, as etapas
        # de um run idêntico já concluído — sem re-rodar o pipeline (custo zero).
        self.reused = False
        self.reused_from: str | None = None
        # True quando este run está RETOMANDO um checkpoint interrompido (boot pós
        # restart): o worker resume do último nó concluído, não do zero.
        self.resuming = False
        self.error: str | None = None
        # code estável do erro (no_credit/invalid_key/rate_limit/unavailable) pra UI.
        self.error_code: str | None = None
        self.result: dict[str, Any] | None = None
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.finished_stamp: str | None = None  # Manaus ISO, set on completion
        self.usage_cb = UsageMetadataCallbackHandler()
        self.tracker = ProgressTracker(selected_analysts)
        # Raciocínio ao vivo (task 008): captura a saída de cada agente conforme sai,
        # pra o painel revelar o "pensamento" durante o run. Custo zero de LLM.
        # timeframe (task 009): alimenta o selo de tempo-gráfico por etapa (Mercado
        # ancora o timing no frame de referência da run quando ele é intradiário).
        self.thinking = ThinkingTracker(timeframe=self.timeframe)
        # Fallback transparente (task 027-fallback): registra as trocas AUTOMÁTICAS de
        # provedor por etapa (quando o topo falha por estado do provedor e o motor cai
        # pro próximo da cadeia sem parar). Preenchido em _execute; surfa por-etapa no
        # snapshot e no resultado. None até o worker montar o tracker daquele run.
        self._fallback_tracker = None

    def cost(self) -> dict[str, Any]:
        return cost_breakdown(self.usage_cb.usage_metadata)

    def snapshot(self) -> dict[str, Any]:
        elapsed = (self.finished_at or time.time()) - self.started_at
        return {
            "run_id": self.run_id,
            "ticker": self.ticker,
            "date": self.date,
            "asset_type": self.asset_type,
            # Método da run (padrao/erick/setup123/compare) — o MESMO campo que o
            # histórico persiste; o front lê de um lugar só.
            "method": self.method,
            "status": self.status,
            "error": self.error,
            "error_code": self.error_code,
            "verdict_timeframe": self.timeframe,
            # PARAR/PAUSAR (task 026): quando a run foi cancelada, diz se foi um PAUSAR
            # (retomável) ou um PARAR — a UI mostra a mensagem certa. False no resto.
            "paused": self.pause_keep_resume,
            # Honest reuse marker (DA-058): the whole analysis came back intact from
            # an identical prior run; the UI badges it "reaproveitado", cost is zero.
            "reused": self.reused,
            "reused_from": self.reused_from,
            "resuming": self.resuming,
            # Fallback transparente (task 027-fallback): trocas AUTOMÁTICAS de provedor
            # já ocorridas neste run, visíveis AO VIVO (a análise não parou; trocou e
            # seguiu). Vazio quando não houve desvio. Lido do tracker, thread-safe.
            "fallbacks": (self._fallback_tracker.snapshot()
                          if self._fallback_tracker is not None else []),
            "progress": self.tracker.snapshot(),
            "thinking": self.thinking.snapshot(),
            "cost": self.cost(),
            "elapsed": round(elapsed, 1),
            "finished_at": self.finished_stamp,
            "result": self.result,
        }


class _SimpleProgress:
    """Progresso do confronto como TRILHA de 3 etapas: Análise Padrão → Análise
    método Erick → Comparação (meta-juiz). Cada etapa tem estado próprio
    (``pending``/``running``/``done``/``reused``) para o front desenhar o stepper —
    o motor já roda as 3 em série, isto só EXPÕE o que rodou e o que veio do cache
    (um lado reaproveitado aparece ``reused``, sem fingir que rodou; DA-058).

    Expõe a mesma forma de ``snapshot()`` que :class:`ProgressTracker`, mais o campo
    ``compare_steps``, para fluir por ``status``/``active_runs`` sem mudança."""

    # (key, rótulo) na ordem em que o worker as executa.
    STEPS = (
        ("padrao", "Análise Padrão"),
        ("erick", "Análise método Erick"),
        ("meta", "Comparação (meta-juiz)"),
    )

    def __init__(self):
        self._phase = "Inicializando"
        self._label = "Preparando comparação…"
        self._pct = 0
        self._started = time.monotonic()
        self._state = {key: "pending" for key, _ in self.STEPS}

    def set(self, phase: str, label: str, pct: int) -> None:
        self._phase, self._label, self._pct = phase, label, pct

    def step(self, key: str, state: str) -> None:
        """Marca o estado de uma etapa (running/done/reused/pending)."""
        if key in self._state:
            self._state[key] = state

    def done(self) -> None:
        # qualquer etapa que ficou 'running' na conclusão vira 'done'
        for key, st in self._state.items():
            if st == "running":
                self._state[key] = "done"
        self.set("Concluído", "Comparação concluída", 100)

    def snapshot(self) -> dict[str, Any]:
        steps = [
            {"key": key, "label": label, "state": self._state[key]}
            for key, label in self.STEPS
        ]
        return {
            "phase": self._phase, "label": self._label, "percent": self._pct,
            "index": 0, "total": len(self.STEPS),
            "elapsed": round(time.monotonic() - self._started, 1),
            "plan": [], "reached": [],
            "compare_steps": steps,
        }


class _CompareRun:
    """In-memory state for a Padrão × Erick comparison run.

    Duck-types the pieces of :class:`_Run` that ``status``/``active_runs``/
    ``_running_summary`` read, so it lives in the same ``_runs`` table and reuses
    the polling + history plumbing. Its ``result`` carries a ``compare`` block the
    UI renders side by side.
    """

    def __init__(self, run_id, ticker, date, asset_type, timeframe,
                 overrides: dict[str, Any] | None = None):
        self.run_id = run_id
        self.ticker = ticker
        self.date = date
        self.asset_type = asset_type
        self.timeframe = timeframe
        # BYOK: overrides de LLM da requisição, propagados a cada lado do confronto.
        self.overrides = overrides or {}
        self.status = "running"
        self.error: str | None = None
        self.error_code: str | None = None
        self.result: dict[str, Any] | None = None
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.finished_stamp: str | None = None
        self.tracker = _SimpleProgress()

    def cost(self) -> dict[str, Any]:
        cols = (self.result or {}).get("compare") or {}
        total = 0.0
        for k in ("a", "b"):
            total += float(((cols.get(k) or {}).get("cost") or {}).get("usd") or 0)
        return {"usd": round(total, 6), "complete": True}

    def snapshot(self) -> dict[str, Any]:
        elapsed = (self.finished_at or time.time()) - self.started_at
        return {
            "run_id": self.run_id,
            "ticker": self.ticker,
            "date": self.date,
            "asset_type": self.asset_type,
            "status": self.status,
            "error": self.error,
            "error_code": self.error_code,
            "verdict_timeframe": self.timeframe,
            "compare": True,
            "progress": self.tracker.snapshot(),
            "cost": self.cost(),
            "elapsed": round(elapsed, 1),
            "finished_at": self.finished_stamp,
            "result": self.result,
        }


class AnalysisRunner:
    """Starts analyses and tracks their live status."""

    def __init__(self, base_config: dict[str, Any] | None = None,
                 store: HistoryStore | None = None,
                 graph_factory=None, meta_judge=None):
        # Imported lazily so unit tests can construct the runner (and exercise
        # the pure helpers) without importing the heavy engine / config.
        if base_config is None:
            from tradingagents.default_config import DEFAULT_CONFIG
            base_config = DEFAULT_CONFIG
        self.base_config = dict(base_config)
        if store is None:
            from pathlib import Path
            store = HistoryStore(Path(self.base_config["results_dir"]) / "webui")
        self.store = store
        # Watchlist MANUAL do scan (curada pelo dono; semeia do histórico na 1ª
        # leitura) + log append-only dos gatilhos flagrados (track record $0).
        self.watchlist_store = WatchlistStore(self.store.base, store)
        self.scan_log = ScanLog(self.store.base / "scans.jsonl")
        # Single-flight do scan: uma varredura por vez, com memo curtíssimo pra o
        # segundo pedido não re-varrer (ver :meth:`scan_portfolio`).
        self._scan_lock = threading.Lock()
        self._scan_memo: tuple[str, float, dict[str, Any]] | None = None
        # graph_factory(config, selected_analysts, callbacks) -> engine graph.
        # Injectable so tests can drive a fake engine.
        self._graph_factory = graph_factory or self._default_graph_factory
        # meta_judge(padrao_col, erick_col, asset_type) -> comparison dict.
        # Deterministic by default (anchored, free, keeps the run at 2 pipelines);
        # injectable so tests drive it and a future LLM narrative can slot in.
        self._meta_judge = meta_judge or (lambda p, e, at: deterministic_meta(p, e))
        self._runs: dict[str, Any] = {}
        self._lock = threading.Lock()
        # Cache TTL curto do preço LIVE da watchlist (sym -> (monotonic_ts, payload)).
        # Serve o /api/prices sem martelar a fonte nem rodar o pipeline.
        self._price_cache: dict[str, Any] = {}
        self._price_lock = threading.Lock()
        # Descritores das runs em voo (checkpoint/resume): gravados no start,
        # apagados no terminal — o que sobra num boot é a fila de retomada.
        self.active = ActiveRunStore(self.store.base / "active")
        # Checkpoint LangGraph por-nó ligado por padrão (deploy/erro no meio de uma
        # run não descarta o trabalho — retoma do último nó). Válvula de escape por
        # env; só efetivo quando há data_cache_dir (o real; o fake dos testes ignora).
        self.checkpoint_enabled = os.getenv("TRADINGDEGENS_RESUME", "1") != "0"
        # Janela de reúso de dia-corrente (DA-058), resolvida do TTL do cache OHLCV;
        # fail-soft pro default. Testes ajustam pra exercitar os dois lados do reúso.
        self.reuse_same_day_ttl = _resolve_same_day_ttl()
        # Atualizações de etapa em voo (task 002): run_id -> {node, label}. Uma
        # atualização PAUSA a run, rebobina o checkpoint e re-enfileira — três passos
        # que o usuário vive como UM. Isto os mantém legíveis no snapshot, pra a UI
        # dizer "atualizando <etapa>" em vez de piscar "pausada" no meio do caminho.
        self._refreshing: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _default_graph_factory(config, selected_analysts, callbacks):
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        return TradingAgentsGraph(
            selected_analysts=selected_analysts,
            debug=False,
            config=config,
            callbacks=callbacks,
        )

    @staticmethod
    def detect_asset_type(ticker: str) -> str:
        from cli.utils import detect_asset_type
        return detect_asset_type(ticker).value

    def start(self, ticker: str, date: str, method: str = "padrao",
              timeframe: str = _DEFAULT_TIMEFRAME,
              overrides: dict[str, Any] | None = None,
              reuse: bool = True) -> str:
        """Kick off an analysis; returns a run_id to poll immediately.

        ``method="erick"`` adds the on-demand Erick-method analyst to the run
        (Modo Erick); any other value runs the Padrão selection unchanged.

        ``reuse`` (default on) reaproveita, HONESTAMENTE, uma análise idêntica já
        concluída — mesmo (ticker, data, timeframe, método) — devolvendo o
        resultado íntegro sem re-rodar o pipeline (custo zero, marcado ``reused``,
        DA-058). Só reusa quando os INSUMOS batem: data histórica é imutável e
        reusa sempre; data=hoje só reusa dentro da janela de frescor do dado
        (:attr:`reuse_same_day_ttl`) — passada, o dado live pode ter refrescado e
        recomputa. Um run interrompido/errado não conta como concluído: um novo
        start com checkpoint ligado RETOMA do último nó (roda só o que faltou).

        ``timeframe`` is the reference frame the market analyst reads (task 012);
        it must be one of the asset's operable frames (``timeframes_for_asset``) —
        an intraday frame on an equity is rejected here, mirroring ``/api/chart``.
        Each (ticker, date, timeframe) is a distinct, cacheable run whose verdict
        may differ; only the technical read changes, the fundamental thesis holds.

        ``ticker`` may be a company NAME ("Microsoft", "Bitcoin"): it is resolved to
        the symbol via Yahoo search first (a plain ticker passes through untouched,
        no network); if Yahoo is down the raw term is kept and the data path decides.
        """
        raw = (ticker or "").strip()
        if not raw:
            raise ValueError("ticker vazio")
        ticker = (resolve_ticker_query(raw) or raw).strip().upper()
        # Default to *today in Manaus*, not the process/UTC date: at 21:30 Manaus
        # it is already the next day in UTC and the run would carry tomorrow.
        date = (date or "").strip() or timeutil.today()
        asset_type = self.detect_asset_type(ticker)
        timeframe = (timeframe or _DEFAULT_TIMEFRAME).strip() or _DEFAULT_TIMEFRAME
        allowed = timeframes_for_asset(asset_type)
        if timeframe not in allowed:
            raise ValueError(
                f"timeframe {timeframe!r} indisponível para {asset_type} "
                f"(disponíveis: {', '.join(allowed)})"
            )
        # ANÁLISE 1-2-3 (setup123): o atalho estrutural — gatilho/invalidação/SL/TP/
        # R:R do plano determinístico, $0 de LLM, sem agentes. Vive no mesmo fluxo
        # de run (histórico, reúso DA-058, status) pra o resultado abrir como qualquer
        # análise e o botão de análise completa ficar a um clique.
        if method in _METODOS_ESTRUTURAIS:
            return self._start_estrutural(method, ticker, date, asset_type, timeframe,
                                          overrides, reuse)
        selected = select_analysts_for_asset(
            asset_type, include_erick=(method == "erick")
        )
        method_norm = "erick" if "erick" in selected else "padrao"
        # Reúso entre runs (DA-058): uma análise idêntica já concluída E com o dado
        # ainda íntegro volta inteira, sem re-rodar. Só o caminho concluído; um run
        # interrompido é retomado pelo checkpoint no worker abaixo (não aqui).
        if reuse:
            prior = self._find_reusable_completed(ticker, date, timeframe, method_norm)
            if prior is not None:
                return self._register_reused_run(
                    prior, ticker, date, asset_type, selected, timeframe, overrides
                )
        run_id = timeutil.run_id_stamp() + "-" + uuid.uuid4().hex[:6]
        run = _Run(run_id, ticker, date, asset_type, selected, timeframe=timeframe,
                   overrides=overrides)
        with self._lock:
            self._runs[run_id] = run
        # Descritor em disco ANTES de rodar: um kill (deploy/OOM) no meio da run
        # deixa ele pra trás e o boot seguinte retoma. Apagado no terminal.
        self._write_descriptor(run, overrides)
        threading.Thread(target=self._worker, args=(run,), daemon=True).start()
        return run_id

    def _start_estrutural(self, method: str, ticker: str, date: str, asset_type: str,
                          timeframe: str, overrides: dict[str, Any] | None,
                          reuse: bool) -> str:
        """A run instantânea de um método ESTRUTURAL: só plano, sem LLM ($0).

        Reusa uma run idêntica DO MESMO MÉTODO (DA-058) como qualquer outro; a chave
        de reúso é o próprio método, então uma run Storm nunca volta no lugar de uma
        1-2-3 (nem de uma Padrão/Erick) do mesmo dia.
        """
        if reuse:
            prior = self._find_reusable_completed(ticker, date, timeframe, method)
            if prior is not None:
                return self._register_reused_run(
                    prior, ticker, date, asset_type, [], timeframe, overrides
                )
        run_id = timeutil.run_id_stamp() + "-" + uuid.uuid4().hex[:6]
        run = _Run(run_id, ticker, date, asset_type, [], timeframe=timeframe,
                   overrides=overrides)
        run.method = method
        with self._lock:
            self._runs[run_id] = run
        self._write_descriptor(run, overrides)
        threading.Thread(target=self._worker_estrutural, args=(run,), daemon=True).start()
        return run_id

    def _cotacao_da_run(self, run: _Run) -> dict[str, Any] | None:
        """Cotação ATUAL do ativo pra o cabeçalho — só em run de HOJE.

        O plano é date-guarded: o preço que ele carrega é o ÚLTIMO FECHAMENTO da
        série, e a tela o mostrava como se fosse "agora" (MSFT em 29/08: 505,06 de
        27/08 no cabeçalho, com o papel valendo 513,53). Buscar a cotação resolve
        metade; a outra metade é DIZER qual preço é esse — fechado, pré ou pós vêm
        rotulados de ``fetch_live_price``.

        Em run de data PASSADA não se busca nada: a cotação de hoje não pertence à
        análise daquele dia (mesma regra do DA-073), e a tela declara a data em vez
        de exibir um número que não é dali. Fail-open: sem cotação, o cabeçalho cai
        no preço da análise, como antes.
        """
        try:
            if str(run.date)[:10] != timeutil.today():
                return None
            from tradingagents.dataflows.live_price import fetch_live_price

            cot = fetch_live_price(run.ticker)
            if not cot:
                return None
            # CARIMBO do dia da cotação: o resultado inteiro é persistido, e uma run
            # reaberta amanhã mostraria a cotação de hoje como se fosse "agora". Com
            # o carimbo, a tela só a trata como atual enquanto for do dia corrente.
            cot["em"] = timeutil.today()
            return cot
        except Exception as exc:  # noqa: BLE001 — cotação nunca derruba a análise
            logger.info("cotação do cabeçalho indisponível para %s: %s", run.ticker, exc)
            return None

    def _worker_estrutural(self, run: _Run) -> None:
        """Worker das runs estruturais (1-2-3 e Storm): chart+plano e encerra.

        O STORM lê a MESMA série e acrescenta a SUA leitura — o plano do Storm entra
        em ``actionable["storm"]``, ao lado (nunca no lugar) do 1-2-3 e do recuo à
        média, porque as três são leituras INDEPENDENTES do mesmo candle e a tela
        mostra uma por card (DA-077). O gráfico da run Storm desenha a MME 80 do
        Éden, que é o filtro que decide se o setup opera.
        """
        data_notices.reset()   # avisos de qualidade de dado desta run começam do zero
        storm = run.method == "storm123"
        try:
            chart = fetch_price_chart(run.ticker, run.date, run.timeframe,
                                      "storm" if storm else "padrao")
            plan = fetch_actionable_plan(run.ticker, run.date, run.timeframe, "padrao")
            if storm:
                plan = dict(plan or {})
                plan["storm"] = fetch_storm_plan(run.ticker, run.date, run.timeframe)
            run.result = {
                "verdict": None,
                "final_decision": "",
                # O atalho também DECLARA série vencida (C4): $0 de LLM não é
                # desculpa pra mostrar número velho com cara de novo.
                "degraded": normalize_degraded(data_notices.drain()),
                "bull": "", "bear": "", "research_manager": "",
                "investment_plan": "", "trader_plan": "", "risk_decision": "",
                "market_report": "", "sentiment_report": "", "news_report": "",
                "fundamentals_report": "", "erick_report": "", "drop_nature": {},
                "derivatives_report": "",
                "price_chart": chart or {},
                "actionable": plan or {},
                "as_of_price": (plan or {}).get("price"),
                # Cotação ATUAL + a sessão dela (só em run de hoje) — ver
                # :meth:`_cotacao_da_run`.
                "live_price": self._cotacao_da_run(run),
                # Marcas de MÉTODO no resultado persistido: o front e o
                # ``compare._method_of_result`` leem daqui qual leitura é esta. São
                # excludentes — nunca uma run com as duas ligadas.
                "setup123": not storm,
                "storm123": storm,
                "timeframes": timeframes_for_asset(run.asset_type),
            }
            run.status = "done"
        except Exception as exc:  # noqa: BLE001 — erro vira run errada honesta
            logger.exception("%s run failed for %s", run.method, run.ticker)
            run.error = f"{type(exc).__name__}: {exc}"
            run.error_code = "unavailable"
            run.status = "error"
            run.result = None
        run.finished_at = time.time()
        run.finished_stamp = timeutil.stamp()
        if run.status != "cancelled":
            self._persist(run, run.status)
        self.active.remove(run.run_id)

    def _worker(self, run: _Run) -> None:
        # ``final_status`` is flipped onto the run only after the history write,
        # so any poller that sees a terminal status also sees both the result and
        # the persisted history row (no read-before-write race).
        final_status = self._execute(run)
        # PARAR/PAUSAR (task 026): uma run cancelada não vai pro histórico (não é uma
        # análise concluída nem um erro) — só encerra e libera a UI. Um PAUSAR
        # (pause_keep_resume) MANTÉM o descritor da 022 pra o "Retomar"/boot-resume
        # continuar do último nó; um PARAR apaga como um terminal normal.
        if final_status != "cancelled":
            self._persist(run, final_status)
        run.status = final_status
        # Terminal reached in-process → drop the resume descriptor so the next boot
        # doesn't re-run a finished analysis. (A kill before here leaves it behind,
        # which is exactly what makes the run recoverable.) Exceções que GUARDAM o
        # descritor: PAUSAR (retoma do último nó) e ERRO numa run RESUMÍVEL — pra o dono
        # ESCALAR a etapa que falhou com outro LLM (task 027), reaproveitando o
        # checkpoint. O boot não re-roda a errada: ``resume_interrupted`` vê o record
        # terminal e limpa o descritor.
        keep = (final_status == "cancelled" and run.pause_keep_resume) or (
            final_status == "error" and self._run_is_resumable(run)
        )
        if not keep:
            self.active.remove(run.run_id)

    def _execute(self, run: _Run) -> str:
        """Run the analysis pipeline for ``run`` in the CURRENT thread.

        Fills ``run.result`` / ``run.error`` and stamps ``finished_at`` /
        ``finished_stamp``; returns ``"done"`` or ``"error"``. Does NOT persist or
        flip ``run.status`` — the caller owns those so it can compose (the compare
        orchestrator runs two of these inline before persisting).
        """
        final_status = "error"
        # Avisos de qualidade de dado (série OHLCV vencida) são POR RUN: zera na
        # entrada pra um worker nunca herdar o aviso do anterior na mesma thread.
        data_notices.reset()
        # Config efetiva computada fora do try pra estar disponível no except mesmo
        # se a construção do grafo falhar (o provider vira parte da mensagem humana).
        config = apply_llm_overrides(self.base_config, run.overrides)
        # Checkpoint por-nó (resume): o grafo persiste o estado a cada nó concluído
        # num SQLite keyed por ticker+data+forma-do-grafo, e RETOMA do último nó se
        # já houver checkpoint (deploy/erro no meio não joga o trabalho fora). Só
        # quando há data_cache_dir (o grafo real; o fake dos testes ignora a config).
        config["checkpoint_enabled"] = bool(
            self.checkpoint_enabled and config.get("data_cache_dir")
        )
        # Fallback transparente (task 027-fallback): monta o tracker das trocas deste
        # run e injeta no config, junto do sinal de DONO (allow_server_key destrava a
        # cadeia de fallback com chave de servidor). Ambos são TRANSIENTES — o grafo os
        # tira do config ANTES do set_config global, então não vazam no singleton nem
        # em record persistido. Sem dono (público/BYOK) → allow_server_key False → a
        # cadeia fica só no topo (comportamento de hoje). O tracker fica no run pra o
        # snapshot ao vivo e a enriquecimento do resultado lerem as trocas.
        from tradingagents.llm_clients.fallback import FallbackTracker
        fb_tracker = FallbackTracker()
        run._fallback_tracker = fb_tracker
        config["_fallback_tracker"] = fb_tracker
        config["_allow_server_key"] = run.overrides.get("allow_server_key") is True
        # Provedor owner-only (assinatura do dono, ex.: claude-cli): só roda pro dono
        # logado — nem BYOK falso destrava. Barra ANTES da chave (a assinatura é do
        # dono; público jamais roteia a análise dele pela cota do dono).
        if _owner_only_blocked(config, run.overrides):
            run.error = _OWNER_ONLY_MESSAGE
            run.error_code = _OWNER_ONLY_CODE
            run.result = None
            run.finished_at = time.time()
            run.finished_stamp = timeutil.stamp()
            return "error"
        # Gating da chave do servidor (defesa em profundidade — o server já recusa
        # antes de criar a run): requisição PÚBLICA (allow_server_key=False, que o
        # server marca explicitamente pra quem não é dono) sem chave própria NÃO roda
        # e NUNCA cai na env do servidor. Chamador interno confiável (sem o flag) roda.
        if not config.get("llm_api_key") and run.overrides.get("allow_server_key") is False:
            run.error = NEED_KEY_MESSAGE
            run.error_code = NEED_KEY_CODE
            run.result = None
            run.finished_at = time.time()
            run.finished_stamp = timeutil.stamp()
            return "error"
        # Cross-provider (027): valida a credencial de CADA nível (RÁPIDO/PESADO)
        # ANTES de rodar — um nível sem credencial erra aqui, não roda pela metade.
        lvl_code, lvl_msg = levels_credential_error(config, run.overrides)
        if lvl_code:
            run.error = lvl_msg
            run.error_code = lvl_code
            run.result = None
            run.finished_at = time.time()
            run.finished_stamp = timeutil.stamp()
            return "error"
        try:
            # Config efetiva da run: base do servidor + overrides BYOK (chave do
            # usuário tem prioridade; sem chave, cai na env do servidor). O grafo
            # tira ``llm_api_key`` do dict antes do set_config global.
            progress_cb = ProgressCallbackHandler(run.tracker)
            thinking_cb = ThinkingCallbackHandler(run.thinking)
            # PARAR/PAUSAR (task 026): o callback levanta RunCancelled no próximo limite
            # quando o usuário cancela — aborta o grafo em poucos segundos, sem órfão.
            cancel_cb = CancelCallbackHandler(run.cancel_event)
            graph = self._graph_factory(
                config, run.selected_analysts,
                [run.usage_cb, progress_cb, thinking_cb, cancel_cb],
            )
            # RETOMADA (task 002/DA-062): o que já estava pronto no checkpoint entra
            # no stepper JÁ concluído (verde) e com o parecer preservado no painel.
            # Sem isto o LangGraph pula esses nós — nenhum callback dispara — e a tela
            # pinta de cinza justamente o trabalho que foi salvo, como se nada tivesse
            # rodado. Melhor esforço: falhar aqui só custaria a cor, não a análise.
            if run.resuming:
                self._seed_from_checkpoint(run, config)
            final_state, signal = graph.propagate(
                run.ticker, run.date, asset_type=run.asset_type,
                timeframe=run.timeframe
            )
            # Avisos da camada de FETCH (série OHLCV vencida servida no fail-open)
            # entram no mesmo ``degraded_sources`` das fontes que caíram — é o canal
            # que a UI já sabe nomear. Sem isto o dado velho chegava mudo (bug L2).
            data_notices.merge_into(final_state)
            run.result = extract_result(final_state, signal)
            # Cotação ATUAL + sessão (fechado/pré/pós) pro cabeçalho — só run de hoje.
            run.result["live_price"] = self._cotacao_da_run(run)
            if run.asset_type == "crypto":
                run.result["derivatives_report"] = fetch_derivatives_report(
                    run.ticker, run.date
                )
            # A estrutura desenhada é CIENTE DO MÉTODO (task 031): a coluna Erick lê o
            # recuo/1-2-3 na EMA 8/21 (a média do método), o Padrão nas MMS — assim o
            # confronto mostra estruturas de verdade diferentes, não o mesmo overlay 2x.
            method = "erick" if "erick" in run.selected_analysts else "padrao"
            # The chart opens on the SAME frame the verdict was computed on, so the
            # picture matches the stamp; the UI can still recalc other frames via
            # /api/chart. Persist the ladder + the shown frame + the verdict frame.
            run.result["price_chart"] = fetch_price_chart(
                run.ticker, run.date, run.timeframe, method
            )
            run.result["actionable"] = fetch_actionable_plan(
                run.ticker, run.date, run.timeframe, method
            )
            # THE single frozen reference price of the run (date-guarded daily close,
            # the same one the chart/verdict/fundamentals-anchor use). One canonical
            # field so the cover, UI and automated consumers never disagree on price.
            run.result["as_of_price"] = (run.result.get("actionable") or {}).get("price")
            # Reading axes (item 8): every parecer declares eixo+horizonte so the UI
            # frames the modules as layers, not competing verdicts.
            run.result["axes"] = module_axes()
            # Audit footer (item 10): run_id + single collection timestamp + pipeline
            # version + the model behind each agent tier, so a regression between runs
            # is attributable instead of a mystery. deep_think drives the juiz / PM /
            # risco / debate; quick_think the analistas / trader.
            run.result["audit"] = {
                "run_id": run.run_id,
                "collected_at": timeutil.stamp(),
                "pipeline_version": _pipeline_version(),
                "models": {
                    "provider": config.get("llm_provider"),
                    "deep_think": config.get("deep_think_llm"),
                    "quick_think": config.get("quick_think_llm"),
                },
                # Atribuição POR ETAPA (task 024, parte 1): qual provedor+modelo REALMENTE
                # rodou cada etapa (capturado dos callbacks do LLM, não inferido da
                # config). Responde "qual LLM fez cada etapa" de forma auditável; vazio
                # numa run reaproveitada (não rodou LLM) — honesto, sem inventar.
                "models_by_step": run.thinking.models_snapshot(),
            }
            run.result["timeframe"] = run.timeframe
            run.result["verdict_timeframe"] = run.timeframe
            run.result["timeframes"] = timeframes_for_asset(run.asset_type)
            # Scrub internal error/component strings from the published texts (item 6d)
            # — the reader sees "dados indisponíveis", not a NoMarketDataError leak.
            sanitize_result(run.result)
            # Keystone pre-publication check (item 7): a final deterministic sweep of
            # the ASSEMBLED report for the whole 1-6 class of inconsistencies (double
            # decision, chart-vs-text 1-2-3, price drift, aggregates that don't
            # reconcile). Attached as a list the UI surfaces — a listed inconsistency
            # beats a hard block; fail-open so a checker bug never sinks a run.
            run.result["contradictions"] = check_contradictions(run.result)
            # Carimbo do veredito (task 016): se os insumos tinham inconsistência na
            # hora da decisão, o juiz recebeu os DADOS VERIFICADOS pra usar o valor
            # certo — e AINDA assim o veredito sai marcado, pra o julgamento nunca se
            # apoiar calado num dado furado (ou usa o verificado, ou sai avisado).
            run.result["verdict_caveat"] = format_verdict_caveat(
                run.result.get("pre_judge_findings"))
            # Fallback transparente (task 027-fallback): se houve troca AUTOMÁTICA de
            # provedor em alguma etapa, marca isso NO resultado — banner de resumo +
            # selo na etapa que caiu ("fallback X→Y, motivo 429"). A análise não parou;
            # o leitor SABE que houve o desvio (não silencioso). Vazio → nada muda.
            self._apply_fallbacks(run.result, fb_tracker)
            run.tracker.mark_done()
            final_status = "done"
        except RunCancelled:
            # Usuário mandou PARAR/PAUSAR (task 026): estado HONESTO — não é erro, não
            # fingir concluída. Sem result/erro; a UI mostra "interrompida pelo usuário".
            # O worker decide manter (Pausar) ou apagar (Parar) o descritor de retomada.
            run.error = None
            run.error_code = None
            run.result = None
            run.tracker.mark_cancelled()
            final_status = "cancelled"
        except Exception as exc:  # surface, never crash the server
            # BYOK + erro humano: a UI recebe uma frase acionável em pt-BR (429 sem
            # crédito, 401 chave inválida, rate limit, timeout) SEM stack e SEM a
            # chave. O técnico cru (redigido da chave) vai só pro log do servidor.
            secret = run.overrides.get("api_key")
            provider = (config.get("llm_provider") or "").lower() if isinstance(config, dict) else ""
            run.error = _clean_error(exc, provider, secret)
            run.error_code = _error_code(exc, provider, secret)
            # PRESERVAR o trabalho já feito (task 015): um erro no meio NÃO zera a
            # análise. Monta um result PARCIAL com as etapas concluídas (analistas +
            # debate, o que o raciocínio-ao-vivo já capturou em memória — vale owner E
            # BYOK) e marca a etapa que falhou. Vazio (nada concluído) → None honesto.
            # A run PARA aqui; o dono CONTINUA do ponto via escalar (027)/retomar (022),
            # que reaproveitam o checkpoint — nunca refaz tudo (só re-run explícito).
            failed_label = ""
            try:
                run.tracker.mark_failed()
                failed_label = run.tracker.current_label()
            except Exception:  # noqa: BLE001 — marcar a falha nunca pode derrubar o run
                pass
            run.result = self._partial_result_from_thinking(run, failed_label)
            logger.warning(
                "run %s falhou: %s", run.run_id,
                _redact_secret(traceback.format_exc()[-3000:], secret),
            )
        run.finished_at = time.time()
        run.finished_stamp = timeutil.stamp()
        return final_status

    # node LangGraph (chave do raciocínio-ao-vivo) -> campo do result. Só os nós cujo
    # TEXTO já foi capturado (etapas concluídas) entram no result parcial (task 015).
    _THINKING_TO_RESULT = {
        "Market Analyst": "market_report",
        "Sentiment Analyst": "sentiment_report",
        "News Analyst": "news_report",
        "Fundamentals Analyst": "fundamentals_report",
        "Erick Analyst": "erick_report",
        "Bull Researcher": "bull",
        "Bear Researcher": "bear",
        "Research Manager": "research_manager",
        "Trader": "trader_plan",
        "Portfolio Manager": "risk_decision",
    }

    def _partial_result_from_thinking(self, run: _Run, failed_label: str):
        """Result PARCIAL a partir do que o run já produziu (task 015).

        Um erro no meio não pode descartar as etapas concluídas. O raciocínio-ao-vivo
        (task 008) já captura, EM MEMÓRIA, o texto de cada nó conforme termina — vale
        pro dono E pro BYOK, sem depender do checkpoint. Aqui viramos isso num result
        na MESMA forma do sucesso (tolerante a campos faltando), marcado ``partial`` +
        ``failed_step`` pra a UI mostrar as concluídas e a etapa que parou (em vez de
        tela vazia). Retorna ``None`` quando NADA concluiu — aí não há o que preservar."""
        try:
            snap = run.thinking.snapshot()
        except Exception:  # noqa: BLE001
            snap = []
        by_node = {it.get("id"): (it.get("text") or "") for it in snap if it.get("text")}
        if not by_node:
            return None   # nada concluído — erro honesto de tela vazia (não há parcial)
        result = extract_result({}, "")   # shell na forma canônica (tudo vazio)
        for node, field in self._THINKING_TO_RESULT.items():
            txt = by_node.get(node)
            if txt:
                result[field] = txt
        # marca o estado parcial + a etapa que falhou (a UI para nela, não zera)
        result["partial"] = True
        result["failed_step"] = {"label": failed_label or ""}
        result["error"] = run.error
        result["error_code"] = run.error_code
        result["timeframe"] = run.timeframe
        result["verdict_timeframe"] = run.timeframe
        try:
            result["timeframes"] = timeframes_for_asset(run.asset_type)
        except Exception:  # noqa: BLE001
            pass
        result["audit"] = {
            "run_id": run.run_id,
            "collected_at": timeutil.stamp(),
            "pipeline_version": _pipeline_version(),
            "models_by_step": run.thinking.models_snapshot(),
            "partial": True,
        }
        # Fallbacks transparentes que já rolaram antes do erro (task 027-fallback).
        try:
            self._apply_fallbacks(result, getattr(run, "_fallback_tracker", None))
        except Exception:  # noqa: BLE001
            pass
        # Escova strings internas de erro dos textos publicados (mesma higiene do sucesso).
        try:
            sanitize_result(result)
        except Exception:  # noqa: BLE001
            pass
        return result

    @staticmethod
    def _apply_fallbacks(result: dict[str, Any] | None, tracker) -> None:
        """Carimba as trocas AUTOMÁTICAS de provedor no resultado (transparência).

        Nada acontece quando não houve desvio — o caminho feliz fica byte-a-byte igual.
        Havendo troca(s): ``result['fallbacks']`` recebe a lista plana (pro banner de
        resumo) e cada linha da atribuição por-etapa (``audit.models_by_step``) do nó
        que caiu ganha um campo ``fallback`` com de→para + motivo, pra o selo aparecer
        exatamente na etapa onde o motor trocou de provedor."""
        if not result or tracker is None or not tracker.any():
            return
        hops = tracker.snapshot()
        result["fallbacks"] = hops
        audit = result.get("audit") or {}
        steps = audit.get("models_by_step") or []
        by_node: dict[Any, list[dict[str, Any]]] = {}
        for h in hops:
            by_node.setdefault(h.get("node"), []).append(h)
        for s in steps:
            node_hops = by_node.get(s.get("node"))
            if not node_hops:
                continue
            last = node_hops[-1]
            s["fallback"] = {
                "from_provider": node_hops[0].get("from_provider"),
                "to_provider": last.get("to_provider"),
                "reason": last.get("reason"),
                "code": last.get("code"),
                "hops": len(node_hops),
            }
        audit["models_by_step"] = steps
        result["audit"] = audit

    def _persist(self, run: _Run, status: str) -> None:
        try:
            cost = run.cost()
            elapsed = round((run.finished_at or time.time()) - run.started_at, 1)
            record = {
                "run_id": run.run_id,
                "ticker": run.ticker,
                "date": run.date,
                "asset_type": run.asset_type,
                "status": status,
                "error": run.error,
                "error_code": run.error_code,
                "verdict": (run.result or {}).get("verdict") if status == "done" else None,
                "verdict_timeframe": run.timeframe,
                # Method for the manual-confront picker (task 018): reliable from the
                # analyst selection, done or errored. setup123 (run instantânea do
                # 1-2-3) vem como método próprio — nunca colide com padrao/erick.
                "method": getattr(run, "method", None)
                or ("erick" if "erick" in run.selected_analysts else "padrao"),
                # Veredito de uma run 1-2-3 (setup_state) — a watchlist mostra isto
                # no lugar de "CONCLUÍDO": setup123 não tem verdict Buy/Hold, o seu
                # resultado é o estado do setup (ativo/aguardar_*/sem_*). Vem do
                # mesmo actionable plan que a view aberta já rendera (SETUP_PT).
                "setup_state": ((run.result or {}).get("actionable") or {}).get("setup_state"),
                "cost_usd": cost["usd"],
                "elapsed": elapsed,
                # Manaus wall-clock with explicit -04:00 offset, so the UI can
                # show it verbatim regardless of the browser's timezone.
                "finished_at": run.finished_stamp or timeutil.stamp(),
                "result": run.result,
                "cost": cost,
            }
            self.store.save(record)
        except Exception:
            pass  # history is best-effort; a failed write must not kill the run

    # ------------------------------------------ reúso entre runs (DA-058) ------
    def _find_reusable_completed(
        self, ticker: str, date: str, timeframe: str, method: str
    ) -> dict[str, Any] | None:
        """Registro DONE mais recente idêntico em (ticker, data, timeframe, método)
        cujo dado ainda é íntegro (:meth:`_is_reuse_fresh`), ou ``None``.

        A chave inclui o método (padrao/erick); registros de comparação (method=
        ``compare``) e errados/interrompidos ficam de fora — não são uma leitura
        simples reaproveitável. É o análogo single-run do ``_find_reusable`` do
        confronto, com a guarda de frescor de dia-corrente por cima (correção de
        cache, herda DA-058: só reusa quando o insumo é o mesmo)."""
        want = (method or "padrao").lower()
        for summ in self.store.recent(_REUSE_SCAN_LIMIT):
            if summ.get("status") != "done":
                continue
            if (summ.get("ticker") or "").upper() != ticker.upper():
                continue
            if (summ.get("date") or "") != date:
                continue
            if (summ.get("verdict_timeframe") or _DEFAULT_TIMEFRAME) != timeframe:
                continue
            if (summ.get("method") or "padrao").lower() != want:
                continue
            rec = self._record(summ["run_id"])
            if not rec or rec.get("status") != "done":
                continue
            res = rec.get("result") or {}
            if not res or res.get("compare"):
                continue  # nada a reusar / não é leitura simples
            # Invalidação de 1º deploy (task 005 — coerência do drop_nature): um
            # registro erick gravado ANTES do fix não tem o campo ``drop_nature`` e
            # reapareceria com o Estado antigo (contraditório). Não reusa — força
            # recomputar; o novo registro já traz o campo e volta a reusar normalmente.
            if want == "erick" and "drop_nature" not in res:
                continue
            if not self._is_reuse_fresh(rec, date):
                continue
            return rec
        return None

    def _is_reuse_fresh(self, record: dict[str, Any], date: str) -> bool:
        """Se o dado que ``record`` leu ainda vale pra reusar HOJE (correção de cache,
        DA-058).

        Data histórica (< hoje em Manaus) é imutável → reusa sempre. Data=hoje só
        reusa dentro da janela de frescor (:attr:`reuse_same_day_ttl`, o TTL do
        cache OHLCV): passada, a barra live pode ter refrescado (candle parcial →
        final) e o julgamento fica obsoleto — recomputa. Fail-safe: se não dá pra
        determinar a idade de um registro de hoje, NÃO reusa (recomputar é mais
        seguro que reusar podre)."""
        try:
            today = timeutil.today()
        except Exception:  # noqa: BLE001
            return False
        if (date or "") < today:
            return True  # passado imutável
        if (date or "") > today:
            return False  # futuro (não deveria ocorrer) → conservador
        # Dia corrente: idade do run < TTL de frescor?
        stamp = record.get("finished_at") or (record.get("result") or {}).get(
            "audit", {}
        ).get("collected_at")
        if not stamp:
            return False
        try:
            finished = datetime.fromisoformat(str(stamp))
            age = (timeutil.now() - finished).total_seconds()
        except Exception:  # noqa: BLE001
            return False
        return 0 <= age < float(self.reuse_same_day_ttl)

    def _register_reused_run(
        self, prior: dict[str, Any], ticker: str, date: str, asset_type: str,
        selected: list[str], timeframe: str, overrides: dict[str, Any] | None,
    ) -> str:
        """Cria um run já CONCLUÍDO que devolve, íntegro, o resultado de ``prior`` —
        sem tocar no pipeline (custo zero). Marca ``reused``/``reused_from`` pra o
        front badgear "reaproveitado" e pra o teste provar que o grafo não rodou.
        Não re-persiste no histórico (o original já está lá) nem grava descritor."""
        run_id = timeutil.run_id_stamp() + "-" + uuid.uuid4().hex[:6]
        run = _Run(run_id, ticker, date, asset_type, selected, timeframe=timeframe,
                   overrides=overrides)
        # O método do ORIGINAL prevalece (setup123 reusa setup123; erick, erick) —
        # o reuso nunca muda o rótulo do que está devolvendo.
        if prior.get("method"):
            run.method = prior["method"]
        result = copy.deepcopy(prior.get("result") or {})
        result["reused"] = True
        result["reused_from"] = prior.get("run_id")
        # A ANÁLISE é reaproveitada; a COTAÇÃO não. O reuso devolve a leitura
        # estrutural íntegra (é isso que o DA-058 promete), mas o preço de tela é do
        # momento em que se OLHA — herdar o do run anterior mostraria uma cotação
        # velha com carimbo de agora, que é o defeito que esta tela acabou de
        # corrigir. Fail-open: sem cotação, some o bloco (o preço da análise fica).
        result["live_price"] = self._cotacao_da_run(run)
        run.result = result
        run.reused = True
        run.reused_from = prior.get("run_id")
        run.finished_at = time.time()
        run.finished_stamp = timeutil.stamp()
        run.tracker.mark_done()
        run.status = "done"
        with self._lock:
            self._runs[run_id] = run
        return run_id

    # ---------------------------------- checkpoint / resume (deploy-safe) ------
    def _write_descriptor(
        self, run: _Run, overrides: dict[str, Any] | None
    ) -> None:
        """Grava o descritor de retomada da run em disco (sem NUNCA a chave BYOK).

        ``resumable`` só quando a run roda com a chave do SERVIDOR (dono logado, sem
        chave colada): aí um boot pós-restart a retoma sozinho. Uma run BYOK (chave
        do usuário, que vive só no navegador) é não-resumível — o boot a re-superfície
        HONESTAMENTE como "interrompida, rode de novo", jamais finge que rodou."""
        ov = overrides or {}
        resumable = (not ov.get("api_key")) and (ov.get("allow_server_key") is True)
        safe_ov: dict[str, Any] = {"allow_server_key": True} if resumable else {}
        for k in ("provider", "deep_model", "quick_model", "base_url"):
            if ov.get(k):
                safe_ov[k] = ov[k]
        self.active.put(run.run_id, {
            "run_id": run.run_id,
            "ticker": run.ticker,
            "date": run.date,
            "asset_type": run.asset_type,
            "timeframe": run.timeframe,
            "method": run.method,
            "selected_analysts": list(run.selected_analysts),
            "started_at": run.finished_stamp or timeutil.stamp(),
            "resumable": resumable,
            "overrides": safe_ov,
        })

    def resume_interrupted(self) -> int:
        """Na subida do servidor: retoma as runs que um restart matou no meio.

        Cada descritor que sobrou em disco é uma run interrompida. As resumíveis
        (chave do servidor) voltam a rodar — com o checkpoint por-nó, RETOMAM do
        último estágio concluído (só o que faltou), não do zero. As não-resumíveis
        (BYOK, sem a chave) viram um registro ``error`` honesto ("interrompida, rode
        de novo") — nada de reúso falso. Retorna quantas foram re-enfileiradas."""
        resumed = 0
        for desc in self.active.list_pending():
            rid = desc.get("run_id")
            if not rid:
                continue
            # Já concluída em disco (crash entre persist e remove) → só limpa.
            rec = self._record(rid)
            if rec and rec.get("status") in ("done", "error"):
                self.active.remove(rid)
                continue
            if not desc.get("resumable"):
                self._persist_interrupted(desc)
                self.active.remove(rid)
                continue
            try:
                self._reenqueue(desc)
                resumed += 1
            except Exception:  # noqa: BLE001 — uma retomada não pode derrubar o boot
                logger.warning("falha ao retomar run %s", rid, exc_info=True)
        return resumed

    def _reenqueue(self, desc: dict[str, Any]) -> None:
        """Sobe de novo uma run interrompida resumível. O worker chama o mesmo
        ``_execute``; com o checkpoint ligado e o thread_id derivado de ticker+data+
        forma-do-grafo (não do run_id), o grafo retoma do último nó concluído."""
        asset_type = desc.get("asset_type") or self.detect_asset_type(desc["ticker"])
        selected = desc.get("selected_analysts") or select_analysts_for_asset(
            asset_type, include_erick=(desc.get("method") == "erick")
        )
        run = _Run(
            desc["run_id"], desc["ticker"], desc["date"], asset_type, list(selected),
            timeframe=desc.get("timeframe", _DEFAULT_TIMEFRAME),
            overrides=desc.get("overrides") or {},
        )
        run.resuming = True
        with self._lock:
            self._runs[run.run_id] = run
        threading.Thread(target=self._worker, args=(run,), daemon=True).start()

    def _persist_interrupted(self, desc: dict[str, Any]) -> None:
        """Registro honesto de uma run interrompida que NÃO dá pra retomar sozinha
        (BYOK, sem a chave). Aparece no histórico como erro claro, não some calada
        nem finge estar completa."""
        try:
            record = {
                "run_id": desc.get("run_id"),
                "ticker": desc.get("ticker"),
                "date": desc.get("date"),
                "asset_type": desc.get("asset_type"),
                "status": "error",
                "error": ("Análise interrompida por reinício do servidor — rode de "
                          "novo (sua chave não fica salva)."),
                "error_code": "interrupted",
                "verdict": None,
                "verdict_timeframe": desc.get("timeframe"),
                "method": desc.get("method", "padrao"),
                "cost_usd": 0,
                "elapsed": 0,
                "finished_at": timeutil.stamp(),
                "result": None,
                "cost": {"usd": 0, "complete": True},
            }
            self.store.save(record)
        except Exception:  # noqa: BLE001
            pass  # registro honesto é best-effort; não pode derrubar o boot

    def active_run_ids(self) -> list[str]:
        """Ids das runs ainda EXECUTANDO neste processo (pro /api/health e o drain
        de shutdown gracioso). Reused/terminais não contam."""
        with self._lock:
            return [rid for rid, r in self._runs.items()
                    if getattr(r, "status", "") == "running"]

    def _run_is_resumable(self, run: _Run) -> bool:
        """PAUSAR (retomável) só é honesto quando a run RETOMA sem a chave do usuário:
        sem chave BYOK na requisição (cai na env do servidor) e com checkpoint ligado.
        BYOK não é retomável — a chave não persiste no descritor da 022."""
        return not run.overrides.get("api_key") and bool(self.checkpoint_enabled)

    def cancel(self, run_id: str, *, keep_resume: bool = False) -> dict[str, Any] | None:
        """PARAR (``keep_resume=False``) ou PAUSAR (``True``) uma run em andamento.

        Cooperativo (task 026): seta o Event de cancelamento; o CancelCallbackHandler
        levanta RunCancelled no próximo limite (nó/LLM/token), o grafo aborta e o worker
        encerra com status ``cancelled`` em poucos segundos — ``active_runs`` cai a 0,
        sem thread órfão nem freeze. PAUSAR só é honrado se a run é retomável (senão vira
        PARAR honesto, sem prometer retomada). ``None`` se a run não existe, já terminou,
        ou não é cancelável (ex.: comparação). Idempotente: cancelar de novo é no-op."""
        with self._lock:
            run = self._runs.get(run_id)
        if run is None or not hasattr(run, "cancel_event"):
            return None
        if run.status != "running":
            return None
        paused = bool(keep_resume) and self._run_is_resumable(run)
        run.pause_keep_resume = paused
        run.cancel_event.set()
        return {"run_id": run_id, "cancelled": True, "paused": paused,
                "resumable": self._run_is_resumable(run)}

    def resume(self, run_id: str) -> dict[str, Any] | None:
        """RETOMAR uma run PAUSADA (task 026): re-enfileira a partir do descritor que o
        Pausar guardou. Com o checkpoint da 022 (thread_id = ticker+data+forma-do-grafo),
        o grafo CONTINUA do último nó concluído — reaproveita o que já rodou, não recomeça.
        Só resumível (dono/servidor). ``None`` se não há descritor resumível pra esse id.
        Idempotente: se já está rodando, é no-op."""
        with self._lock:
            cur = self._runs.get(run_id)
        if cur is not None and getattr(cur, "status", "") == "running":
            return {"run_id": run_id, "resuming": True}   # já rodando: no-op
        desc = self.active.get(run_id)
        if not desc or not desc.get("resumable"):
            return None
        self._reenqueue(desc)
        return {"run_id": run_id, "resuming": True}

    def escalate(self, run_id: str, level: str,
                 provider: str | None = None, model: str | None = None) -> dict[str, Any] | None:
        """ESCALAR uma etapa que falhou/degradou com OUTRO LLM (task 027 parte B).

        Re-roda SÓ a etapa incompleta reaproveitando o checkpoint (022): o thread_id
        é ticker+data+forma-do-grafo (NÃO inclui modelo), então trocar o provedor+
        modelo do nível mantém os nós já concluídos e re-executa só o que faltou com o
        LLM escalado. ``level`` ∈ {``quick``, ``deep``}: quick = analistas/debate/
        trader/risco, deep = pesquisa/juiz.

        Só uma run RESUMÍVEL (dono/servidor, checkpoint ligado) pode escalar — uma run
        BYOK não é retomável (a chave não persiste) e retorna indisponível HONESTO.
        ``None`` só quando não existe descritor algum pro id. Idempotente: se já está
        rodando, é no-op."""
        if level not in ("quick", "deep"):
            return {"ok": False, "code": "bad_level",
                    "error": "nível inválido para escalonamento (use 'quick' ou 'deep')."}
        with self._lock:
            cur = self._runs.get(run_id)
        if cur is not None and getattr(cur, "status", "") == "running":
            return {"ok": True, "run_id": run_id, "escalating": True}  # já rodando: no-op
        desc = self.active.get(run_id)
        if not desc:
            return None
        if not desc.get("resumable"):
            return {"ok": False, "code": "not_resumable",
                    "error": ("Escalonamento indisponível: esta análise não é retomável "
                              "(rodou com chave própria, que não fica salva). Rode de novo.")}
        # Aplica o override do nível escalado por cima do descritor (modo avançado).
        ov = dict(desc.get("overrides") or {})
        ov["allow_server_key"] = True
        ov["advanced"] = True
        prov = (provider or "").strip().lower()
        if prov:
            ov[f"{level}_provider"] = prov
        if (model or "").strip():
            ov[f"{level}_model"] = model.strip()
        if not ov.get(f"{level}_provider") and not ov.get(f"{level}_model"):
            return {"ok": False, "code": "no_target",
                    "error": "informe o provedor e/ou o modelo do LLM para escalar."}
        desc = dict(desc)
        desc["overrides"] = ov
        self.active.put(run_id, desc)
        self._reenqueue(desc)
        return {"ok": True, "run_id": run_id, "escalating": True,
                "level": level, "provider": ov.get(f"{level}_provider"),
                "model": ov.get(f"{level}_model")}

    # ------------------------------------- atualizar UMA etapa com dado fresco --
    def _checkpoint_addr(self, selected_analysts, asset_type: str, timeframe: str,
                         overrides: dict[str, Any] | None = None,
                         ) -> tuple[str, str] | None:
        """``(data_cache_dir, assinatura)`` que endereçam o checkpoint de um run.

        Mesmo par que o motor deriva em ``propagate`` — vem de ``run_signature``, a
        fonte única, pra a UI ler/rebobinar EXATAMENTE a thread que o grafo usa em vez
        de uma chave parecida. ``None`` quando o checkpoint está desligado ou não há
        diretório de cache (o fake dos testes)."""
        from tradingagents.graph.trading_graph import run_signature
        config = apply_llm_overrides(self.base_config, overrides or {})
        data_dir = config.get("data_cache_dir")
        if not (self.checkpoint_enabled and data_dir):
            return None
        return data_dir, run_signature(
            list(selected_analysts), config.get("max_debate_rounds"),
            config.get("max_risk_discuss_rounds"), asset_type, timeframe,
        )

    def _seed_from_checkpoint(self, run: _Run, config: dict[str, Any]) -> None:
        """Traz pro tracker/painel as etapas que a retomada recuperou do checkpoint."""
        try:
            from tradingagents.graph.checkpointer import completed_reports
            data_dir = config.get("data_cache_dir")
            if not (config.get("checkpoint_enabled") and data_dir):
                return
            addr = self._checkpoint_addr(run.selected_analysts, run.asset_type,
                                         run.timeframe, run.overrides)
            if addr is None:
                return
            reports = completed_reports(data_dir, run.ticker, run.date, addr[1])
            if not reports:
                return
            run.tracker.mark_resumed(list(reports))
            run.thinking.seed_from_checkpoint(reports)
        except Exception:  # noqa: BLE001 — cor de stepper nunca derruba uma análise
            logger.debug("falha ao ler etapas prontas do checkpoint", exc_info=True)

    def refresh_step(self, run_id: str, node: str) -> dict[str, Any] | None:
        """ATUALIZAR uma etapa concluída com DADO FRESCO (task 002 / DA-062).

        Não é o ESCALAR (027): lá o que muda é o LLM, aqui é o DADO. O cache de preço
        do ativo é invalidado e o checkpoint REBOBINA pra antes daquela etapa, então
        ela re-roda com número novo — e tudo que veio ANTES continua voltando pronto
        do checkpoint, de graça. O que vinha DEPOIS re-roda junto por necessidade: foi
        julgado em cima do dado que o usuário acabou de trocar, e manter seria carimbar
        um veredito sobre número que não existe mais.

        Só run RESUMÍVEL (dono/servidor, checkpoint ligado), igual ao escalar. Se a run
        ainda está viva, PAUSA primeiro e só rebobina quando ela encerra — mexer no
        checkpoint sob um grafo em execução o corromperia. ``None`` quando não há
        descritor algum pro id."""
        stage = stage_for_node(node)
        if stage is None:
            return {"ok": False, "code": "bad_step",
                    "error": "etapa desconhecida para atualização."}
        desc = self.active.get(run_id)
        if not desc:
            return None
        if not desc.get("resumable"):
            return {"ok": False, "code": "not_resumable",
                    "error": ("Atualizar etapa indisponível: esta análise não é "
                              "retomável (rodou com chave própria, que não fica "
                              "salva). Rode de novo.")}
        with self._lock:
            if run_id in self._refreshing:
                return {"ok": True, "run_id": run_id, "refreshing": True,
                        "node": node, "label": stage[1]}   # já pedido: idempotente
            cur = self._runs.get(run_id)
            alive = cur is not None and getattr(cur, "status", "") == "running"
            self._refreshing[run_id] = {"node": node, "label": stage[1]}
        if alive:
            self.cancel(run_id, keep_resume=True)
        threading.Thread(target=self._refresh_worker,
                         args=(run_id, node, dict(desc), alive), daemon=True).start()
        return {"ok": True, "run_id": run_id, "refreshing": True,
                "node": node, "label": stage[1], "paused_first": alive}

    def _refresh_worker(self, run_id: str, node: str, desc: dict[str, Any],
                        wait: bool) -> None:
        """Pausa (se preciso) → invalida o dado → rebobina o checkpoint → re-enfileira."""
        try:
            if wait and not self._await_idle(run_id):
                logger.warning("atualização de etapa abortada: run %s não encerrou",
                               run_id)
                return
            selected = desc.get("selected_analysts") or select_analysts_for_asset(
                desc.get("asset_type") or "stock",
                include_erick=(desc.get("method") == "erick"),
            )
            addr = self._checkpoint_addr(
                selected, desc.get("asset_type") or "stock",
                desc.get("timeframe", _DEFAULT_TIMEFRAME), desc.get("overrides"),
            )
            if addr is not None:
                data_dir, signature = addr
                # dado fresco de verdade: sem isto a etapa re-roda lendo o MESMO
                # candle do cache e o usuário atualiza pra receber o que já tinha.
                from tradingagents.dataflows.cache_control import invalidate_price_cache
                from tradingagents.graph.checkpointer import rewind_before_node
                invalidate_price_cache(data_dir, desc["ticker"])
                rewind_before_node(data_dir, desc["ticker"], desc["date"],
                                   signature, node=node)
            # Re-grava o descritor: se a run encerrou como terminal enquanto
            # esperávamos, o worker antigo o apagou — e a run que vai subir agora
            # precisa dele pra ser recuperável num restart.
            self.active.put(run_id, desc)
            self._reenqueue(desc)
        except Exception:  # noqa: BLE001 — um refresh não pode derrubar o servidor
            logger.warning("falha ao atualizar etapa %s da run %s", node, run_id,
                           exc_info=True)
        finally:
            with self._lock:
                self._refreshing.pop(run_id, None)

    def _await_idle(self, run_id: str, timeout: float = 180.0) -> bool:
        """Espera a run sair do ``running`` (pausa cooperativa). False no estouro."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                run = self._runs.get(run_id)
            if run is None or getattr(run, "status", "") != "running":
                return True
            time.sleep(0.2)
        return False

    # ----------------------------------------------------- compare (Fase 3) ----
    def start_compare(self, ticker: str, date: str,
                      timeframe: str = _DEFAULT_TIMEFRAME,
                      overrides: dict[str, Any] | None = None) -> str:
        """Kick off a Padrão × Erick comparison; returns a run_id to poll.

        Runs both readings (reusing a cached prior run for either side when one
        exists for the same ticker/date/timeframe) and confronts them with the
        meta-judge. Cost is up to two pipelines — a cached side costs nothing.

        ``ticker`` may be a company name — resolved to its symbol first (see
        :meth:`start`), so "Microsoft" and "MSFT" both confront the same run.
        """
        raw = (ticker or "").strip()
        if not raw:
            raise ValueError("ticker vazio")
        ticker = (resolve_ticker_query(raw) or raw).strip().upper()
        date = (date or "").strip() or timeutil.today()
        asset_type = self.detect_asset_type(ticker)
        timeframe = (timeframe or _DEFAULT_TIMEFRAME).strip() or _DEFAULT_TIMEFRAME
        allowed = timeframes_for_asset(asset_type)
        if timeframe not in allowed:
            raise ValueError(
                f"timeframe {timeframe!r} indisponível para {asset_type} "
                f"(disponíveis: {', '.join(allowed)})"
            )
        run_id = timeutil.run_id_stamp() + "-cmp" + uuid.uuid4().hex[:4]
        crun = _CompareRun(run_id, ticker, date, asset_type, timeframe,
                           overrides=overrides)
        with self._lock:
            self._runs[run_id] = crun
        threading.Thread(target=self._compare_worker, args=(crun,), daemon=True).start()
        return run_id

    def _compare_worker(self, crun: _CompareRun) -> None:
        final_status = "error"
        tr = crun.tracker
        try:
            # Etapa 1 — Padrão. Marca 'running' antes de resolver; se voltou do cache
            # (DA-058: lado já existente NÃO re-roda), a etapa vira 'reused'.
            tr.step("padrao", "running")
            tr.set("Padrão", "Resolvendo a leitura Padrão…", 8)
            padrao_rec = self._resolve_side(crun, want_erick=False)
            tr.step("padrao", "reused" if padrao_rec.get("_reused") else "done")
            # Etapa 2 — método Erick (mesma regra de cache).
            tr.step("erick", "running")
            tr.set("Erick", "Resolvendo a leitura pelo método Erick…", 55)
            erick_rec = self._resolve_side(crun, want_erick=True)
            tr.step("erick", "reused" if erick_rec.get("_reused") else "done")
            # Etapa 3 — meta-juiz (sempre roda: é o confronto das duas leituras).
            tr.step("meta", "running")
            tr.set("Meta-juiz", "Confrontando as duas leituras…", 92)
            col_a = build_column(padrao_rec, "padrao")
            col_b = build_column(erick_rec, "erick")
            meta = self._meta_judge(col_a, col_b, crun.asset_type)
            tr.step("meta", "done")
            crun.result = {
                "compare": {"a": col_a, "b": col_b, "meta": meta},
                "verdict": meta.get("verdict"),
                "verdict_timeframe": crun.timeframe,
                "asset_type": crun.asset_type,
            }
            crun.tracker.done()
            final_status = "done"
        except Exception as exc:  # surface, never crash the server
            # Erro humano no confronto (cada lado já roda por _execute, que trata os
            # seus; este cobre falha do próprio orquestrador/meta-juiz). SEM stack
            # nem chave na UI; técnico redigido só no log.
            secret = crun.overrides.get("api_key")
            provider = (apply_llm_overrides(self.base_config, crun.overrides)
                        .get("llm_provider") or "").lower()
            crun.error = _clean_error(exc, provider, secret)
            crun.error_code = _error_code(exc, provider, secret)
            crun.result = None
            logger.warning(
                "compare %s falhou: %s", crun.run_id,
                _redact_secret(traceback.format_exc()[-3000:], secret),
            )
        crun.finished_at = time.time()
        crun.finished_stamp = timeutil.stamp()
        self._persist_compare(crun, final_status)
        crun.status = final_status

    def _resolve_side(self, crun: _CompareRun, want_erick: bool) -> dict[str, Any]:
        """Return the full record for one side of the comparison — reusing a
        cached prior run for (ticker, date, timeframe, method) when present, else
        running that pipeline fresh (inline, in this worker thread)."""
        existing = self._find_reusable(
            crun.ticker, crun.date, crun.timeframe, want_erick
        )
        if existing is not None:
            existing = dict(existing)
            existing["_reused"] = True
            return existing
        selected = select_analysts_for_asset(crun.asset_type, include_erick=want_erick)
        sub_id = timeutil.run_id_stamp() + "-" + uuid.uuid4().hex[:6]
        sub = _Run(sub_id, crun.ticker, crun.date, crun.asset_type, selected,
                   timeframe=crun.timeframe, overrides=crun.overrides)
        # Not registered in ``_runs``: the compare run's coarse progress already
        # covers it, so it should not appear as a second live item. It is still
        # persisted below, so it shows in history and is reusable afterwards.
        status = self._execute(sub)
        self._persist(sub, status)
        sub.status = status
        return self._record(sub_id) or {
            "run_id": sub_id, "ticker": crun.ticker, "date": crun.date,
            "asset_type": crun.asset_type, "status": status, "error": sub.error,
            "verdict_timeframe": crun.timeframe, "result": sub.result,
            "cost": sub.cost(),
        }

    def _find_reusable(self, ticker: str, date: str, timeframe: str,
                       want_erick: bool) -> dict[str, Any] | None:
        """Most-recent DONE plain run matching (ticker, date, timeframe) whose
        Erick-presence matches ``want_erick`` — or ``None``. Compare runs are
        skipped (they are not a plain padrão/erick reading)."""
        for summ in self.store.recent(30):
            if summ.get("status") != "done":
                continue
            if (summ.get("ticker") or "").upper() != ticker.upper():
                continue
            if (summ.get("date") or "") != date:
                continue
            if (summ.get("verdict_timeframe") or _DEFAULT_TIMEFRAME) != timeframe:
                continue
            rec = self._record(summ["run_id"])
            if not rec or rec.get("status") != "done":
                continue
            res = rec.get("result") or {}
            if res.get("compare"):
                continue  # a comparison record is not a single-method reading
            # Nem o ATALHO 1-2-3: ele grava relatório vazio e veredito None, então a
            # detecção por ausência de ``erick_report`` o dava como "padrao" — e o
            # confronto reusava um registro EM BRANCO como o lado Padrão, mandando o
            # meta-juiz comparar nada com um Erick real. Uma leitura estrutural não é
            # uma leitura de método — vale para o 1-2-3 e para o Storm.
            if res.get("setup123") or res.get("storm123"):
                continue
            has_erick = bool((res.get("erick_report") or "").strip())
            # Invalidação de 1º deploy (task 005): registro erick pré-coerência (sem
            # ``drop_nature``) não é reusável — reapareceria com o Estado antigo.
            if has_erick and "drop_nature" not in res:
                continue
            if has_erick == want_erick:
                return rec
        return None

    def _persist_compare(self, crun: _CompareRun, status: str) -> None:
        try:
            cost = crun.cost()
            elapsed = round((crun.finished_at or time.time()) - crun.started_at, 1)
            record = {
                "run_id": crun.run_id,
                "ticker": crun.ticker,
                "date": crun.date,
                "asset_type": crun.asset_type,
                "status": status,
                "error": crun.error,
                "error_code": crun.error_code,
                "verdict": (crun.result or {}).get("verdict") if status == "done" else None,
                "verdict_timeframe": crun.timeframe,
                "method": "compare",  # not a single reading — excluded from confront picker
                "cost_usd": cost["usd"],
                "elapsed": elapsed,
                "finished_at": crun.finished_stamp or timeutil.stamp(),
                "result": crun.result,
                "cost": cost,
                "compare": True,  # marks this record as a comparison
            }
            self.store.save(record)
        except Exception:
            pass

    def _record(self, run_id: str) -> dict[str, Any] | None:
        """Disk record for a run, with its ``degraded`` list normalized.

        The single door to :meth:`HistoryStore.get` — records written before the
        structured-entry fix (task 20260828-003) still carry the free-text note,
        and this path feeds the UI, the confronto AND the reuse lookup, so a stale
        record would otherwise carry the nameless "fonte" placeholder forward into
        brand-new runs.
        """
        rec = self.store.get(run_id)
        if isinstance(rec, dict):
            normalize_result(rec.get("result"))
        return rec

    def _load_record(self, run_id: str) -> dict[str, Any] | None:
        """Full record for a run — from the live table (its snapshot) or disk."""
        with self._lock:
            run = self._runs.get(run_id)
        if run is not None:
            snap = run.snapshot()
            return {
                "run_id": snap.get("run_id"), "ticker": snap.get("ticker"),
                "date": snap.get("date"), "asset_type": snap.get("asset_type"),
                "status": snap.get("status"), "error": snap.get("error"),
                "verdict_timeframe": snap.get("verdict_timeframe"),
                "verdict": (snap.get("result") or {}).get("verdict"),
                "cost": snap.get("cost"), "elapsed": snap.get("elapsed"),
                "result": snap.get("result"),
            }
        return self._record(run_id)

    def confront(self, id_a: str, id_b: str,
                 overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        """Confront two analyses of the same ticker — ALWAYS Padrão × Erick on the
        same timeframe/date (Samyr's rule, task 024). Never 'método contra ele mesmo'.

        If the two picked runs already are a valid Padrão × Erick pair on the same
        frame and date, meta-judge them directly (free, no re-run, the exact runs
        chosen). Anything else — two of the same method, or mismatched frames/dates —
        is NOT a confront: it reroutes through :meth:`start_compare`, anchored on the
        open run A, which reuses the cached side and runs ONLY the missing method so
        the outcome is a true Padrão × Erick.

        Returns a done snapshot (``result.compare`` populated) for the direct case,
        or ``{"run_id": ..., "rerouted": True}`` for the rerouted (async) case — the
        caller polls that run_id like any other.
        """
        if not id_a or not id_b:
            raise ValueError("selecione duas análises")
        if id_a == id_b:
            raise ValueError("selecione duas análises diferentes")
        rec_a, rec_b = self._load_record(id_a), self._load_record(id_b)
        if rec_a is None or rec_b is None:
            raise ValueError("análise não encontrada")
        ta = (rec_a.get("ticker") or "").upper()
        tb = (rec_b.get("ticker") or "").upper()
        if ta != tb or not ta:
            raise ValueError("as duas análises precisam ser do mesmo ativo")
        if detect_method(rec_a) == "compare" or detect_method(rec_b) == "compare":
            raise ValueError("selecione análises simples, não uma comparação")

        col_a = build_column(rec_a, detect_method(rec_a))
        col_b = build_column(rec_b, detect_method(rec_b))

        # Not a real confront (same method, or different frame/date) → reroute to a
        # true Padrão × Erick compare, anchored on run A (ticker/date/frame the user
        # is looking at). start_compare reuses the cached side and runs only the
        # missing method — impossible to produce método×ele-mesmo by this door.
        if not confront_pair_valid(col_a, col_b):
            run_id = self.start_compare(
                ta, rec_a.get("date") or "",
                timeframe=col_a.get("timeframe") or _DEFAULT_TIMEFRAME,
                overrides=overrides,
            )
            return {"run_id": run_id, "rerouted": True, "ticker": ta,
                    "status": "running"}

        # Valid pair: keep Padrão first / Erick second so the header always reads
        # "Padrão · X × Método Erick · X" regardless of which side was picked first.
        if col_a.get("method") != "padrao":
            col_a, col_b = col_b, col_a
        asset_type = rec_a.get("asset_type") or rec_b.get("asset_type") or "stock"
        meta = self._meta_judge(col_a, col_b, asset_type)

        run_id = timeutil.run_id_stamp() + "-cmp" + uuid.uuid4().hex[:4]
        crun = _CompareRun(run_id, ta, col_a.get("date") or rec_a.get("date") or "",
                           asset_type, col_a.get("timeframe") or _DEFAULT_TIMEFRAME)
        crun.result = {
            "compare": {"a": col_a, "b": col_b, "meta": meta, "manual": True},
            "verdict": meta.get("verdict"),
            "verdict_timeframe": crun.timeframe,
            "asset_type": asset_type,
        }
        crun.tracker.done()
        crun.finished_at = time.time()
        crun.finished_stamp = timeutil.stamp()
        crun.status = "done"
        self._persist_compare(crun, "done")
        return crun.snapshot()

    # ------------------------------------------------------- Q&A ancorado ----
    def ask(self, run_id: str, question: str,
            overrides: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Responde uma pergunta ANCORADA nos dados JÁ computados de uma run.

        Não re-roda a análise nem toca em dado externo: monta o contexto
        (price_structure + veredito + relatórios que a run já cacheou) e chama o
        modelo BARATO (``quick_think_llm``) UMA vez, medindo o custo. O grounding
        e a honestidade ("não dá pra afirmar") vivem no prompt de
        :mod:`tradingagents.webui.ask`. Retorna ``None`` se a run é desconhecida;
        levanta ``ValueError`` se a pergunta vier vazia."""
        question = (question or "").strip()
        if not question:
            raise ValueError("faça uma pergunta")
        question = question[:_MAX_QUESTION_CHARS]
        record = self._load_record(run_id)
        if record is None:
            return None

        # Provedor owner-only (assinatura do dono, ex.: claude-cli): idem análise —
        # a pergunta ancorada só roda pro dono logado.
        _ask_cfg = apply_llm_overrides(self.base_config, overrides)
        if _owner_only_blocked(_ask_cfg, overrides or {}):
            return {
                "run_id": record.get("run_id") or run_id, "question": question,
                "error": _OWNER_ONLY_MESSAGE, "error_code": _OWNER_ONLY_CODE,
            }
        # Gating da chave do servidor (idem análise): requisição pública explícita
        # sem chave própria não roda a pergunta — nunca usa a env.
        if (not _ask_cfg.get("llm_api_key")
                and (overrides or {}).get("allow_server_key") is False):
            return {
                "run_id": record.get("run_id") or run_id, "question": question,
                "error": NEED_KEY_MESSAGE, "error_code": NEED_KEY_CODE,
            }

        messages, meta = ask_module.build_messages(record, question)
        usage_cb = UsageMetadataCallbackHandler()
        llm = self._answer_llm([usage_cb], overrides)
        try:
            reply = llm.invoke(messages)
        except Exception as exc:
            # Mesmo tratamento humano da análise: a caixa de pergunta mostra a frase
            # acionável (429 sem crédito, 401 chave inválida…), SEM stack nem chave.
            secret = (overrides or {}).get("api_key")
            provider = (apply_llm_overrides(self.base_config, overrides)
                        .get("llm_provider") or "").lower()
            logger.warning(
                "ask %s falhou: %s", run_id,
                _redact_secret(f"{type(exc).__name__}: {exc}", secret),
            )
            return {
                "run_id": record.get("run_id") or run_id,
                "question": question,
                "error": _clean_error(exc, provider, secret),
                "error_code": _error_code(exc, provider, secret),
            }
        answer = getattr(reply, "content", reply)
        # Alguns provedores devolvem o conteúdo em "partes" (lista) em vez de texto.
        if isinstance(answer, list):
            answer = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in answer
            )
        return {
            "run_id": record.get("run_id") or run_id,
            "question": question,
            "answer": str(answer).strip(),
            "cost": cost_breakdown(usage_cb.usage_metadata),
            "model": apply_llm_overrides(self.base_config, overrides).get("quick_think_llm"),
            "mode": meta.get("mode"),
            "grounded": bool(meta.get("has_numbers")),
            "timeframe": meta.get("timeframe"),
            "as_of": meta.get("as_of"),
        }

    def _answer_llm(self, callbacks: list, overrides: dict[str, Any] | None = None):
        """Chat client BARATO pra responder perguntas (``quick_think_llm``), com os
        mesmos knobs de provedor da run e os callbacks de uso pra medir o custo.

        BYOK: usa a config efetiva (chave/provider/modelo do usuário têm prioridade;
        sem chave, cai na env do servidor). A ``api_key`` vai como kwarg explícito
        do client — jamais entra em estado global ou é persistida."""
        from tradingagents.llm_clients import create_llm_client
        cfg = apply_llm_overrides(self.base_config, overrides)
        kwargs: dict[str, Any] = {"callbacks": callbacks}
        provider = (cfg.get("llm_provider") or "openai").lower()
        if cfg.get("llm_api_key"):
            kwargs["api_key"] = cfg["llm_api_key"]
        temperature = cfg.get("temperature")
        if temperature is not None and temperature != "":
            kwargs["temperature"] = float(temperature)
        if provider == "openai" and cfg.get("openai_reasoning_effort"):
            kwargs["reasoning_effort"] = cfg["openai_reasoning_effort"]
        elif provider == "anthropic" and cfg.get("anthropic_effort"):
            kwargs["effort"] = cfg["anthropic_effort"]
        elif provider == "google" and cfg.get("google_thinking_level"):
            kwargs["thinking_level"] = cfg["google_thinking_level"]
        max_retries = cfg.get("llm_max_retries")
        if max_retries is not None and max_retries != "":
            kwargs["max_retries"] = int(max_retries)
        client = create_llm_client(
            provider=provider,
            model=cfg["quick_think_llm"],
            base_url=cfg.get("backend_url"),
            **kwargs,
        )
        return client.get_llm()

    def test_key(self, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        """Valida a chave/config efetiva com UMA chamada barata (sem rodar análise).

        Monta o client do modelo BARATO com a config efetiva (chave do usuário >
        env do servidor) e faz um único ``invoke`` mínimo. ``ok`` diz se a chave
        autenticou; em erro, a mensagem é REDIGIDA da chave. Não persiste nada e
        não usa estado global — a ``api_key`` é kwarg do client, só em memória.
        ``max_retries=0`` pra uma chave inválida falhar na hora (401), sem espera."""
        from tradingagents.llm_clients import create_llm_client
        cfg = apply_llm_overrides(self.base_config, overrides)
        provider = (cfg.get("llm_provider") or "openai").lower()
        model = cfg.get("quick_think_llm")
        secret = cfg.get("llm_api_key")
        # Requisição pública explícita sem chave própria não tem o que testar — a
        # chave do servidor é só do dono, jamais exposta a um "testar" público.
        if not secret and (overrides or {}).get("allow_server_key") is False:
            return {"ok": False, "provider": provider, "model": model,
                    "using_user_key": False,
                    "error": NEED_KEY_MESSAGE, "error_code": NEED_KEY_CODE}
        kwargs: dict[str, Any] = {"max_retries": 0}
        if secret:
            kwargs["api_key"] = secret
        info = {"provider": provider, "model": model,
                "using_user_key": bool(secret)}
        try:
            client = create_llm_client(
                provider=provider, model=model,
                base_url=cfg.get("backend_url"), **kwargs,
            )
            llm = client.get_llm()
            reply = llm.invoke("ping")
            _ = getattr(reply, "content", reply)  # força a resolução da resposta
            return {"ok": True, **info}
        except Exception as exc:
            raw = _redact_secret(f"{type(exc).__name__}: {exc}", secret)
            human = humanize_provider_error(raw, provider)
            return {"ok": False, **info,
                    "error": human["message"] if human else raw,
                    "error_code": human["code"] if human else None}

    def _ping_model(self, provider: str, model: str | None, secret: str | None,
                    base_url: str | None) -> dict[str, Any]:
        """Pinga UM modelo com um prompt trivial e mede a latência REAL do ``invoke``.

        Uma chamada minúscula (``_MODEL_TEST_PROMPT``, poucos tokens) — confirma que o
        modelo responde e cronometra a resposta, SEM rodar análise nem persistir nada.
        ``max_retries=0`` pra um modelo inexistente/chave inválida falhar na hora, sem
        espera. Sucesso → ``ok`` + ``latency_ms`` + ``sample`` (trecho curto); erro →
        mensagem humana (mapa da 041) já REDIGIDA da chave. Nunca loga/expõe a chave."""
        from tradingagents.llm_clients import create_llm_client
        info: dict[str, Any] = {"model": model}
        if not model:
            # provider sem modelo nomeado (custom-only): não há o que pingar.
            return {**info, "ok": False,
                    "error": "Escolha um modelo antes de testar.",
                    "error_code": "no_model"}
        kwargs: dict[str, Any] = {"max_retries": 0}
        if secret:
            kwargs["api_key"] = secret
        try:
            client = create_llm_client(
                provider=provider, model=model, base_url=base_url, **kwargs,
            )
            llm = client.get_llm()
            started = time.perf_counter()
            reply = llm.invoke(_MODEL_TEST_PROMPT)
            latency_ms = int((time.perf_counter() - started) * 1000)
            content = getattr(reply, "content", reply)
            # Alguns provedores devolvem o conteúdo em "partes" (lista) em vez de texto.
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                )
            sample = " ".join(str(content).split())[:_MODEL_TEST_SAMPLE_CHARS]
            return {**info, "ok": True, "latency_ms": latency_ms, "sample": sample}
        except Exception as exc:
            raw = _redact_secret(f"{type(exc).__name__}: {exc}", secret)
            human = humanize_provider_error(raw, provider)
            return {**info, "ok": False,
                    "error": human["message"] if human else raw,
                    "error_code": human["code"] if human else None}

    def test_model(self, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        """Pinga o modelo RÁPIDO e o PESADO escolhidos e devolve latência de cada.

        O ``/api/test-key`` só valida a chave (e lista modelos); aqui a pergunta é
        outra — "o modelo que escolhi responde, e quão rápido?", SEM esperar os 12+
        min da análise. Manda ``_MODEL_TEST_PROMPT`` (trivial) pra cada modelo com a
        config efetiva (chave do usuário > env do servidor) e mede a latência real.
        Testa os dois porque podem ser modelos/velocidades diferentes (o caso do
        brief: OpenRouter/GLM lento). Não cria run, não persiste, chave só em memória
        e nunca logada/ecoada. Requisição pública sem chave própria → ``need_key``."""
        cfg = apply_llm_overrides(self.base_config, overrides)
        provider = (cfg.get("llm_provider") or "openai").lower()
        quick = cfg.get("quick_think_llm")
        deep = cfg.get("deep_think_llm")
        secret = cfg.get("llm_api_key")
        base_url = cfg.get("backend_url")
        # Cross-provider por nível (task 027/014): no avançado cada nível pinga o SEU
        # provedor — senão "Testar modelo" testava tudo no provedor-base (ex.: Rápido=
        # claude-cli mas pingava no OpenAI → falso erro de crédito). Fora do avançado,
        # os dois caem no provedor único (quick_/deep_think_provider = None → base).
        quick_provider = (cfg.get("quick_think_provider") or provider).lower()
        deep_provider = (cfg.get("deep_think_provider") or provider).lower()
        # Owner-only em profundidade (task 014/030): a assinatura claude-cli é do DONO
        # (proxy server-side). Um público com chave BYOK NÃO pode "testar" claude-cli
        # (simples OU por-nível) e gastar a cota do dono. Barra antes de pingar.
        if ({provider, quick_provider, deep_provider} & _OWNER_ONLY_PROVIDERS) \
                and (overrides or {}).get("allow_server_key") is not True:
            return {"ok": False, "provider": provider, "using_user_key": bool(secret),
                    "error": "acesso restrito ao dono", "error_code": _OWNER_ONLY_CODE,
                    "models": []}
        # Público explícito sem chave própria não testa — a chave do servidor é só do
        # dono, jamais exposta (nem gasta) num "testar" público. Não pinga nada.
        if not secret and (overrides or {}).get("allow_server_key") is False:
            return {"ok": False, "provider": provider, "using_user_key": False,
                    "error": NEED_KEY_MESSAGE, "error_code": NEED_KEY_CODE, "models": []}
        # Rápido primeiro (o mais provável de responder), pesado depois. Cada item traz
        # role/label pra UI rotular ("Rápido" / "Pesado") sem adivinhar. A chave
        # BYOK pertence ao provedor-base; um nível cujo provedor difere dele resolve a
        # própria credencial (assinatura/env) — não recebe a chave do outro.
        models = []
        for role, label, model, lvl_provider in (
            ("quick", "rápido", quick, quick_provider),
            ("deep", "pesado", deep, deep_provider),
        ):
            lvl_secret = secret if lvl_provider == provider else None
            # endpoint do NÍVEL (task 017): Ollama no Rápido + OpenAI no Pesado não
            # podem compartilhar base_url — pingar no endereço errado dava falso erro.
            lvl_base = cfg.get(f"{role}_backend_url") or base_url
            res = self._ping_model(lvl_provider, model, lvl_secret, lvl_base)
            models.append({"role": role, "label": label, "provider": lvl_provider, **res})
        return {"ok": all(m["ok"] for m in models), "provider": provider,
                "using_user_key": bool(secret), "models": models}

    def status(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            run = self._runs.get(run_id)
        if run is not None:
            snap = run.snapshot()
            # PARAR/PAUSAR (task 026): a UI decide os botões pelo snapshot — Parar
            # sempre; Pausar só quando a run é retomável (não-BYOK + checkpoint).
            if hasattr(run, "cancel_event"):
                snap["cancellable"] = (run.status == "running")
                snap["resumable"] = self._run_is_resumable(run)
            # Atualizar etapa em voo (task 002): estado honesto do intervalo entre a
            # pausa e a re-entrada — a UI não trata como "run pausada e acabou".
            with self._lock:
                pending = self._refreshing.get(run_id)
            if pending:
                snap["refreshing"] = dict(pending)
            return snap
        # fall back to persisted history for a run this process didn't start
        record = self._record(run_id)
        if record is None:
            return None
        return {
            "run_id": record["run_id"],
            "ticker": record.get("ticker"),
            "date": record.get("date"),
            "asset_type": record.get("asset_type"),
            "status": record.get("status"),
            "error": record.get("error"),
            "error_code": record.get("error_code"),
            "verdict_timeframe": record.get("verdict_timeframe")
                or (record.get("result") or {}).get("verdict_timeframe")
                or _DEFAULT_TIMEFRAME,
            "progress": {"percent": 100, "phase": "Concluído", "label": "Do histórico",
                          "index": 0, "total": 0, "elapsed": record.get("elapsed"),
                          "plan": [], "reached": []},
            "cost": record.get("cost", {"usd": record.get("cost_usd", 0)}),
            "elapsed": record.get("elapsed"),
            "result": record.get("result"),
            "from_history": True,
        }

    @staticmethod
    def _running_summary(run: _Run) -> dict[str, Any]:
        """History-shaped summary for a run still executing in this process.

        Same keys the on-disk index carries (so the UI renders it with the same
        code path) plus a light ``progress`` (percent + phase) for the live
        em-andamento marker. The full progress feed still comes from
        ``/api/status/<run_id>`` when the run is opened.
        """
        prog = run.tracker.snapshot()
        cost = run.cost()
        return {
            "run_id": run.run_id,
            "ticker": run.ticker,
            "date": run.date,
            "asset_type": run.asset_type,
            "status": "running",
            "verdict": None,
            # method + setup_state no resumo ao vivo igual ao index persistido
            # (task 010): a watchlist usa o mesmo código pra done e running.
            "method": getattr(run, "method", None)
            or ("erick" if "erick" in run.selected_analysts else "padrao"),
            "setup_state": ((run.result or {}).get("actionable") or {}).get("setup_state"),
            "cost_usd": cost.get("usd", 0),
            "elapsed": round(time.time() - run.started_at, 1),
            "finished_at": None,
            "progress": {"percent": prog.get("percent", 0), "phase": prog.get("phase", "")},
        }

    def active_runs(self) -> list[dict[str, Any]]:
        """Summaries for the runs still executing in THIS process, newest-first.

        Each analysis runs on its own daemon thread and keeps computing even after
        the client navigates to another ticker, reloads the page, or closes the
        tab. Those live runs are not in the on-disk history yet (that write happens
        only on completion), so without surfacing them the UI has no way to show an
        "em andamento" marker or re-open a running analysis. Merged in front of the
        persisted history by :meth:`history`.
        """
        with self._lock:
            live = [r for r in self._runs.values() if r.status == "running"]
        live.sort(key=lambda r: r.started_at, reverse=True)
        return [self._running_summary(r) for r in live]

    def history(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Lista de OBSERVAÇÃO: UM item por ticker já pesquisado (persistente, só
        cresce — task 011), com o run mais recente daquele ticker, mais os runs em
        andamento (status ``running``) sobrepostos no topo.

        Antes devolvia só os ``limit`` runs mais recentes: um ativo pesquisado há
        tempo sumia quando seus runs saíam da janela. Agora varre o index inteiro
        (``store.watchlist()``) — nenhum ticker cai por causa de limite de runs. Um
        ticker em andamento aparece "rodando", preservando a contagem de análises já
        persistidas. ``limit`` é ignorado (mantido por compatibilidade de assinatura).
        """
        wl = self.store.watchlist()
        count_by_ticker = {
            (w.get("ticker") or "").upper(): w.get("count", 1) for w in wl
        }
        live = self.active_runs()
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for lr in live:                       # runs em andamento no topo
            t = (lr.get("ticker") or "").upper()
            if t in seen:
                out.append(lr)                # 2 runs do mesmo ticker rodando: mantém os dois
                continue
            seen.add(t)
            # +1: o próprio run em andamento (ainda não persistido no index)
            out.append({**lr, "count": count_by_ticker.get(t, 0) + 1})
        for w in wl:                          # os demais tickers da watchlist
            t = (w.get("ticker") or "").upper()
            if t in seen:
                continue
            seen.add(t)
            out.append(w)
        return out

    def delete_ticker(self, ticker: str) -> int:
        """Remove do histórico persistido todas as análises de um ticker (a lista
        lateral é por ativo). Runs em andamento (em memória) não são tocados —
        eles voltam à lista ao terminar. Retorna quantas foram removidas."""
        return self.store.delete_ticker(ticker)

    def live_prices(self, tickers: list[str]) -> dict[str, Any]:
        """Preço LIVE por ticker pra 3ª linha da watchlist: ``sym -> {price,
        change_pct, currency}`` ou ``None`` quando a fonte cai (a UI mostra "—").

        Cacheado ~45s por símbolo (``_PRICE_TTL``): só busca os que expiraram, então
        chamadas repetidas dos tickers visíveis não martelam o yfinance. NUNCA roda
        o pipeline — é só o quote rápido (``fetch_live_price``).
        """
        from tradingagents.dataflows.live_price import fetch_live_price

        out: dict[str, Any] = {}
        now = time.monotonic()
        to_fetch: list[str] = []
        with self._price_lock:
            for raw in tickers:
                key = (raw or "").strip().upper()
                if not key or key in out or key in to_fetch:
                    continue
                hit = self._price_cache.get(key)
                if hit and (now - hit[0]) < _PRICE_TTL:
                    out[key] = hit[1]
                else:
                    to_fetch.append(key)
        for key in to_fetch:
            payload = fetch_live_price(key)
            out[key] = payload
            with self._price_lock:
                self._price_cache[key] = (time.monotonic(), payload)
        return out

    def search_symbols(self, term: str, limit: int = 8) -> list[dict[str, Any]]:
        """Autocomplete candidates for a name-or-ticker term (fail-open)."""
        return fetch_symbol_search(term, limit)

    # ------------------------------------------------- watchlist + scan 1-2-3 ----
    def watchlist_get(self) -> list[dict[str, Any]]:
        """A watchlist manual do scan (semeada do histórico na primeira vez)."""
        return self.watchlist_store.get()

    def watchlist_add(self, ticker: str) -> list[dict[str, Any]]:
        return self.watchlist_store.add(ticker)

    def watchlist_remove(self, ticker: str) -> list[dict[str, Any]]:
        return self.watchlist_store.remove(ticker)

    def watchlist_set(self, tickers: list) -> list[dict[str, Any]]:
        return self.watchlist_store.set([str(t) for t in tickers])

    def scan_portfolio(self, date: str) -> dict[str, Any]:
        """Varre a watchlist (1d+4h+1h) — $0 de LLM, só plano determinístico.

        Todo ``em_gatilho`` é LOGADO (dedup por ticker+frame+gatilho: o mesmo
        setup não re-entrega) — é o insumo do track record do scan.

        **Uma varredura por vez (single-flight).** O servidor é threaded e não sabe
        que o cliente desistiu: um usuário que cansou de esperar e reclicou disparava
        uma SEGUNDA varredura enquanto a primeira ainda batia no provedor, dobrando
        as chamadas e cobrando throttle — a explicação mais provável pro outlier de
        75s que a revisão mediu. Agora o segundo pedido ESPERA o primeiro e recebe o
        resultado dele (se ainda fresco, :data:`_SCAN_MEMO_TTL`), em vez de somar
        pressão em cima de uma fonte que já está reclamando.
        """
        with self._scan_lock:
            memo = self._scan_memo
            if memo and memo[0] == date and time.time() - memo[1] < _SCAN_MEMO_TTL:
                return memo[2]
            tickers = [w.get("ticker") for w in self.watchlist_store.get() if w.get("ticker")]
            result = scan_watchlist(tickers, date)
            # A chave de "já logado" carrega o SETUP: o mesmo ativo/frame pode estar
            # em gatilho nos DOIS setups ao mesmo tempo, com gatilhos diferentes, e
            # sem o setup na chave o segundo seria descartado como repetido do
            # primeiro (ou pior: com gatilhos iguais por coincidência, um sumiria).
            known = {(_setup_da_entrada(e), e.get("ticker"), e.get("frame"), e.get("trigger"))
                     for e in self.scan_log.entries()}
            for s in result.get("ativos", []):
                for f in s.get("frames", []):
                    if (f.get("estado") == "em_gatilho"
                            and ("123", s["ticker"], f.get("frame"), f.get("trigger")) not in known):
                        self.scan_log.record({**f, "ticker": s["ticker"], "setup": "123"})
                    # O STORM loga o SEU gatilho, com a SUA identidade — e só quando o
                    # Éden autoriza: gatilho que a regra proíbe operar não é trade, e
                    # jogá-lo no ledger contaminaria a taxa de acerto com o que
                    # ninguém teria operado.
                    st = f.get("storm") or {}
                    if (st.get("estado") == "em_gatilho" and st.get("opera")
                            and ("storm", s["ticker"], f.get("frame"), st.get("trigger")) not in known):
                        self.scan_log.record({**st, "ticker": s["ticker"],
                                              "frame": f.get("frame"), "setup": "storm"})
            self._scan_memo = (date, time.time(), result)
            return result

    def scan_track_record(self, date: str) -> dict[str, Any]:
        """Re-avalia os gatilhos logados contra o preço da data dada.

        A lista da watchlist NÃO entra: o track record é do que foi LOGADO, e tirar
        um ticker da watchlist não apaga o trade que já aconteceu. O parâmetro era
        recebido e ignorado — a lista era montada a cada chamada pra nada.
        """
        return scan_verdicts(self.scan_log, date)

    def resolve_names(self, symbols: list[str]) -> dict[str, str]:
        """Batch symbol -> display name for the UI chips/header (fail-open)."""
        return fetch_symbol_names(symbols)

    def config_info(self) -> dict[str, Any]:
        """Client bootstrap: the authoritative *Manaus* today + tz for the UI.

        The date selector defaults to this, not to the browser's clock, so a
        laptop set to another timezone still runs under Samyr's day.
        """
        return {
            "today": timeutil.today(),
            "now": timeutil.stamp(),
            "tz": timeutil.TZ_NAME,
            "tz_label": timeutil.TZ_LABEL,
            # BYOK: o front usa isto pra montar a config de chaves. NUNCA devolve
            # a chave em si — só o provider/modelo padrão do servidor (o fallback)
            # e SE existe env de fallback pro provider default (sem revelar valor).
            "llm": self._llm_config_info(),
        }

    # Provedores oferecidos na UI de config (BYOK). Campos:
    # ``(id, label, needs_base_url, key_optional, owner_only)``.
    # ``needs_base_url`` marca os que exigem endpoint (Ollama/self-host);
    # ``key_optional`` os que rodam sem chave (Ollama local, assinatura);
    # ``owner_only`` os que usam credencial do SERVIDOR/assinatura — só o dono logado
    # (o front esconde do público; o servidor barra em profundidade).
    # Os defaults de modelo saem do catálogo (_provider_default_models).
    _BYOK_PROVIDERS = (
        ("openai", "OpenAI", False, False, False),
        # Assinatura Claude via CLI OAuth (task 20260826-030): custo/token = $0, sem
        # chave — a auth é server-side (proxy). Só o dono (usa a assinatura dele). Vem
        # ANTES do Anthropic pago (task 014) pra ser a escolha ÓBVIA de Claude pro dono.
        ("claude-cli", "Claude — assinatura ($0/token · dono)", False, True, True),
        ("anthropic", "Anthropic (Claude) — chave paga", False, False, False),
        ("openrouter", "OpenRouter", False, False, False),
        ("ollama", "Ollama / Llama (local)", True, True, False),
        ("google", "Google (Gemini)", False, False, False),
        ("deepseek", "DeepSeek", False, False, False),
        ("xai", "xAI (Grok)", False, False, False),
        ("openai_compatible", "OpenAI-compatível (self-host)", True, True, False),
    )

    def _llm_config_info(self) -> dict[str, Any]:
        """Metadados de LLM pro front (BYOK) — sem jamais expor chave alguma."""
        from tradingagents.llm_clients.api_key_env import get_api_key_env
        cfg = self.base_config
        default_provider = (cfg.get("llm_provider") or "openai").lower()
        providers = []
        for pid, label, needs_base_url, key_optional, owner_only in self._BYOK_PROVIDERS:
            deep, quick = _provider_default_models(pid)
            key_env = get_api_key_env(pid)
            providers.append({
                "id": pid,
                "label": label,
                "needs_base_url": needs_base_url,
                "key_optional": key_optional,
                # Provedor de assinatura/servidor: o front só mostra pro dono logado.
                "owner_only": owner_only,
                "default_deep": deep,
                "default_quick": quick,
                # Modelos do catálogo curado (task 014): o front popula o dropdown
                # DIRETO daqui ao trocar de provedor — sem esperar /models e sem
                # mismatch (ex.: Anthropic com modelo OpenAI). [] = custom-only.
                "models": _provider_catalog_models(pid),
                # Regras de FORMATO do id deste provedor (task 016): o front usa
                # pra normalizar o modelo ao trocar de provedor com a MESMA regra
                # do backend (nada de "anthropic/claude-opus-5" num claude-cli).
                "id_format": id_format_meta(pid),
                # Presença (não o valor) da env de fallback no servidor: deixa o
                # front dizer "sem chave → usa a do servidor" só quando é verdade.
                # claude-cli não usa env de key (auth server-side via proxy).
                "server_key": bool(key_env and os.environ.get(key_env)),
            })
        return {
            "default_provider": default_provider,
            "default_deep": cfg.get("deep_think_llm"),
            "default_quick": cfg.get("quick_think_llm"),
            "default_base_url": cfg.get("backend_url"),
            "providers": providers,
        }

    def timeframe_view(self, ticker: str, date: str, timeframe: str,
                       method: str = "padrao") -> dict[str, Any]:
        """Recompute the chart + actionable plan for ``timeframe`` on demand.

        Backs the ``/api/chart`` endpoint the timeframe selector calls: the region,
        1-2-3 and bands are re-detected on the chosen frame's own series (daily from
        the cached yfinance series; 4h/1h/15m from the keyless intraday source —
        the exchange for crypto, yfinance for an equity). ``method`` keeps the
        structure family consistent with the open analysis (Erick EMA 8/21 / Padrão
        MMS) when the user flips timeframe. Everything reuses the DA-058 caches, so
        flipping back to a frame already fetched costs zero network.

        Honesty guards:

        * an unsupported frame (e.g. a ``3m`` that no asset offers) is a
          ``ValueError`` — the UI only renders the operable ladder;
        * an intraday **source outage** (feed down → empty candles, ``sem_dado``
          plan) is NOT fabricated: the view degrades to the daily frame and flags
          ``degraded`` with a pt-BR ``notice`` so the UI can say so plainly;
        * a frame the source genuinely has no candle for (``intradiario_indisponivel``
          — e.g. an equity backtest beyond yfinance's ~60-day intraday window) is
          left on the requested frame with an honest "indisponível" plan, not swapped.
        """
        ticker = (ticker or "").strip().upper()
        date = (date or "").strip() or timeutil.today()
        if not ticker:
            raise ValueError("ticker vazio")
        asset_type = self.detect_asset_type(ticker)
        allowed = timeframes_for_asset(asset_type)
        if timeframe not in allowed:
            raise ValueError(
                f"timeframe {timeframe!r} indisponível para {asset_type} "
                f"(disponíveis: {', '.join(allowed)})"
            )

        chart = fetch_price_chart(ticker, date, timeframe, method)
        plan = fetch_actionable_plan(ticker, date, timeframe, method)

        degraded = False
        notice: str | None = None
        requested = timeframe
        has_candles = bool((chart or {}).get("candles"))
        # An intraday frame that came back empty with a ``sem_dado`` plan means the
        # feed is momentarily down → degrade to the daily. A genuine
        # "intradiario_indisponivel" plan (the source truly has no candle for this
        # symbol/date, e.g. an out-of-window equity backtest) is expected and left
        # on the requested frame — the UI shows "indisponível", nothing is invented.
        if timeframe != _DEFAULT_TIMEFRAME and not has_candles \
                and (plan or {}).get("setup_state") != "intradiario_indisponivel":
            degraded = True
            timeframe = _DEFAULT_TIMEFRAME
            notice = (
                "Fonte intradiária indisponível agora — mostrando o diário. "
                "Nenhuma barra inventada."
            )
            chart = fetch_price_chart(ticker, date, timeframe, method)
            plan = fetch_actionable_plan(ticker, date, timeframe, method)

        return {
            "ticker": ticker,
            "date": date,
            "asset_type": asset_type,
            "timeframe": timeframe,
            "requested": requested,
            "timeframes": allowed,
            "degraded": degraded,
            "notice": notice,
            "price_chart": chart,
            "actionable": plan,
        }

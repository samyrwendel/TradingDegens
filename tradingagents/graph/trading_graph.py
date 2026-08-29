# TradingAgents/graph/trading_graph.py

import contextlib
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yfinance as yf
from langgraph.prebuilt import ToolNode

# Import the abstract tool methods from agent_utils
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_balance_sheet,
    get_cashflow,
    get_crypto_context,
    get_crypto_derivatives,
    get_fundamentals,
    get_global_news,
    get_income_statement,
    get_indicators,
    get_insider_transactions,
    get_macro_indicators,
    get_news,
    get_prediction_markets,
    get_price_timeframes,
    get_stock_data,
    get_verified_market_snapshot,
    resolve_instrument_identity,
)
from tradingagents.agents.utils.date_guard import base_date
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.dataflows import data_notices
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.utils import safe_ticker_component
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.llm_clients import create_llm_client
from tradingagents.reporting import write_report_tree

from .checkpointer import checkpoint_step, clear_checkpoint, get_checkpointer, thread_id
from .conditional_logic import ConditionalLogic
from .propagation import Propagator
from .reflection import Reflector
from .resilience import tool_error_message
from .setup import GraphSetup
from .signal_processing import SignalProcessor

logger = logging.getLogger(__name__)


def _coerce_max_retries(value):
    """Validate an ``llm_max_retries`` value to a non-negative int.

    Accepts an int or a numeric string (env vars arrive as strings). Rejects
    booleans and negatives loudly so a misconfiguration fails at startup rather
    than silently disabling retries.
    """
    if isinstance(value, bool):
        raise ValueError(f"llm_max_retries must be an integer, not a boolean: {value!r}")
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"llm_max_retries must be an integer, got {value!r}") from exc
    if n < 0:
        raise ValueError(f"llm_max_retries must be >= 0, got {n}")
    return n


# Levels the graph builds, in (config-prefix, model-key) form. deep = PESADO
# (pesquisa/juiz), quick = RÁPIDO (analistas/debate/trader/risco) — o mapa que o
# resto do motor já usa. Um único ponto de verdade pros dois níveis.
_LEVELS = {"deep": "deep_think_llm", "quick": "quick_think_llm"}


def resolve_level_specs(config: dict, byok_key: str | None = None) -> dict[str, dict]:
    """Resolve ``(provider, model, base_url, api_key)`` para cada nível de LLM.

    Cross-provider RÁPIDO/PESADO (task 027): cada nível pode rodar um provedor+modelo
    DIFERENTE. Provedor por-nível ausente → cai no único ``llm_provider`` (modo
    simples, inalterado). A chave BYOK pertence ao provedor-base e é repassada SÓ pro
    nível cujo provedor casa com ele — nunca vaza um provedor no outro (uma chave
    OpenAI jamais chega num client Anthropic). Retorna ``{"deep": {...}, "quick": {...}}``.
    """
    base_provider = (config.get("llm_provider") or "").lower()
    base_url = config.get("backend_url")
    specs: dict[str, dict] = {}
    for prefix, model_key in _LEVELS.items():
        provider = (config.get(f"{prefix}_think_provider") or base_provider).lower()
        specs[prefix] = {
            "provider": provider,
            "model": config.get(model_key),
            "base_url": config.get(f"{prefix}_backend_url") or base_url,
            "api_key": byok_key if provider == base_provider else None,
        }
    return specs


# Ordem PADRÃO da cadeia de fallback (o TAIL, o que vem DEPOIS do topo), começando
# pelo CLAUDE ($0/token). O TOPO da cadeia de cada nível é SEMPRE o provedor já
# configurado do nível (modo simples ou o seletor avançado 027 — inalterado); esta
# ordem é só o fallback quando o topo falha por estado do provedor. Extensível.
_DEFAULT_FALLBACK_ORDER = ("claude-cli", "openai")
# Teto de saltos por etapa (cadeia = 1 topo + até N fallbacks).
_DEFAULT_MAX_HOPS = 2
# claude via assinatura (proxy): não tem env-key; a auth é do proxy do dono.
_CLAUDE_CLI_PROVIDERS = frozenset({"claude-cli", "claude_cli", "claude-subscription"})


def _catalog_default_model(provider: str, level: str) -> str | None:
    """Modelo padrão do catálogo pra ``(provider, level)`` — usado só nos LINKS de
    fallback (o topo já traz o modelo da config). ``None`` se o provedor não tem
    catálogo (o link é descartado antes de virar um client sem modelo)."""
    try:
        from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS
    except Exception:  # noqa: BLE001
        return None
    opts = MODEL_OPTIONS.get((provider or "").lower())
    if not isinstance(opts, dict):
        return None
    lst = opts.get(level) or []
    return lst[0][1] if lst else None


def _fallback_link_available(provider: str) -> bool:
    """Se dá pra CONSTRUIR um link de fallback pra ``provider`` com credencial de
    servidor. claude-cli (assinatura via proxy) e provedores keyless passam; um
    provedor com env-var de chave só entra se a chave do servidor existir — senão o
    salto certeiro em 401 é inútil e some da cadeia."""
    from tradingagents.llm_clients.api_key_env import get_api_key_env

    p = (provider or "").lower()
    if p in _CLAUDE_CLI_PROVIDERS:
        return True  # assinatura do dono; se o proxy cair, o próprio invoke pula
    env_var = get_api_key_env(p)
    if not env_var:
        return True  # keyless (ollama/bedrock)
    return bool(os.environ.get(env_var))


def resolve_fallback_chain(config: dict, byok_key: str | None = None,
                           allow_server_key: bool = False) -> dict[str, list[dict]]:
    """Cadeia ORDENADA de provedores por nível (deep/quick) pro fallback automático.

    Topo = o provedor JÁ resolvido do nível (:func:`resolve_level_specs` — modo simples
    ou seletor avançado 027, inalterado). Tail = :data:`_DEFAULT_FALLBACK_ORDER`
    (claude-cli → openai), deduplicado contra o topo, limitado a ``max_hops`` saltos e
    filtrado ao que tem credencial de servidor.

    Gates (senão a cadeia é só ``[topo]``, isto é, comportamento de hoje, sem fallback):
    - **Owner-gated:** só com ``allow_server_key`` (dono logado) — os links de fallback
      usam a assinatura/chave do servidor (claude-cli é owner-only; openai é server-key).
    - **BYOK:** se a chave do usuário dirige o nível (``api_key`` no topo), a cadeia fica
      LIMITADA a esse provedor — não vaza pra outro provedor; se falhar, erro honesto.
    """
    base = resolve_level_specs(config, byok_key)
    enabled = config.get("fallback_enabled", True)
    try:
        max_hops = int(config.get("fallback_max_hops", _DEFAULT_MAX_HOPS))
    except (TypeError, ValueError):
        max_hops = _DEFAULT_MAX_HOPS
    max_hops = max(0, max_hops)

    chains: dict[str, list[dict]] = {}
    for level in ("deep", "quick"):
        top = dict(base[level])
        chain = [top]
        byok_driven = bool(top.get("api_key"))
        if enabled and allow_server_key and not byok_driven and max_hops > 0:
            for prov in _DEFAULT_FALLBACK_ORDER:
                if len(chain) >= 1 + max_hops:
                    break
                if any(prov == c["provider"] for c in chain):
                    continue
                if not _fallback_link_available(prov):
                    continue
                model = _catalog_default_model(prov, level)
                if not model:
                    continue  # sem modelo do catálogo → não constrói um client vazio
                chain.append({
                    "provider": prov, "model": model,
                    "base_url": None,  # link usa o endpoint padrão do provedor/proxy
                    "api_key": None,   # server-key / proxy da assinatura
                })
        chains[level] = chain
    return chains


def run_signature(selected_analysts, max_debate_rounds, max_risk_discuss_rounds,
                  asset_type: str, timeframe: str = "1d") -> str:
    """Graph-shape inputs that must invalidate a checkpoint if changed.

    Keyed into the checkpoint thread ID so a resume under a different analyst
    selection, debate/risk depth, asset mode, or reference timeframe starts
    fresh instead of silently continuing the previous graph (#1089). Each
    (ticker, date, TF) is therefore its own cacheable run.

    Module-level so anything that needs to ADDRESS a run's checkpoint without
    building the graph (the web UI's resume/refresh, which reads which stages came
    back done and rewinds one of them) derives the same key from one source.
    """
    return "|".join([
        "analysts=" + ",".join(selected_analysts),
        f"debate={max_debate_rounds}",
        f"risk={max_risk_discuss_rounds}",
        f"asset={asset_type}",
        f"tf={timeframe}",
    ])


class TradingAgentsGraph:
    """Main class that orchestrates the trading agents framework."""

    def __init__(
        self,
        selected_analysts=("market", "social", "news", "fundamentals"),
        debug=False,
        config: dict[str, Any] = None,
        callbacks: list | None = None,
    ):
        """Initialize the trading agents graph and components.

        Args:
            selected_analysts: List of analyst types to include
            debug: Whether to run in debug mode
            config: Configuration dictionary. If None, uses default config
            callbacks: Optional list of callback handlers (e.g., for tracking LLM/tool stats)
        """
        self.debug = debug
        self.config = config or DEFAULT_CONFIG
        self.callbacks = callbacks or []

        # BYOK: a per-run LLM API key may ride in the config dict (the webui injects
        # the key a user brought). It must feed the LLM clients but NEVER enter the
        # shared global config (set_config keeps a process-wide singleton that other
        # runs read) or be persisted — pull it out into an instance attr first, so
        # two concurrent users can't clobber each other's key through the global.
        self._llm_api_key = self._pop_llm_api_key(self.config)

        # Fallback transparente (task 027-fallback): o runner injeta, por-run, se a
        # requisição é de DONO (allow_server_key → destrava a cadeia de fallback com
        # chave de servidor) e um FallbackTracker pra registrar as trocas. Ambos são
        # objetos/estado VIVOS — tirados do config ANTES do set_config global pra não
        # vazarem no singleton compartilhado nem em nenhum record persistido (mesma
        # razão do _llm_api_key). Ausentes (CLI/testes) → sem fallback, comportamento
        # de hoje.
        self._allow_server_key = self._pop_transient(self.config, "_allow_server_key") is True
        self._fallback_tracker = self._pop_transient(self.config, "_fallback_tracker")

        # Update the interface's config
        set_config(self.config)

        # Create necessary directories
        os.makedirs(self.config["data_cache_dir"], exist_ok=True)
        os.makedirs(self.config["results_dir"], exist_ok=True)

        # Per-level provider/model/credential — cross-provider RÁPIDO/PESADO (task
        # 027). Each level (deep = PESADO, quick = RÁPIDO) may run a DIFFERENT
        # provider+model+endpoint, so a run can mix (ex.: Rápido=openai server-key,
        # Pesado=claude-cli assinatura $0/token). Absent per-level provider falls back
        # to the single ``llm_provider`` — simple mode is byte-for-byte unchanged.
        #
        # Fallback automático (task 027-fallback): cada nível vira uma CADEIA — o topo
        # é o provedor resolvido acima (inalterado); atrás dele, os provedores de
        # fallback (claude-cli → openai) SÓ pro dono (server-key), pra a análise não
        # parar quando o topo falha por estado do provedor. Cadeia de 1 = idêntico ao
        # caminho de hoje (público/BYOK, ou sem link de fallback disponível).
        chains = resolve_fallback_chain(
            self.config, getattr(self, "_llm_api_key", None), self._allow_server_key
        )

        # Callbacks (LLM/tool stats + per-step attribution) ride every level.
        common: dict[str, Any] = {}
        if self.callbacks:
            common["callbacks"] = self.callbacks

        self.deep_thinking_llm = self._build_level_llm("deep", chains["deep"], common)
        self.quick_thinking_llm = self._build_level_llm("quick", chains["quick"], common)

        self.memory_log = TradingMemoryLog(self.config)

        # Create tool nodes
        self.tool_nodes = self._create_tool_nodes()

        # Initialize components
        self.conditional_logic = ConditionalLogic(
            max_debate_rounds=self.config["max_debate_rounds"],
            max_risk_discuss_rounds=self.config["max_risk_discuss_rounds"],
        )
        self.graph_setup = GraphSetup(
            self.quick_thinking_llm,
            self.deep_thinking_llm,
            self.tool_nodes,
            self.conditional_logic,
        )

        self.propagator = Propagator(
            max_recur_limit=self.config.get("max_recur_limit", 100),
        )
        self.reflector = Reflector(self.quick_thinking_llm)
        self.signal_processor = SignalProcessor(self.quick_thinking_llm)

        # State tracking
        self.curr_state = None
        self.ticker = None
        self.log_states_dict = {}  # date to full state dict

        # Graph-shape-affecting run choices, kept for the checkpoint signature.
        self.selected_analysts = tuple(selected_analysts)

        # Set up the graph: keep the workflow for recompilation with a checkpointer.
        self.workflow = self.graph_setup.setup_graph(selected_analysts)
        self.graph = self.workflow.compile()
        self._checkpointer_ctx = None

    @staticmethod
    def _pop_llm_api_key(config: Any) -> str | None:
        """BYOK: remove and return a per-run ``llm_api_key`` from ``config``.

        Called before :func:`set_config` so the key feeds the LLM clients (as an
        explicit kwarg) but never lands in the process-wide global config that other
        runs read, and is never written to any persisted record. Mutating the dict
        here is intended — the caller passes a per-run copy."""
        if isinstance(config, dict):
            return config.pop("llm_api_key", None) or None
        return None

    @staticmethod
    def _pop_transient(config: Any, key: str) -> Any:
        """Remove e devolve um valor TRANSIENTE (objeto/estado vivo do runner) do
        ``config`` — chamado antes do :func:`set_config` pra o valor não vazar no
        singleton global nem em nenhum record persistido. ``None`` fora de dict."""
        if isinstance(config, dict):
            return config.pop(key, None)
        return None

    def _build_level_llm(self, level: str, chain: list[dict], common: dict[str, Any]) -> Any:
        """Constrói o LLM de um nível a partir da sua CADEIA de fallback.

        Cadeia de 1 elemento → devolve o client cru (idêntico ao caminho de hoje, sem
        wrapper). Cadeia de 2+ → embrulha num :class:`FallbackRunnable`, que tenta o
        topo e cai pro próximo só em erro de estado do provedor, registrando a troca
        no tracker. Cada membro recebe os MESMOS callbacks, então o que de fato roda
        reporta seu modelo real (atribuição por-etapa 024P1)."""
        members: list[dict[str, Any]] = []
        for spec in chain:
            client = create_llm_client(
                provider=spec["provider"],
                model=spec["model"],
                base_url=spec["base_url"],
                **self._get_provider_kwargs(spec["provider"], spec["api_key"]),
                **common,
            )
            members.append({
                "provider": spec["provider"], "model": spec["model"],
                "llm": client.get_llm(),
            })
        if len(members) == 1:
            return members[0]["llm"]
        from tradingagents.llm_clients.fallback import FallbackRunnable

        try:
            max_hops = int(self.config.get("fallback_max_hops", _DEFAULT_MAX_HOPS))
        except (TypeError, ValueError):
            max_hops = _DEFAULT_MAX_HOPS
        return FallbackRunnable(
            members, tracker=self._fallback_tracker, level=level, max_hops=max_hops
        )

    def _get_provider_kwargs(
        self, provider: str | None = None, api_key: str | None = None
    ) -> dict[str, Any]:
        """Provider-specific kwargs for ONE LLM level's client.

        ``provider`` defaults to the base ``llm_provider`` (simple mode: both levels
        share it). In cross-provider mode each level passes its own provider so the
        effort/thinking knob matches the model that actually runs that level.
        ``api_key`` is the credential resolved for THIS level (BYOK for the base
        provider, else ``None`` so the client falls back to the server env key)."""
        kwargs: dict[str, Any] = {}
        provider = (provider or self.config.get("llm_provider", "")).lower()

        # BYOK: forward the level's resolved key as an explicit client kwarg. It
        # takes precedence over the server env key and, for key-required providers,
        # satisfies the "key present" check without one.
        if api_key:
            kwargs["api_key"] = api_key

        if provider == "google":
            thinking_level = self.config.get("google_thinking_level")
            if thinking_level:
                kwargs["thinking_level"] = thinking_level

        elif provider == "openai":
            reasoning_effort = self.config.get("openai_reasoning_effort")
            if reasoning_effort:
                kwargs["reasoning_effort"] = reasoning_effort

        elif provider in ("anthropic", "claude-cli", "claude_cli", "claude-subscription"):
            # claude-cli reusa o AnthropicClient (via proxy), então o mesmo knob de
            # effort vale — o Pesado pela assinatura respeita o effort configurado.
            effort = self.config.get("anthropic_effort")
            if effort:
                kwargs["effort"] = effort

        # Sampling temperature is cross-provider: forward it whenever set.
        # float() here so a value coming from a TRADINGAGENTS_TEMPERATURE env
        # string ("0.2") works the same as a programmatic float.
        temperature = self.config.get("temperature")
        if temperature is not None and temperature != "":
            kwargs["temperature"] = float(temperature)

        # SDK retry budget is cross-provider. Forward it only when explicitly set
        # so each provider keeps its own default (usually 2) otherwise (#1091).
        max_retries = self.config.get("llm_max_retries")
        if max_retries is not None and max_retries != "":
            kwargs["max_retries"] = _coerce_max_retries(max_retries)

        return kwargs

    def _create_tool_nodes(self) -> dict[str, ToolNode]:
        """Create tool nodes for different data sources using abstract methods."""
        # A tool that raises must NOT abort the graph: the handler turns any tool
        # exception into a message the analyst reads and moves past (a cache-wrapped
        # RuntimeError from a bad indicator used to tear the whole run down here).
        def _tn(tools):
            return ToolNode(tools, handle_tool_errors=tool_error_message)

        return {
            "market": _tn(
                [
                    # Core stock data tools
                    get_stock_data,
                    # Technical indicators
                    get_indicators,
                    # Weekly+daily multi-timeframe trend read.
                    get_price_timeframes,
                    # Crypto perp funding / open interest / liquidations. Bound
                    # unconditionally so a crypto run's tool call executes here;
                    # the analyst only offers it to the LLM on crypto assets.
                    get_crypto_derivatives,
                    # Crypto network context — on-chain, spot-ETF flow, Fear &
                    # Greed. Bound unconditionally for the same reason; offered to
                    # the LLM only on crypto assets.
                    get_crypto_context,
                    # Deterministic verification snapshot (bound to the analyst
                    # LLM and required by its prompt; must be executable here or
                    # the call fails and the model reports it "unavailable").
                    get_verified_market_snapshot,
                ]
            ),
            # On-demand "Modo Erick": same data surface as the market analyst
            # (EMA/intraday come through the deterministic method coverage, not a
            # new tool), but its own node so it can run beside market without
            # sharing the tool-routing edge.
            "erick": _tn(
                [
                    get_stock_data,
                    get_indicators,
                    get_price_timeframes,
                    get_crypto_derivatives,
                    get_crypto_context,
                    get_verified_market_snapshot,
                ]
            ),
            "social": _tn(
                [
                    # News tools for social media analysis
                    get_news,
                ]
            ),
            "news": _tn(
                [
                    # News and insider information
                    get_news,
                    get_global_news,
                    get_insider_transactions,
                    get_macro_indicators,
                    get_prediction_markets,
                ]
            ),
            "fundamentals": _tn(
                [
                    # Fundamental analysis tools
                    get_fundamentals,
                    get_balance_sheet,
                    get_cashflow,
                    get_income_statement,
                ]
            ),
        }

    def _resolve_benchmark(self, ticker: str) -> str:
        """Pick the benchmark ticker for alpha calculation against ``ticker``.

        ``config["benchmark_ticker"]`` overrides everything when set; otherwise
        the suffix map matches the ticker's exchange suffix (e.g. ``.T`` for
        Tokyo). US-listed tickers without a dotted suffix fall through to the
        empty-suffix entry (SPY by default). Unrecognised suffixes (including
        US tickers with dots like ``BRK.B``) also fall back to the empty-suffix
        entry, which is the right default because the alpha calculation works
        in USD.
        """
        explicit = self.config.get("benchmark_ticker")
        if explicit:
            return explicit
        benchmark_map = self.config.get("benchmark_map", {})
        ticker_upper = ticker.upper()
        for suffix, benchmark in benchmark_map.items():
            if suffix and ticker_upper.endswith(suffix.upper()):
                return benchmark
        return benchmark_map.get("", "SPY")

    def _fetch_returns(
        self, ticker: str, trade_date: str, holding_days: int = 5,
        benchmark: str = "SPY",
    ) -> tuple[float | None, float | None, int | None]:
        """Fetch raw and alpha return for ticker over holding_days from trade_date.

        ``benchmark`` is the index used as the alpha baseline (resolved by the
        caller via ``_resolve_benchmark``). Returns ``(raw_return, alpha_return,
        actual_holding_days)`` or ``(None, None, None)`` if price data is
        unavailable (too recent, delisted, or network error).
        """
        from tradingagents.dataflows.symbol_utils import normalize_symbol

        try:
            start = datetime.strptime(trade_date, "%Y-%m-%d")
            end = start + timedelta(days=holding_days + 7)  # buffer for weekends/holidays
            end_str = end.strftime("%Y-%m-%d")

            # Normalize so the realized-return lookup hits the same instrument
            # the analysis priced (e.g. XAUUSD -> GC=F) (#984). The benchmark is
            # already a canonical Yahoo symbol from ``_resolve_benchmark``.
            stock = yf.Ticker(normalize_symbol(ticker)).history(start=trade_date, end=end_str)
            bench = yf.Ticker(benchmark).history(start=trade_date, end=end_str)

            if len(stock) < 2 or len(bench) < 2:
                return None, None, None

            actual_days = min(holding_days, len(stock) - 1, len(bench) - 1)
            raw = float(
                (stock["Close"].iloc[actual_days] - stock["Close"].iloc[0])
                / stock["Close"].iloc[0]
            )
            bench_ret = float(
                (bench["Close"].iloc[actual_days] - bench["Close"].iloc[0])
                / bench["Close"].iloc[0]
            )
            alpha = raw - bench_ret
            return raw, alpha, actual_days
        except Exception as e:
            logger.warning(
                "Could not resolve outcome for %s on %s vs %s (will retry next run): %s",
                ticker, trade_date, benchmark, e,
            )
            return None, None, None

    def _resolve_pending_entries(self, ticker: str) -> None:
        """Resolve pending log entries for ticker at the start of a new run.

        Fetches returns for each same-ticker pending entry, generates reflections,
        then writes all updates in a single atomic batch write to avoid redundant I/O.
        Skips entries whose price data is not yet available (too recent or delisted).

        Trade-off: only same-ticker entries are resolved per run.  Entries for
        other tickers accumulate until that ticker is run again.
        """
        pending = [e for e in self.memory_log.get_pending_entries() if e["ticker"] == ticker]
        if not pending:
            return

        benchmark = self._resolve_benchmark(ticker)
        updates = []
        for entry in pending:
            raw, alpha, days = self._fetch_returns(
                ticker, entry["date"], benchmark=benchmark,
            )
            if raw is None:
                continue  # price not available yet — try again next run
            reflection = self.reflector.reflect_on_final_decision(
                final_decision=entry.get("decision", ""),
                raw_return=raw,
                alpha_return=alpha,
                benchmark_name=benchmark,
            )
            updates.append({
                "ticker": ticker,
                "trade_date": entry["date"],
                "raw_return": raw,
                "alpha_return": alpha,
                "holding_days": days,
                "reflection": reflection,
            })

        if updates:
            self.memory_log.batch_update_with_outcomes(updates)

    def resolve_instrument_context(
        self, ticker: str, asset_type: str = "stock", trade_date: str | None = None
    ) -> str:
        """Resolve ticker identity once and return the full instrument context.

        Deterministic yfinance lookup (cached, fail-open) injected into a
        context string so every agent anchors to the real company instead of
        hallucinating one from the price chart (#814). Both the propagate()
        path and the CLI call this so the resolved identity reaches the whole
        graph regardless of entry point.

        When ``trade_date`` is given, a single frozen reference price (the
        date-guarded daily close — the SAME the chart/verdict use) is resolved and
        injected too, so every module anchors ONE price instead of drifting off its
        own live quote. Fail-open: a price hiccup just omits the reference line.
        """
        identity = resolve_instrument_identity(ticker)
        reference_price = None
        if trade_date and asset_type != "crypto":
            try:
                from tradingagents.dataflows.fundamentals_anchors import (
                    as_of_reference_price,
                )

                reference_price = as_of_reference_price(ticker, trade_date)
            except Exception:  # noqa: BLE001 — never block on the price enrichment
                reference_price = None
        return build_instrument_context(
            ticker, asset_type, identity,
            reference_price=reference_price, as_of=trade_date,
        )

    def _run_signature(self, asset_type: str, timeframe: str = "1d") -> str:
        """This graph's checkpoint signature — see :func:`run_signature`."""
        return run_signature(
            self.selected_analysts, self.config["max_debate_rounds"],
            self.config["max_risk_discuss_rounds"], asset_type, timeframe,
        )

    def propagate(self, company_name, trade_date, asset_type: str = "stock",
                  timeframe: str = "1d"):
        """Run the trading agents graph for a company on a specific date.

        ``asset_type`` selects between the stock pipeline (default) and the
        crypto pipeline (``"crypto"``) shipped in #567 — the CLI auto-detects
        from the ticker; programmatic callers pass it explicitly. When
        ``checkpoint_enabled`` is set in config, the graph is recompiled with
        a per-ticker SqliteSaver so a crashed run can resume from the last
        successful node on a subsequent invocation with the same ticker+date.

        ``timeframe`` is the reference frame the market analyst reads the technical
        structure on (daily by default); a different frame is a distinct, cacheable
        run whose timing/verdict may differ. Only the market analyst is
        timeframe-aware — the fundamental/news/sentiment theses are unchanged.
        """
        self.ticker = company_name

        # Resolve any pending memory-log entries for this ticker before the pipeline runs.
        self._resolve_pending_entries(company_name)

        # Recompile with a checkpointer if the user opted in. Fail-soft: the resume
        # machinery must NEVER be the thing that kills a run — if the checkpoint DB
        # can't be opened/compiled, fall back to a plain (non-resumable) run instead
        # of erroring the analysis.
        resume = False
        if self.config.get("checkpoint_enabled"):
            try:
                self._checkpointer_ctx = get_checkpointer(
                    self.config["data_cache_dir"], company_name
                )
                saver = self._checkpointer_ctx.__enter__()
                self.graph = self.workflow.compile(checkpointer=saver)

                step = checkpoint_step(
                    self.config["data_cache_dir"], company_name, str(trade_date),
                    self._run_signature(asset_type, timeframe),
                )
                if step is not None:
                    # A checkpoint exists → resume from the last completed node. The
                    # graph must be invoked with ``None`` (not the initial state) so
                    # LangGraph continues the saved thread instead of re-seeding it
                    # and re-running completed nodes (mirrors the resume test).
                    resume = True
                    logger.info(
                        "Resuming from step %d for %s on %s",
                        step, company_name, trade_date,
                    )
                else:
                    logger.info("Starting fresh for %s on %s", company_name, trade_date)
            except Exception:  # noqa: BLE001 — checkpoint is best-effort, never fatal
                logger.warning(
                    "checkpoint disabled for %s on %s (setup failed); running plain",
                    company_name, trade_date, exc_info=True,
                )
                if self._checkpointer_ctx is not None:
                    # best-effort cleanup of a half-open checkpointer
                    with contextlib.suppress(Exception):
                        self._checkpointer_ctx.__exit__(None, None, None)
                    self._checkpointer_ctx = None
                self.graph = self.workflow.compile()
                resume = False

        try:
            return self._run_graph(company_name, trade_date, asset_type=asset_type,
                                   timeframe=timeframe, resume=resume)
        finally:
            if self._checkpointer_ctx is not None:
                self._checkpointer_ctx.__exit__(None, None, None)
                self._checkpointer_ctx = None
                self.graph = self.workflow.compile()

    def save_reports(self, final_state, ticker, save_path=None) -> Path:
        """Write the markdown report tree for a completed run, like the CLI does.

        Programmatic callers get the same on-disk reports the CLI produces. Pass
        an explicit ``save_path`` or let it default under ``results_dir``.
        """
        if save_path is None:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            save_path = (
                Path(self.config["results_dir"])
                / "reports"
                / f"{safe_ticker_component(ticker)}_{stamp}"
            )
        return write_report_tree(final_state, ticker, save_path)

    def _run_graph(self, company_name, trade_date, asset_type: str = "stock",
                   timeframe: str = "1d", resume: bool = False):
        """Execute the graph and write the resulting state to disk and memory log.

        ``resume`` (a checkpoint for this thread already exists) feeds ``None`` to
        the graph instead of the freshly-built initial state, so the run continues
        from the last completed node rather than restarting it.
        """
        # Avisos de QUALIDADE DE DADO (série OHLCV vencida servida no fail-open) são
        # POR RUN, e o cano vive AQUI e não no chamador: a webui drenava, mas quem
        # chama ``propagate`` direto — CLI (main.py), backtests, run_portfolio — nunca
        # via nada. É justamente no backtest que os limiares provisórios seriam
        # calibrados, e calibrar num caminho mudo sobre dado velho é calibrar em cima
        # de ruído silencioso. Zera na entrada (nenhuma run herda o aviso da anterior)
        # e junta no estado final logo depois do grafo — quem já drenava por fora
        # continua funcionando, só encontra o coletor vazio.
        data_notices.reset()

        # Initialize state — inject memory log context for PM and the
        # deterministically resolved instrument identity for all agents.
        past_context = self.memory_log.get_past_context(company_name)
        instrument_context = self.resolve_instrument_context(
            company_name, asset_type, trade_date
        )
        init_agent_state = self.propagator.create_initial_state(
            company_name,
            trade_date,
            asset_type=asset_type,
            past_context=past_context,
            instrument_context=instrument_context,
            timeframe=timeframe,
        )
        args = self.propagator.get_graph_args()

        # Inject thread_id so same ticker+date+graph-shape resumes; a different
        # date or graph shape starts fresh (#1089).
        if self.config.get("checkpoint_enabled"):
            tid = thread_id(company_name, str(trade_date),
                            self._run_signature(asset_type, timeframe))
            args.setdefault("config", {}).setdefault("configurable", {})["thread_id"] = tid

        # On resume, feed ``None`` so LangGraph continues the checkpointed thread
        # from its last completed node instead of re-seeding and re-running it.
        graph_input = None if resume else init_agent_state

        # Pin the analysis date as the run's base date so every data tool clamps
        # look-ahead: a backtest at a past date can never fetch data dated after
        # it (see agents/utils/date_guard.py). The whole graph runs synchronously
        # inside this block, so the ContextVar covers all in-run tool calls.
        with base_date(str(trade_date)):
            if self.debug:
                trace = []
                last_printed = None
                for chunk in self.graph.stream(graph_input, **args):
                    if chunk["messages"]:
                        msg = chunk["messages"][-1]
                        # Nodes after the trader don't append to messages, so the
                        # same trailing message repeats across chunks. Print it only
                        # when it changes (#1027); the trace/state merge is unchanged.
                        signature = (type(msg).__name__, getattr(msg, "content", None))
                        if signature != last_printed:
                            msg.pretty_print()
                            last_printed = signature
                        trace.append(chunk)
                # Streamed chunks are per-node deltas. Merge them so the returned
                # state matches what graph.invoke() yields in the non-debug path.
                final_state = {}
                for chunk in trace:
                    final_state.update(chunk)
            else:
                final_state = self.graph.invoke(graph_input, **args)

        # Tudo que a run buscou já foi buscado: os avisos da camada de fetch entram
        # no mesmo ``degraded_sources`` das fontes que caíram — o canal que a UI e o
        # relatório já sabem nomear.
        data_notices.merge_into(final_state)

        # Store current state for reflection.
        self.curr_state = final_state

        # Log state to disk.
        self._log_state(trade_date, final_state)

        # Store decision for deferred reflection on the next same-ticker run.
        self.memory_log.store_decision(
            ticker=company_name,
            trade_date=trade_date,
            final_trade_decision=final_state["final_trade_decision"],
        )

        # Clear checkpoint on successful completion to avoid stale state.
        if self.config.get("checkpoint_enabled"):
            clear_checkpoint(
                self.config["data_cache_dir"], company_name, str(trade_date),
                self._run_signature(asset_type, timeframe),
            )

        return final_state, self.process_signal(final_state["final_trade_decision"])

    def _log_state(self, trade_date, final_state):
        """Log the final state to a JSON file."""
        self.log_states_dict[str(trade_date)] = {
            "company_of_interest": final_state["company_of_interest"],
            "trade_date": final_state["trade_date"],
            "market_report": final_state["market_report"],
            "sentiment_report": final_state["sentiment_report"],
            "news_report": final_state["news_report"],
            "fundamentals_report": final_state["fundamentals_report"],
            # On-demand; "" unless the Erick method was requested for this run.
            "erick_report": final_state.get("erick_report", ""),
            "investment_debate_state": {
                "bull_history": final_state["investment_debate_state"]["bull_history"],
                "bear_history": final_state["investment_debate_state"]["bear_history"],
                "history": final_state["investment_debate_state"]["history"],
                "current_response": final_state["investment_debate_state"][
                    "current_response"
                ],
                "judge_decision": final_state["investment_debate_state"][
                    "judge_decision"
                ],
            },
            "trader_investment_decision": final_state["trader_investment_plan"],
            "risk_debate_state": {
                "aggressive_history": final_state["risk_debate_state"]["aggressive_history"],
                "conservative_history": final_state["risk_debate_state"]["conservative_history"],
                "neutral_history": final_state["risk_debate_state"]["neutral_history"],
                "history": final_state["risk_debate_state"]["history"],
                "judge_decision": final_state["risk_debate_state"]["judge_decision"],
            },
            "investment_plan": final_state["investment_plan"],
            "final_trade_decision": final_state["final_trade_decision"],
        }

        # Save to file. Reject ticker values that would escape the
        # results directory when joined as a path component.
        safe_ticker = safe_ticker_component(self.ticker)
        directory = Path(self.config["results_dir"]) / safe_ticker / "TradingAgentsStrategy_logs"
        directory.mkdir(parents=True, exist_ok=True)

        log_path = directory / f"full_states_log_{trade_date}.json"
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(self.log_states_dict[str(trade_date)], f, indent=4)

    def process_signal(self, full_signal):
        """Process a signal to extract the core decision."""
        return self.signal_processor.process_signal(full_signal)

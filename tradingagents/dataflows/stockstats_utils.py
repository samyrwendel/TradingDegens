import logging
import os
import time
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time
from typing import Annotated
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from stockstats import wrap
from yfinance.exceptions import YFRateLimitError

from . import data_notices
from .config import get_config
from .symbol_utils import NoMarketDataError, crypto_base, normalize_symbol
from .utils import safe_ticker_component

logger = logging.getLogger(__name__)

# A vendor's latest OHLCV row this many calendar days before the requested date
# is treated as stale. Generous enough to span long holiday weekends, tight
# enough to catch the year-old frames yfinance occasionally returns (#1021).
# É o limiar de REJEIÇÃO — dado velho demais pra existir, erro duro.
MAX_OHLCV_STALE_DAYS = 10

# Limiar de DECLARAÇÃO (≠ rejeição). O bug L2 era um buraco de 3 dias: bem abaixo
# dos 10, então o guard passava calado e o ``drop_nature`` lia -1,3% onde a queda
# real era -4,6%. A granularidade que o método exige é de DIAS, não de semanas —
# um buraco desses muda o veredito sem mudar a cara do relatório.
#
# Contado em dias ÚTEIS pra não gritar toda segunda-feira (sexta→segunda é 1 dia
# útil de distância, não 3 de calendário). É o MENOR buraco que DECLARA — inclusive:
# 2 dias úteis declara, 1 cala (a condição é ``< OHLCV_STALE_NOTICE_BDAYS``, não
# ``<=``). O comentário e o código já discordaram aqui: com ``<=`` o buraco de
# EXATAMENTE 2 — o que a própria linha chamava de "o menor que já corrompeu uma
# leitura" — passava calado, e só 3+ declarava.
#
# Por que 2 e não 1, se o ``drop_nature`` trabalha em granularidade DIÁRIA e 1 dia
# já muda a leitura: 1 dia útil de atraso é o estado NORMAL de uma run ao vivo antes
# do fechamento — a barra de hoje ainda não publicou. Declarar aí seria um aviso em
# toda run intradiária, e aviso que aparece sempre é aviso que ninguém lê. 2 é o
# primeiro atraso que não tem explicação inocente. PROVISÓRIO, a calibrar com o
# histórico do próprio painel.
OHLCV_STALE_NOTICE_BDAYS = 2

# How long a same-day cache that does not yet reach the requested day may be
# reused before it is refetched (#1150). Short enough that an intraday run picks
# up today's close soon after it publishes, long enough that a day with no bar
# at all (weekend, holiday) cannot trigger a download on every call.
OHLCV_CACHE_TTL_SECONDS = 900


def yf_retry(func, max_retries=3, base_delay=2.0):
    """Execute a yfinance call with exponential backoff on rate limits.

    yfinance raises YFRateLimitError on HTTP 429 responses but does not
    retry them internally. This wrapper adds retry logic specifically
    for rate limits. Other exceptions propagate immediately.
    """
    for attempt in range(max_retries + 1):
        try:
            return func()
        except YFRateLimitError:
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(f"Yahoo Finance rate limited, retrying in {delay:.0f}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(delay)
            else:
                raise


def _ensure_date_column(data: pd.DataFrame) -> pd.DataFrame:
    """Normalize the date column to ``Date``.

    Some yfinance builds leave the index unnamed (so ``reset_index()`` yields
    ``index``) or use ``Datetime`` for intraday data. Rename the first
    date-like column so indicators don't silently drop when it isn't ``Date``.
    """
    if "Date" in data.columns:
        return data
    for candidate in ("index", "Datetime", "date"):
        if candidate in data.columns:
            return data.rename(columns={candidate: "Date"})
    return data


def _clean_dataframe(data: pd.DataFrame) -> pd.DataFrame:
    """Normalize a stock DataFrame for stockstats: parse dates, drop invalid rows, fill price gaps."""
    data = _ensure_date_column(data)
    data["Date"] = pd.to_datetime(data["Date"], errors="coerce")
    data = data.dropna(subset=["Date"])

    price_cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in data.columns]
    data[price_cols] = data[price_cols].apply(pd.to_numeric, errors="coerce")
    data = data.dropna(subset=["Close"])
    data[price_cols] = data[price_cols].ffill().bfill()

    return data


def _coerce_ohlcv_dates(data: pd.DataFrame) -> pd.Series:
    """Return parsed dates from an OHLCV frame, whether Date is a column or the index."""
    if "Date" in data.columns:
        return pd.to_datetime(data["Date"], errors="coerce").dropna()
    # yfinance keeps the dates in the index (a DatetimeIndex, sometimes unnamed).
    if isinstance(data.index, pd.DatetimeIndex):
        return pd.Series(pd.to_datetime(data.index, errors="coerce")).dropna()
    # Fallback: expose the index and look for any date-like column.
    df = data.reset_index()
    for col in ("Date", "Datetime", "date", "index"):
        if col in df.columns:
            parsed = pd.to_datetime(df[col], errors="coerce").dropna()
            if not parsed.empty:
                return parsed
    return pd.Series(dtype="datetime64[ns]")


def _bdays_atras(latest: pd.Timestamp, requested: pd.Timestamp) -> int:
    """Dias ÚTEIS entre a última barra e a data pedida (0 quando alcança)."""
    if latest >= requested:
        return 0
    # bdate_range inclui as duas pontas; a barra que EXISTE não conta como buraco.
    return max(0, len(pd.bdate_range(latest, requested)) - 1)


def _declara_serie_vencida(
    data: pd.DataFrame | None,
    curr_date: str,
    canonical: str | None,
    *,
    motivo: str | None = None,
    max_bdays: int = OHLCV_STALE_NOTICE_BDAYS,
) -> None:
    """Registra em ``degraded_sources`` que a série servida NÃO alcança a data.

    Silêncio aqui é o defeito do L2: o relatório saía com a queda medida numa série
    que parava dias antes, sem nada dizendo isso. Declara a fonte, a última barra e
    a idade — nunca "consertando" o número, só nomeando o buraco.
    """
    if data is None or getattr(data, "empty", True):
        return
    requested = pd.to_datetime(curr_date, errors="coerce")
    if pd.isna(requested):
        return
    requested = requested.normalize()
    dates = _coerce_ohlcv_dates(data)
    if dates.empty:
        return
    latest = dates.max().normalize()
    atraso_uteis = _bdays_atras(latest, requested)
    if motivo is None and atraso_uteis < max_bdays:
        return
    dias = (requested - latest).days
    fonte = canonical or "OHLCV"
    razao = (
        f"última barra em {latest.date().isoformat()} para a data pedida "
        f"{requested.date().isoformat()} — {dias} dia(s) de atraso "
        f"({atraso_uteis} útil/úteis)"
    )
    if motivo:
        razao = f"{motivo}; {razao}"
    data_notices.record(f"série OHLCV de {fonte}", razao)


def _assert_ohlcv_not_stale(
    data: pd.DataFrame,
    curr_date: str,
    symbol: str,
    canonical: str | None = None,
    *,
    max_stale_days: int = MAX_OHLCV_STALE_DAYS,
) -> None:
    """Reject OHLCV whose latest row is far older than curr_date.

    Raises NoMarketDataError (with a stale-specific detail) so the router treats
    it like any other "no usable data from this vendor" — try the next vendor,
    then emit one clear unavailable signal. Empty frames are left to the
    caller's existing no-data handling; this guards only the dangerous case of
    present-but-stale rows (a vendor returning a year-old frame that would
    otherwise feed wrong prices to the agent, #1021).
    """
    if data is None or data.empty:
        return
    requested = pd.to_datetime(curr_date, errors="coerce")
    if pd.isna(requested):
        return
    requested = requested.normalize()
    dates = _coerce_ohlcv_dates(data)
    if dates.empty:
        return
    latest = dates.max().normalize()
    stale_days = (requested - latest).days
    if stale_days > max_stale_days:
        raise NoMarketDataError(
            symbol,
            canonical,
            f"latest row is {latest.date()}, {stale_days} days before the "
            f"requested {requested.date()} (stale) — refusing to use it",
        )


# ============================ BUSCA INCREMENTAL DO DIÁRIO (DA-119) ============
#
# Pedido do Samyr: *"sempre com dados incrementais, nada revalidar 123 pedindo
# todo histórico que vc já tem em cache, vc tem os últimos dias e horas"*.
#
# O que havia: toda revalidação do diário rebaixava a JANELA DE 5 ANOS inteira
# (~110 KB por ativo) para obter a barra do dia. Com 20 ativos e a revalidação
# automática por fechamento de candle da DA-118, isso multiplicaria por 24 no dia,
# contra um provedor cujo throttle já nos mordeu (o outlier de 75s que o
# single-flight veio matar) e a partir do único IP disponível, que é o de
# PRODUÇÃO. O intradiário já era incremental por dia (cache por ``day_key``,
# história imutável) — esse lado não se toca.
#
# **A regra é integridade acima de economia.** Este produto inteiro é NÍVEL
# calculado em cima de série; uma série remendada errada não dá erro, dá número
# errado em todos os níveis. Então o remendo só acontece com PROVA de
# continuidade, e na dúvida cai no download completo dizendo por quê.
#
# Como a prova é obtida: pede-se a partir do ÚLTIMO DIA QUE JÁ ESTÁ NO CACHE (não
# do dia seguinte). O trecho novo tem então de COMEÇAR em cima do que já se tem —
# essa sobreposição é a prova de que não há buraco entre os dois. Sem ela (feriado
# longo mal calculado, símbolo que parou de negociar, cache velho demais, fonte
# que repaginou o histórico) não se costura: baixa tudo.
#
# E a emenda substitui POR DATA, nunca concatena: a barra do dia corrente é
# MUTÁVEL (o Yahoo publica candle parcial durante o pregão) e tem de ser
# sobrescrita, senão duas leituras no mesmo dia deixariam a série com a linha
# duplicada — uma congelada no valor da primeira leitura.


def _ultimo_dia(frame) -> "pd.Timestamp | None":
    """O último dia presente num frame de OHLCV, ou ``None``."""
    if frame is None or getattr(frame, "empty", True):
        return None
    dates = _coerce_ohlcv_dates(frame)
    return None if dates.empty else dates.max().normalize()


def emenda_ohlcv(cached: pd.DataFrame, novo: pd.DataFrame) -> pd.DataFrame:
    """Cola ``novo`` no fim de ``cached`` SUBSTITUINDO por data.

    Tudo a partir do primeiro dia do trecho novo sai do cache antes da junção —
    é isso que faz a barra do dia corrente ser atualizada em vez de duplicada.

    **As duas pontas são normalizadas para datetime ANTES de juntar**, e isso não
    é zelo: o cache vem de ``read_csv`` (``Date`` é *string*) e o trecho novo vem
    do yfinance (``Date`` é ``Timestamp``). Concatenar os dois crus produz uma
    coluna de tipos misturados, e o ``sort_values`` seguinte levanta
    ``TypeError: '<' not supported between Timestamp and str`` — na produção, não
    na suíte, porque um teste que monta o cache em memória nunca vê a string.
    """
    cached = cached.copy()
    novo = novo.copy()
    cached["Date"] = _coerce_ohlcv_dates(cached)
    novo["Date"] = _coerce_ohlcv_dates(novo)
    corte = novo["Date"].min()
    base = cached[cached["Date"] < corte]
    out = pd.concat([base, novo], ignore_index=True)
    return out.sort_values("Date").reset_index(drop=True)


def busca_ohlcv(canonical: str, start_str: str, end_str: str,
                cached: pd.DataFrame | None = None) -> tuple[pd.DataFrame, str, str]:
    """A série do símbolo — INCREMENTAL quando o cache permite provar continuidade.

    Devolve ``(frame, modo, motivo)`` com ``modo`` em ``completo`` /
    ``incremental`` / ``sem_novidade``. O ``motivo`` diz por que o completo foi
    escolhido — é o que impede "caiu no completo" de virar mistério.

    Nunca levanta por conta própria: quem chama decide o que fazer com um frame
    vazio (hoje, ``NoMarketDataError``).
    """
    def _baixa(inicio: str) -> pd.DataFrame:
        return _ensure_date_column(yf_retry(lambda: yf.download(
            canonical, start=inicio, end=end_str, multi_level_index=False,
            progress=False, auto_adjust=True,
        )).reset_index())

    fim = _ultimo_dia(cached)
    if fim is None:
        return _baixa(start_str), "completo", "não havia série em cache"
    if fim < pd.to_datetime(start_str).normalize():
        # O cache inteiro já saiu da janela de 5 anos: não há em que emendar.
        return _baixa(start_str), "completo", "o cache é anterior à janela de 5 anos"

    trecho = _baixa(fim.strftime("%Y-%m-%d"))
    if trecho.empty or "Close" not in trecho.columns:
        # Fora do pregão isto é o caso NORMAL: não há barra nova. O cache fica
        # como está — e quem chama regrava o arquivo pra o TTL não pedir de novo
        # a cada chamada.
        return cached, "sem_novidade", "a fonte não devolveu barra nova"

    inicio_novo = _coerce_ohlcv_dates(trecho).min()
    if inicio_novo > fim:
        # SEM SOBREPOSIÇÃO: entre o fim do cache e o começo do trecho novo há um
        # intervalo que ninguém verificou. Costurar aqui inventaria continuidade.
        return (_baixa(start_str), "completo",
                f"buraco entre o cache (até {fim.date()}) e o trecho novo "
                f"(desde {inicio_novo.date()})")

    return emenda_ohlcv(cached, trecho), "incremental", ""


# ======================= REGRA ÚNICA DO CACHE DIÁRIO: O FECHAMENTO ============
#
# A série diária/semanal fica CONGELADA dentro do pregão e é renovada — INTEIRA,
# sem emenda — na primeira leitura depois do fechamento. Nunca fresca a cada
# chamada (estabilidade do veredito e o throttle do Yahoo, no único IP de
# produção), nunca eterna.
#
# Por que inteira: a emenda incremental (DA-119) confiava no trecho novo. Em
# 22/09/2026 o Yahoo devolveu um trecho SEM aquele pregão, a sobreposição provou
# "continuidade" e o buraco ficou no cache pra sempre — AAPL EMA21 327,78 no TD ×
# 328,81 fresco em 24/09, AGUARDAR × AGIR. E o Yahoo reescala o histórico todo a
# cada dividendo/split (``auto_adjust``): só o download inteiro traz a escala nova.
# Uma vez por fechamento, então, a série é a da fonte — buraco transitório ou
# ajuste de ação corporativa duram no máximo até o próximo fechamento.
#
# O fechamento é 16:00 de Nova York + folga pro leilão de fechamento publicar,
# em dia útil. Sem calendário de feriado de propósito: num feriado a regra só
# custa UM download a mais (a fonte devolve a mesma série), e um calendário
# nosso envelheceria. ponytail: meio-pregão (13:00) renova só às 16:10 — série
# do pregão anterior por 3h nesse dia; ligar exchange_calendars se importar.
# Cripto fecha a vela D às 00:00 UTC, todo dia.
_NY = ZoneInfo("America/New_York")
FECHAMENTO_NY = dt_time(16, 10)


def ultimo_fechamento(agora: datetime | None = None, cripto: bool = False) -> datetime:
    """O fechamento de vela diária mais recente em ``agora`` (aware; default = já)."""
    agora = agora or datetime.now(timezone.utc)
    if cripto:
        return agora.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    ny = agora.astimezone(_NY)
    f = datetime.combine(ny.date(), FECHAMENTO_NY, tzinfo=_NY)
    while f > ny or f.weekday() >= 5:
        f = datetime.combine(f.date() - timedelta(days=1), FECHAMENTO_NY, tzinfo=_NY)
    return f


def _needs_refresh(data_file, *, cripto: bool = False, agora: datetime | None = None) -> bool:
    """O arquivo foi gravado antes do último fechamento? Então a série é renovada."""
    return os.path.getmtime(data_file) < ultimo_fechamento(agora, cripto).timestamp()


def load_ohlcv(symbol: str, curr_date: str) -> pd.DataFrame:
    """Fetch OHLCV data with caching, filtered to prevent look-ahead bias.

    Downloads 5 years of data up to today and caches per symbol. On
    subsequent calls the cache is reused. Rows after curr_date are
    filtered out so backtests never see future prices.
    """
    # Resolve broker/forex symbols (XAUUSD+ -> GC=F) to Yahoo's convention,
    # then reject values that would escape the cache directory when
    # interpolated into the cache filename (e.g. ``../../tmp/x``).
    canonical = normalize_symbol(symbol)
    safe_symbol = safe_ticker_component(canonical)

    config = get_config()
    curr_date_dt = pd.to_datetime(curr_date)

    # Cache uses a fixed window (5y to today) so one file per symbol.
    today_date = pd.Timestamp.today()
    start_date = today_date - pd.DateOffset(years=5)
    start_str = start_date.strftime("%Y-%m-%d")
    # yfinance ``end`` is EXCLUSIVE; request tomorrow so today's row is included
    # when curr_date is the current day (#986). Look-ahead is still prevented by
    # the curr_date filter below.
    end_str = (today_date + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    os.makedirs(config["data_cache_dir"], exist_ok=True)
    data_file = os.path.join(
        config["data_cache_dir"],
        f"{safe_symbol}-YFin-data-{start_str}-{end_str}.csv",
    )

    # A cached file may be empty if a prior fetch failed (unknown symbol,
    # transient rate limit). Treat an empty/columnless cache as a miss and
    # re-fetch rather than serving the poisoned file forever.
    data = None
    usable_cache = None
    if os.path.exists(data_file):
        cached = pd.read_csv(data_file, on_bad_lines="skip", encoding="utf-8")
        # Serve the cache only when it is usable and was written after the last
        # close (regra do fechamento, acima); otherwise refetch.
        if not cached.empty and "Close" in cached.columns:
            usable_cache = cached
            if not _needs_refresh(data_file, cripto=crypto_base(canonical) is not None):
                data = cached

    if data is None:
        try:
            # INTEIRO, sem emenda no cache (regra do fechamento): a emenda da
            # DA-119 eternizou um pregão que o Yahoo omitiu num trecho.
            downloaded, _modo, _motivo = busca_ohlcv(canonical, start_str, end_str)
        except Exception:
            # A revalidação virou obrigatória para o cache que não cobre o dia; se
            # a fonte estiver fora do ar não se pode perder um cache que antes era
            # servido. Cai para ele — o guard de série vencida (#1021) ainda mata
            # o caso de dado antigo demais, então "degradado" nunca vira "errado".
            if usable_cache is None:
                raise
            logger.warning(
                "OHLCV refresh failed for %s; serving the cached frame (may miss "
                "the most recent bars)", canonical,
            )
            # O fail-open deixa de ser SILENCIOSO (C4): servir cache vencido é uma
            # degradação real, e quem lê o relatório precisa saber que a série pode
            # não alcançar a data pedida. Vai pro mesmo canal que a UI já nomeia.
            _declara_serie_vencida(
                usable_cache, curr_date, canonical,
                motivo="a atualização da fonte falhou e a série veio do cache",
            )
            data = usable_cache
        else:
            # Only cache real data — never persist an empty frame. Um retorno vazio
            # continua sendo NoMarketDataError (contrato de sempre), não cache velho.
            if downloaded.empty or "Close" not in downloaded.columns:
                raise NoMarketDataError(
                    symbol, canonical, "Yahoo Finance returned no rows"
                )
            downloaded.to_csv(data_file, index=False, encoding="utf-8")
            data = downloaded

    data = _clean_dataframe(data)

    # Filter to curr_date to prevent look-ahead bias in backtesting
    data = data[data["Date"] <= curr_date_dt]

    # Reject a stale frame (latest row far older than curr_date) rather than
    # feeding year-old prices into indicators (#1021).
    _assert_ohlcv_not_stale(data, curr_date, symbol, canonical)

    # Passou da rejeição mas ainda tem buraco relevante? DECLARA (C4). O guard duro
    # protege do absurdo (frame de um ano atrás); este aqui protege do sutil — o
    # buraco de poucos dias que muda a leitura sem mudar a aparência.
    _declara_serie_vencida(data, curr_date, canonical)

    return data


def filter_financials_by_date(data: pd.DataFrame, curr_date: str) -> pd.DataFrame:
    """Drop financial statement columns (fiscal period timestamps) after curr_date.

    yfinance financial statements use fiscal period end dates as columns.
    Columns after curr_date represent future data and are removed to
    prevent look-ahead bias.
    """
    if not curr_date or data.empty:
        return data
    cutoff = pd.Timestamp(curr_date)
    mask = pd.to_datetime(data.columns, errors="coerce") <= cutoff
    return data.loc[:, mask]


class StockstatsUtils:
    @staticmethod
    def get_stock_stats(
        symbol: Annotated[str, "ticker symbol for the company"],
        indicator: Annotated[
            str, "quantitative indicators based off of the stock data for the company"
        ],
        curr_date: Annotated[
            str, "curr date for retrieving stock price data, YYYY-mm-dd"
        ],
    ):
        data = load_ohlcv(symbol, curr_date)
        df = wrap(data)
        df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
        curr_date_str = pd.to_datetime(curr_date).strftime("%Y-%m-%d")

        df[indicator]  # trigger stockstats to calculate the indicator
        matching_rows = df[df["Date"].str.startswith(curr_date_str)]

        if not matching_rows.empty:
            indicator_value = matching_rows[indicator].values[0]
            return indicator_value
        else:
            return "N/A: Not a trading day (weekend or holiday)"

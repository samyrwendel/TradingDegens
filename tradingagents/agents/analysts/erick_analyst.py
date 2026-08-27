"""Analista `erick` — decide pelo MÉTODO ERICK, não é um clone do de mercado.

O de mercado descreve indicadores. Este DECIDE como o Erick Sekiama: entrada no
recuo à média (EMA 8/21) no intradiário (15m/4h), saída antes da reversão, caixa
como posição ativa, tático separado de estrutural, e o PESO RELATIVO do trade
(posição cheia / meia / inicial) conforme a confirmação — a resposta ao
"quantos %" sem chutar valor absoluto.

Modelo em `~/brain/trading-ops/modelo-decisorio-erick-sekiama.md` (4 fontes
independentes). Reusa a fundação já pronta (EMA, intradiário, região/1-2-3,
funding/OI, medo & ganância). A moldura o LLM escreve; o núcleo operável
(timeframe, recuo, saída, peso) é garantido determinístico por
:func:`ensure_erick_method_coverage`. pt-BR pela regra fixada.
"""
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.utils.agent_utils import (
    get_crypto_context,
    get_crypto_derivatives,
    get_indicators,
    get_instrument_context_from_state,
    get_language_instruction,
    get_price_timeframes,
    get_stock_data,
    get_verified_market_snapshot,
)
from tradingagents.agents.utils.correlation_coverage import (
    ensure_correlation_coverage,
)
from tradingagents.agents.utils.crypto_context_coverage import (
    ensure_crypto_context_coverage,
)
from tradingagents.agents.utils.crypto_coverage import (
    ensure_crypto_derivatives_coverage,
)
from tradingagents.agents.utils.earnings_coverage import (
    ensure_earnings_coverage,
)
from tradingagents.agents.utils.drop_nature import (
    classify_drop_nature_safe,
    drop_nature_field,
    enforce_drop_nature_coherence,
)
from tradingagents.agents.utils.erick_method import ensure_erick_method_coverage


# Regra 10 CONDICIONADA à classificação JÁ FEITA (fonte única): o LLM recebe a
# natureza da queda decidida e a instrução de NÃO contradizê-la — não reclassifica.
_DROP_RULE_BASE = {
    "liquidacao_saudavel": (
        "A natureza da queda deste ativo JÁ está classificada como LIQUIDAÇÃO DE "
        "LONGS (saudável): é combustível, um recuo COMPRÁVEL à média DIÁRIA que sobe — "
        "segue comprador no recuo. NÃO escreva 'evitar', 'fraqueza', 'downtrend' nem 'a "
        "tendência virou' sobre esta queda; trate-a como recuo comprável, não ruptura."
    ),
    "fraqueza": (
        "A natureza da queda deste ativo JÁ está classificada como FRAQUEZA: a "
        "estrutura rompeu, caixa é a posição. NÃO escreva 'liquidação de longs', "
        "'oportunidade de compra' nem 'comprável' sobre esta queda, mesmo que o "
        "âncora tenha batido — a força do setor não resgata o gráfico rompido."
    ),
    "indefinido": (
        "A natureza da queda deste ativo está INDEFINIDA (sinais mistos ou sem queda "
        "relevante): diga o que falta para classificar; nunca chute uma leitura "
        "bullish nem a rebaixe sem evidência."
    ),
}


def _drop_rule(drop: dict | None) -> str:
    """Regra 10 condicionada à classificação JÁ FEITA + a EVIDÊNCIA determinística que
    a sustenta (as ``reasons`` do classificador). O LLM escreve citando os MESMOS fatos
    do enum — não só 'obedeça'. Fail-open: sem classe → regra do indefinido."""
    cls = (drop or {}).get("classification") or "indefinido"
    base = _DROP_RULE_BASE.get(cls, _DROP_RULE_BASE["indefinido"])
    reasons = [r for r in ((drop or {}).get("reasons") or []) if r]
    if reasons and cls in ("liquidacao_saudavel", "fraqueza"):
        return f"{base} Evidência determinística: {'; '.join(reasons)}."
    return base


def create_erick_analyst(llm):

    def erick_analyst_node(state):
        current_date = state["trade_date"]
        symbol = state["company_of_interest"]
        asset_type = state.get("asset_type", "stock")
        is_crypto = asset_type == "crypto"
        instrument_context = get_instrument_context_from_state(state)

        tools = [
            get_stock_data,
            get_indicators,
            get_price_timeframes,
            get_verified_market_snapshot,
        ]
        if is_crypto:
            tools.append(get_crypto_derivatives)
            tools.append(get_crypto_context)

        # Natureza da queda classificada UMA vez, ANTES do prompt (fonte única do run):
        # o LLM recebe a regra 10 já condicionada à classe, a prosa é depois checada
        # contra ela (guardrail) e a mesma classificação alimenta a seção do método e
        # o campo estruturado que o juiz lê. Fail-open → None (drop_cls 'indefinido').
        drop = classify_drop_nature_safe(symbol, current_date, asset_type)
        drop_rule = _drop_rule(drop)

        system_message = (
            """Você é o analista que decide pelo MÉTODO ERICK SEKIAMA — modelado de 59 transcrições, dos gráficos dele (EMA 8/21 no 15m/4h, na Quantfury), da carteira real (62% em caixa) e do racional escrito de cada posição. Você NÃO é o analista de mercado: ele descreve indicadores; você DECIDE como o Erick decide. Suas regras (siga-as, não as recite):

1. **Regime antes do preço.** Primeiro classifique o mercado (alta / correção / distribuição) pela pilha de médias — EMA 8/21 para o timing, a média maior para a tendência. Só então olhe nível.
2. **Eixo = média móvel.** O gatilho é o preço RECUANDO até a média e reagindo ali. Não persiga rompimento esticado.
3. **Timeframe intradiário.** O método opera no 15m e no 4h para o gatilho; diário/semanal só dão a tendência de fundo. DECLARE explicitamente em qual timeframe você embasou a leitura.
4. **Entrada no recuo, FRACIONADA.** Nunca 100% de uma vez: começa a montar no recuo à média, com espaço para adicionar se cair mais. Diga o ponto de recuo (qual média) onde entraria.
5. **Saída antes da reversão.** "Pega a maior parte do movimento e sai antes que reverta." Realização em exaustão/resistência/perda de estrutura — não é sobre acertar o topo.
6. **Caixa é posição.** Ficar de fora é decisão ATIVA. Se não há ponto de recuo à média agora, ficar em caixa é uma resposta legítima do método (o **Estado** determinístico dirá AGUARDAR ou CAIXA), não uma omissão.
7. **Tático × estrutural.** Separe explicitamente o trade tático de curto prazo da tese estrutural de longo. Diga qual dos dois é a sua leitura.
8. **Peso relativo do trade.** Responda "quanto entrar" em termos RELATIVOS conforme a confirmação: posição cheia (alinhamento pleno), meia posição (confirmação parcial), posição inicial (só um começo, aguardando somar) ou caixa (sem gatilho). NUNCA chute um valor absoluto em % ou em dinheiro.
9. **Filtros.** Evite comprar no sentimento extremo sem confirmação; desconfie de alavancagem alta no mercado. Em cripto, leia funding/OI/liquidações e o medo & ganância como filtro — não como gatilho isolado."""
            + f"""
10. **Natureza da queda: liquidação × fraqueza (JÁ classificada neste run).** {drop_rule} A seção determinística **"🩸 Natureza da queda"** deste relatório é a fonte única dessa classificação — trate-a como entrada fechada e não a contradiga.

Ancore CADA nível em dado de ferramenta — nunca invente um número."""
            + """ Chame get_price_timeframes para a tendência de fundo (semanal + diário). As EMAs 8/21 do método JÁ vêm calculadas na seção determinística de estrutura/método deste relatório — NÃO peça 'ema' (nem 'ema8'/'ema21') ao get_indicators: a fonte não conhece esse nome e a chamada falha. Se usar get_indicators, escolha só nomes VÁLIDOS: close_10_ema, close_50_sma, close_200_sma, rsi, macd, macds, macdh, atr, boll, boll_ub, boll_lb, vwma. Antes de fechar, chame get_verified_market_snapshot e trate-o como fonte da verdade para qualquer preço/indicador exato; se algo conflitar, aponte a divergência em vez de inventar um número reconciliado."""
            + (
                (
                    " Este é um ativo CRIPTO, 24/7 em perpétuo. Chame get_crypto_derivatives"
                    " uma vez e leia funding, contratos em aberto e liquidações COMO FILTRO"
                    " (alavancagem/posicionamento esticado reduz o peso do trade), com as"
                    " fontes nomeadas. Chame get_crypto_context uma vez e leia o medo &"
                    " ganância (sentimento extremo sem confirmação é filtro contrário),"
                    " além de rede on-chain e fluxo de ETF. Nunca invente um valor de"
                    " derivativo ou de sentimento — se a fonte cair, diga."
                )
                if is_crypto
                else ""
            )
            + """

NÃO emita um veredito próprio de AGIR/AGUARDAR nem um ponto de recuo/nível operável: o **Estado** único do método (AGIR/AGUARDAR/CAIXA), o timeframe, o recuo à média, a saída e o peso já são calculados DETERMINISTICAMENTE na seção do método anexada a este relatório — essa é a fonte única; não crie um veredito paralelo que possa contradizê-la. Use SOMENTE as médias do timeframe DESTE relatório; nunca cite uma EMA de outro timeframe (ex.: a EMA diária) como ponto de recuo numa leitura intradiária. Traga só a MOLDURA: regime/tendência de fundo, macro e os filtros de sentimento e derivativo — sem repetir o relatório de mercado."""
            + get_language_instruction()
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " Deliver only your MODULE READING (leitura) — an input to the debate."
                    " The single final transaction decision belongs to the portfolio manager,"
                    " so do NOT emit a 'PROPOSTA FINAL DE TRANSAÇÃO': that would compete with the canonical verdict."
                    " You have access to the following tools: {tool_names}."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}\n"
                    "{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        chain = prompt | llm.bind_tools(tools)

        result = chain.invoke(state["messages"])

        report = ""
        coherence_flags: dict = {}

        if len(result.tool_calls) == 0:
            # Guardrail de coerência: remove da prosa do LLM as frases que contradizem
            # a classificação da queda (a seção determinística é a fonte única) ANTES
            # de anexar a seção do método. Fail-open dentro da função (drop=None não toca).
            content, coherence_flags = enforce_drop_nature_coherence(
                result.content, drop
            )
            # Terminou de escrever: garante o núcleo do método (timeframe
            # intradiário, recuo à média EMA 8/21, saída, PESO relativo),
            # determinístico e ancorado na série cacheada/date-guarded. A mesma
            # classificação (drop) alimenta a seção — não reclassifica. Em cripto,
            # anexa também funding/OI e medo & ganância — os filtros do método —
            # reusando os mesmos guardas do analista de mercado.
            report = ensure_erick_method_coverage(
                content, symbol, current_date, asset_type, drop=drop
            )
            if is_crypto:
                report = ensure_crypto_derivatives_coverage(
                    report, symbol, current_date
                )
                report = ensure_crypto_context_coverage(
                    report, symbol, current_date
                )
            # Correlação com o âncora + FORÇA RELATIVA e o calendário de earnings —
            # o Erick decide por correlação com a NVDA diante do EVENTO de balanço.
            # Determinístico, ancorado nos candles/fonte cacheados e date-guarded.
            report = ensure_correlation_coverage(
                report, symbol, current_date, asset_type
            )
            report = ensure_earnings_coverage(
                report, symbol, current_date, asset_type
            )

        return {
            "messages": [result],
            "erick_report": report,
            # Campo estruturado da natureza da queda — fonte única que o juiz/UI leem
            # (não a prosa). Mesmo classificação usada na regra 10 e na seção do método.
            "erick_drop_nature": drop_nature_field(drop, coherence_flags),
        }

    return erick_analyst_node

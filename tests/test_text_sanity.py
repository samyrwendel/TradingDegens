"""Tests for the debate text-sanity validator (spec 3b, 25/08).

Structural detection is deterministic and zero-dependency, so those assertions
run everywhere. The lexical (aspell) assertions skip cleanly when the pt_BR
speller is not installed, matching how the validator degrades in production.

Fixtures are drawn from the real reference corpus (AAPL 1d / MSFT 4h,
2026-08-25): the corrupt snippets carry the exact garble the field reports
flagged; the clean snippet is ordinary pt-BR trader prose.
"""

import pytest

from tradingagents.agents.utils.text_sanity import (
    aspell_available,
    invented_words,
    sanity_report,
    structural_anomalies,
)

# --- real corrupt tokens observed in the AAPL/MSFT runs -------------------
CORRUPT_MECHANICAL = (
    "Você olha para o gráfico e o MACD es'tá negativo. O bear fica dezANIMAdO "
    "com d%d% de chance, mas os 21por cento de alta seguem firmes."
)
# invented pt-orthography non-words (lexical class), no mechanical artifacts
CORRUPT_INVENTED = (
    "A tese de baixa é uma faiança flexa: o modelo diz que a probababilidade de "
    "queda subiu, mas os últimesos períodos mostram o contrário e o pregõeles "
    "seguem comprando com fudo de mercado."
)
CLEAN_PT = (
    "O tempo gráfico semanal ainda aponta para cima, enquanto o diário virou "
    "para baixo — isso configura um pullback corretivo dentro de uma tendência "
    "de alta maior. A média móvel de 200 períodos está logo abaixo do preço, "
    "e o fluxo de caixa livre segue robusto. Mantemos a leitura construtiva "
    "com stop técnico abaixo do suporte recente."
)


# --------------------------------------------------------------------------
# Structural signal (deterministic, no external dependency)
# --------------------------------------------------------------------------

def test_structural_flags_format_code():
    kinds = {k for k, _ in structural_anomalies("chance de d%d% no pump")}
    assert "fmt" in kinds


def test_structural_flags_case_flip():
    kinds = {k for k, _ in structural_anomalies("o mercado dezANIMAdO hoje")}
    assert "caseflip" in kinds


def test_structural_flags_bad_apostrophe():
    kinds = {k for k, _ in structural_anomalies("o preço es'tá caindo")}
    assert "punct" in kinds


def test_structural_flags_digit_glue():
    kinds = {k for k, _ in structural_anomalies("subiu 21por cento ontem")}
    assert "digitglue" in kinds


def test_structural_allows_english_contractions():
    # "it's", "let's", "Apple's" are legit English — must not be flagged.
    assert structural_anomalies("well, it's clear that let's review Apple's moat") == []


def test_structural_allows_units_and_multipliers():
    # "35x", "200d", "4h", "50sma" are legit finance shorthand.
    assert structural_anomalies("negocia a 35x com MMS de 200d no 4h e 50sma") == []


def test_structural_allows_technical_units(subtests=None):
    """Regressão (task 20260828-004): nó de litografia e capacidade de hardware.

    Um turno inteiro do AAPL foi marcado como corrompido por causa de "2nm"/"3nm" —
    zero palavra inventada nele. Prosa técnica normal não é corrupção.
    """
    text = ("a transição para 2nm e 3nm com 8GB de RAM, clock de 3GHz, latência de "
            "5ms e link de 10Gbps na fábrica de 300mm")
    assert structural_anomalies(text) == []


def test_structural_clean_text_has_no_hits():
    assert structural_anomalies(CLEAN_PT) == []


# --------------------------------------------------------------------------
# Report severity
# --------------------------------------------------------------------------

def test_report_mechanical_is_degraded():
    rep = sanity_report(CORRUPT_MECHANICAL)
    assert rep.degraded
    assert "structural_artifacts" in rep.flags


def test_report_clean_is_clean():
    rep = sanity_report(CLEAN_PT)
    assert rep.severity == "clean"
    assert not rep.structural


def test_report_score_orders_clean_below_corrupt():
    clean = sanity_report(CLEAN_PT)
    corrupt = sanity_report(CORRUPT_MECHANICAL)
    assert clean.score() < corrupt.score()


def test_report_summary_is_stringable():
    assert isinstance(sanity_report(CORRUPT_MECHANICAL).summary(), str)


def test_lexical_disabled_leaves_only_structural():
    rep = sanity_report(CORRUPT_INVENTED, use_lexical=False)
    assert rep.lexical_available is False
    assert rep.invented == []


# --------------------------------------------------------------------------
# Lexical signal (aspell pt_BR — skips when unavailable)
# --------------------------------------------------------------------------

pt_speller = pytest.mark.skipif(
    not aspell_available(), reason="aspell pt_BR dictionary not installed"
)


@pt_speller
def test_invented_words_catches_non_words():
    hits = {w.lower() for w in invented_words(CORRUPT_INVENTED)}
    # at least a couple of the clearly-invented tokens must surface
    assert len({"faiança", "probababilidade", "últimesos", "pregõeles"} & hits) >= 2


@pt_speller
def test_invented_words_spares_bilingual_jargon():
    text = "o setup de breakout com bom momentum e hashrate crescente, buyback à vista"
    assert invented_words(text) == []


@pt_speller
def test_invented_words_spares_the_jargon_the_analysts_actually_write():
    """Regressão (task 20260828-004): vocabulário real dos turnos sinalizados.

    Nenhuma destas está na wordlist base do Debian, então todas contavam como
    "palavra inventada" e empurravam a taxa por cima do limiar — foi o que marcou
    2 dos 3 casos lexicais do histórico, incluindo o run do print do Samyr.
    """
    text = ("o downside vem do repricing dos megacaps: o ativo foi repriced no "
            "endpoint de segurança contra ransomware, e o contrarian aponta "
            "overvaluation com os early adopters já dentro, num setup intradiário "
            "de commoditização multiperíodo que underperformed o índice")
    assert invented_words(text) == []


@pt_speller
def test_allowlist_does_not_swallow_an_invented_pt_conjugation():
    """A lista é de palavras REAIS, não de prefixos: "repricing" passa, mas
    "reprecia" — conjugação que o modelo inventou — continua sinalizada."""
    assert [w.lower() for w in invented_words(
        "o repricing é real mas o mercado reprecia o papel todo dia " * 3
    )] .count("reprecia") >= 1


@pt_speller
def test_real_pt_garbling_still_surfaces_after_the_allowlist_grew():
    """A precisão subiu sem perder o alvo: o texto garbled continua pegando."""
    text = ("o investidor ficou frutsrado com os pregõeles e a mionha posição, "
            "aconte que a probababilidade dos últimesos não userê o comprã")
    hits = {w.lower() for w in invented_words(text)}
    assert len({"frutsrado", "pregõeles", "mionha", "probababilidade"} & hits) >= 3


@pt_speller
def test_clean_pt_has_low_invented_rate():
    rep = sanity_report(CLEAN_PT * 3)  # repeat to clear the min-words gate
    assert rep.invented_rate < 0.012


@pt_speller
def test_invented_heavy_text_is_degraded():
    # dense invented words pushes the rate over the degrade threshold
    rep = sanity_report(CORRUPT_INVENTED * 3)
    assert rep.severity in {"degraded", "suspect"}
    assert rep.invented_rate > 0.012

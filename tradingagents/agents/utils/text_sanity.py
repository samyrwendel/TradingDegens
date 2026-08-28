"""Post-generation text-sanity validator for debate turns (spec 3b, 25/08).

The bull/bear debate and the risk discussion run on the cheap *quick-think*
model (in the field: ``deepseek/deepseek-v4-flash`` via OpenRouter). On LONG
stock runs — ~900s, with market/news/fundamentals reports 2-3x larger than a
short crypto run — that model degrades into garbled pt-BR. Two distinct failure
classes were observed in the reference corpus (AAPL 1d + MSFT 4h, 2026-08-25):

* MECHANICAL artifacts — format codes leaking into prose (``"d%d%"``), embedded
  UPPERCASE case-flips (``"dezANIMAdO"``), invalid mid-token apostrophes
  (``"es'tá"``), digit-glued words (``"21por"``).
* INVENTED words — pt-orthography non-words (``"faiança"``, ``"fraustado"``,
  ``"probababilidade"``, ``"pregõeles"``).

Short clean crypto debates (~150s) show neither. This module scores a turn so
the caller can regenerate it (severe) or mark it degraded (moderate).

Two INDEPENDENT signals, so the reliable one still works if the other is
unavailable:

* STRUCTURAL — zero-dependency regex heuristics for the mechanical class. On the
  53-run reference corpus these fired on the corrupt AAPL turn and on ZERO clean
  turns (per-turn), so any structural hit is treated as high-confidence
  corruption.
* LEXICAL — an out-of-vocabulary rate for the invented-word class, using the
  system ``aspell`` pt_BR speller. A token counts as *invented* only when it is
  misspelled in Portuguese AND is not a valid English word AND is not known
  trader jargon — so bilingual finance-speak ("buyback", "hashrate") is not
  punished while "faiança"/"fraustado" is. Skipped cleanly (no error, no signal)
  when aspell or the pt_BR dictionary is absent, e.g. a stripped container.

Thresholds are conservative and overridable so a false positive costs at most
one extra regeneration, never a wrong-but-shipped turn.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache

# ---------------------------------------------------------------------------
# Tokenisation
# ---------------------------------------------------------------------------

# A token starts on an alphanumeric and may carry internal apostrophes / percent
# so the mechanical artifacts ("es'tá", "d%d%") survive as single tokens.
_TOKEN_RE = re.compile(r"[0-9A-Za-zÀ-ſ][0-9A-Za-zÀ-ſ'’%]*")
# A "word" for the lexical pass: alphabetic only, length >= 4 (short tokens are
# too noisy to spell-judge and dominated by tickers/units).
_WORD_RE = re.compile(r"[A-Za-zÀ-ſ]{4,}")

# Valid suffixes after an apostrophe: English contractions/possessives + a few
# fixed forms. Anything else after a mid-word apostrophe is corruption.
_EN_CONTRACTION = frozenset(
    {"s", "t", "d", "m", "ll", "ve", "re", "n", "clock", "em", "cause", "til", "bout"}
)
# Unit / indicator suffixes that legitimately glue onto a number ("35x", "200d",
# "50sma"). A digit followed by any of these is NOT an anomaly.
_UNIT_SUFFIX = frozenset(
    {
        "x", "h", "m", "d", "w", "y", "k", "bi", "mi", "tri", "bn", "mm", "pb",
        "bps", "pp", "yr", "q", "fy", "usd", "brl", "eur", "pt", "am", "pm",
        "ema", "sma", "wma", "ma", "rsi", "macd", "atr", "adx", "vwap",
        "th", "st", "nd", "rd",
        # Unidades técnicas que aparecem sem espaço na prosa dos analistas. Sem
        # elas o detector chamava de corrupção o nó de litografia ("2nm", "3nm")
        # e as capacidades de hardware — falso positivo puro, medido no histórico.
        "nm", "um", "cm", "km", "kg", "ton", "gb", "tb", "mb", "kb",
        "ghz", "mhz", "khz", "ms", "ns", "kw", "mw", "gw", "kwh", "mwh",
        "gbps", "mbps", "fps", "dpi", "px", "vcpu", "rpm",
    }
)
_ROMAN = frozenset("ivxlcdm")

# Trader jargon (lowercased) that is legitimately non-pt and would otherwise
# inflate the invented-word rate. Kept small and specific.
_FINANCE_ALLOW = frozenset({
    "bull", "bear", "bulls", "bears", "pump", "dump", "drawdown", "breakout",
    "breakouts", "pullback", "pullbacks", "swing", "stop", "short", "long", "hodl",
    "hype", "fomo", "fud", "bullish", "bearish", "overweight", "underweight",
    "hold", "buy", "sell", "trade", "trader", "traders", "trading", "yield",
    "staking", "stake", "airdrop", "altcoin", "crypto", "criptomoeda",
    "criptomoedas", "criptoativos", "ticker", "macd", "rsi", "ema", "sma", "wma",
    "vwap", "atr", "adx", "bollinger", "wick", "funding", "perp", "perps", "spot",
    "defi", "tvl", "apr", "apy", "ttm", "fcf", "fco", "capex", "opex", "guidance",
    "earnings", "moat", "flywheel", "momentum", "setup", "timeframe", "timeframes",
    "crossover", "overbought", "oversold", "sobrecompra", "sobrevenda",
    "sobrecomprado", "sobrecomprada", "sobrevendido", "sobrevendida", "chatbot",
    "chatbots", "blockchain", "buyback", "buybacks", "hashrate", "uptrend",
    "downtrend", "backtest", "backtesting",
    # Vocabulário que os analistas realmente escrevem e que NÃO está na wordlist
    # base do Debian (/usr/share/dict/american-english), então era contado como
    # palavra inventada e empurrava a taxa por cima do limiar. Medido nos turnos
    # sinalizados do histórico: em 2 dos 3 casos lexicais era ISTO, não corrupção.
    "downside", "upside", "contrarian", "repricing", "repriced", "reprice",
    "overvalued", "overvaluation", "undervalued", "undervaluation",
    "underperform", "underperformed", "underperformance", "outperform",
    "outperformed", "outperformance", "megacap", "megacaps", "smallcap",
    "smallcaps", "midcap", "midcaps", "adopters", "adopter", "endpoint",
    "endpoints", "ransomware", "malware", "hyperscaler", "hyperscalers",
    "churn", "backlog", "commoditização", "commoditizado", "commoditizados",
    "commoditizada", "commoditizadas", "multiperíodo", "multiperíodos",
    "intradiário", "intradiária", "intradiários", "intradiárias",
})


def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )


def tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text or "")


def words(text: str) -> list[str]:
    return _WORD_RE.findall(text or "")


# ---------------------------------------------------------------------------
# Signal 1 — structural (mechanical corruption). Zero dependencies.
# ---------------------------------------------------------------------------

def structural_anomalies(text: str) -> list[tuple[str, str]]:
    """Return ``(kind, token)`` for each mechanically-corrupt token.

    Kinds: ``fmt`` (leaked format code), ``punct`` (invalid mid-token
    apostrophe), ``caseflip`` (embedded uppercase run), ``digitglue`` (word
    fused to a number, non-unit), ``triple`` (a letter repeated 3+ times).
    High precision by design — a single hit is enough to call a turn degraded.
    """
    out: list[tuple[str, str]] = []
    for tok in tokens(text):
        # 1) printf-style format codes bleeding into prose: %d %s d%d%
        if "%d" in tok or "%s" in tok or re.search(r"\d%\d", tok) or re.search(
            r"[A-Za-zÀ-ſ]%[A-Za-zÀ-ſ]", tok
        ):
            out.append(("fmt", tok))
            continue
        # 2) mid-token apostrophe whose tail is not a valid EN contraction and
        #    whose head is not a FR/archaic elision (d'água, l'état)
        m = re.search(r"[A-Za-zÀ-ſ][’']([A-Za-zÀ-ſ]+)", tok)
        if (
            m
            and m.group(1).lower() not in _EN_CONTRACTION
            and not re.match(r"^[dlnocDLNOC][’']", tok)
        ):
            out.append(("punct", tok))
            continue
        # 3) an UPPERCASE run of 2+ embedded between lowercase — "dezANIMAdO".
        #    (iPhone / MacBook have a single embedded cap, so they don't match.)
        if re.search(r"[a-zà-ſ][A-ZÀ-Ý]{2,}[a-zà-ſ]", tok):
            out.append(("caseflip", tok))
            continue
        # 4) digits glued to a 2+ letter run that is not a known unit ("21por").
        m = re.search(r"^\D*\d+([A-Za-zÀ-ſ]{2,})$", tok)
        if m and m.group(1).lower() not in _UNIT_SUFFIX:
            out.append(("digitglue", tok))
            continue
        # 5) a letter repeated 3+ times ("fraaause"); skip pure roman numerals.
        low = tok.lower()
        if re.search(r"([a-zà-ſ])\1\1", low) and not set(low) <= _ROMAN:
            out.append(("triple", tok))
            continue
    return out


# ---------------------------------------------------------------------------
# Signal 2 — lexical (invented words). Optional: needs aspell pt_BR.
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _english_words() -> frozenset[str]:
    """Best-effort English wordlist to suppress bilingual false positives.

    Missing file is fine — the finance allowlist still covers the common jargon
    and the pt speller stays the primary judge.
    """
    for path in (
        "/usr/share/dict/american-english",
        "/usr/share/dict/words",
        "/usr/share/dict/english",
    ):
        try:
            with open(path, encoding="latin-1") as fh:
                base = {w.strip().lower() for w in fh if w.strip()}
        except OSError:
            continue
        # fold possessives ("company's" -> "company")
        base |= {w[:-2] for w in base if w.endswith("'s")}
        return frozenset(base)
    return frozenset()


@lru_cache(maxsize=1)
def aspell_available(lang: str = "pt_BR") -> bool:
    """True when the aspell binary and the requested dictionary both work."""
    if not shutil.which("aspell"):
        return False
    try:
        proc = subprocess.run(
            ["aspell", f"--lang={lang}", "--encoding=utf-8", "list"],
            input="teste",
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _aspell_misspelled(text: str, lang: str = "pt_BR") -> set[str]:
    try:
        proc = subprocess.run(
            ["aspell", f"--lang={lang}", "--encoding=utf-8", "list"],
            input=text,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if proc.returncode != 0:
        return set()
    return {w for w in proc.stdout.split("\n") if w.strip()}


def invented_words(text: str, *, lang: str = "pt_BR") -> list[str]:
    """Words misspelled in pt AND not valid English AND not trader jargon.

    Returns ``[]`` when the speller is unavailable, so the caller sees no lexical
    signal rather than a spurious one.
    """
    if not aspell_available(lang):
        return []
    bad = _aspell_misspelled(text, lang)
    if not bad:
        return []
    english = _english_words()
    hits: list[str] = []
    for w in words(text):
        if w not in bad:
            continue
        low = w.lower()
        if low in _FINANCE_ALLOW:
            continue
        if low in english or _strip_accents(low) in english:
            continue
        # A capitalized pure-ASCII token with no pt diacritics is almost always
        # an English proper noun / product name the pt speller doesn't know.
        if w[0].isupper() and w.isascii():
            continue
        hits.append(w)
    return hits


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

# Any structural hit already means corruption; these gate only the lexical rate.
INVENTED_RATE_DEGRADE = 0.02   # >= 2% invented -> regenerate
INVENTED_RATE_SUSPECT = 0.012  # >= 1.2% invented -> mark only (clean corpus tops ~1.08%)
MIN_WORDS_FOR_LEXICAL = 50     # too few words to judge a rate reliably


@dataclass
class SanityReport:
    """Verdict for one generated turn."""

    n_tokens: int
    n_words: int
    structural: list[tuple[str, str]] = field(default_factory=list)
    invented: list[str] = field(default_factory=list)
    invented_rate: float = 0.0
    lexical_available: bool = False
    severity: str = "clean"  # "clean" | "suspect" | "degraded"
    flags: list[str] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return self.severity == "degraded"

    @property
    def clean(self) -> bool:
        return self.severity == "clean"

    def score(self) -> tuple[int, float]:
        """Lower is better — for picking the healthier of two generations."""
        return (len(self.structural), self.invented_rate)

    def summary(self) -> str:
        bits = [f"severity={self.severity}"]
        if self.structural:
            sample = ", ".join(f"{k}:{v}" for k, v in self.structural[:4])
            bits.append(f"structural={len(self.structural)} [{sample}]")
        if self.lexical_available:
            bits.append(f"invented={len(self.invented)} ({self.invented_rate * 100:.2f}%)")
            if self.invented:
                bits.append("[" + ", ".join(self.invented[:4]) + "]")
        else:
            bits.append("lexical=off")
        return " ".join(bits)


def sanity_report(
    text: str,
    *,
    use_lexical: bool = True,
    invented_rate_degrade: float = INVENTED_RATE_DEGRADE,
    invented_rate_suspect: float = INVENTED_RATE_SUSPECT,
    min_words_for_lexical: int = MIN_WORDS_FOR_LEXICAL,
    lang: str = "pt_BR",
) -> SanityReport:
    """Score ``text`` for the two debate-corruption classes."""
    text = text or ""
    tok = tokens(text)
    wds = words(text)
    report = SanityReport(n_tokens=len(tok), n_words=len(wds))

    report.structural = structural_anomalies(text)
    if report.structural:
        report.severity = "degraded"
        report.flags.append("structural_artifacts")

    report.lexical_available = use_lexical and aspell_available(lang)
    if report.lexical_available and len(wds) >= min_words_for_lexical:
        report.invented = invented_words(text, lang=lang)
        report.invented_rate = len(report.invented) / len(wds)
        if report.invented_rate >= invented_rate_degrade:
            report.severity = "degraded"
            report.flags.append("high_invented_rate")
        elif report.invented_rate >= invented_rate_suspect:
            report.flags.append("elevated_invented_rate")
            if report.severity == "clean":
                report.severity = "suspect"

    return report

"""Shared 5-tier rating vocabulary and a deterministic heuristic parser.

The same five-tier scale (Buy, Overweight, Hold, Underweight, Sell) is used by:
- The Research Manager (investment plan recommendation)
- The Portfolio Manager (final position decision)
- The signal processor (rating extracted for downstream consumers)
- The memory log (rating tag stored alongside each decision entry)

Centralising it here avoids drift between those call sites.
"""

from __future__ import annotations

import re

# Canonical, ordered 5-tier scale (most bullish to most bearish).
RATINGS_5_TIER: tuple[str, ...] = (
    "Buy", "Overweight", "Hold", "Underweight", "Sell",
)

_RATING_SET = {r.lower() for r in RATINGS_5_TIER}

# Practical pt-BR meaning for each canonical rating — what to actually *do*,
# not the jargon. The report and UI show this meaning with the English
# canonical kept beside it for whoever knows the scale (same pattern the web
# UI's verdict badge uses). Keyed by the canonical English value.
RATING_PT: dict[str, str] = {
    "Buy": "COMPRAR",
    "Overweight": "AUMENTAR",
    "Hold": "MANTER",
    "Underweight": "REDUZIR",
    "Sell": "VENDER",
}


def rating_pt_label(value: str) -> str:
    """``"Underweight"`` -> ``"REDUZIR — Underweight"`` for report/UI display.

    Keeps the English canonical word verbatim (no parentheses) so
    :func:`parse_rating` still recovers it, while leading with the pt-BR meaning.
    """
    pt = RATING_PT.get(value)
    return f"{pt} — {value}" if pt else value

# Matches "Rating: X" / "rating - X" / "Rating: **X**" — tolerates markdown
# bold wrappers and either a colon or hyphen separator.
_RATING_LABEL_RE = re.compile(r"rating.*?[:\-][\s*]*(\w+)", re.IGNORECASE)


def parse_rating(text: str, default: str = "Hold") -> str:
    """Heuristically extract a 5-tier rating from prose text.

    Two-pass strategy:
    1. Look for an explicit "Rating: X" label (tolerant of markdown bold).
    2. Fall back to the first 5-tier rating word found anywhere in the text.

    Returns a Title-cased rating string, or ``default`` if no rating word appears.
    """
    for line in text.splitlines():
        m = _RATING_LABEL_RE.search(line)
        if m and m.group(1).lower() in _RATING_SET:
            return m.group(1).capitalize()

    for line in text.splitlines():
        for word in line.lower().split():
            clean = word.strip("*:.,()[]—-")
            if clean in _RATING_SET:
                return clean.capitalize()

    return default

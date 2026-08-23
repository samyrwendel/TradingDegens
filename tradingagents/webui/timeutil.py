"""Every user-facing time is America/Manaus (GMT-4) — Samyr's timezone.

Design choice (task 008 acceptance #4): the app keys *durations* off monotonic /
epoch time, which is timezone-free, and any instant is stored as an explicit,
offset-aware ISO string. But everything a human reads — the history timestamps
and, crucially, the "today" the date selector defaults to — is resolved in
America/Manaus, never UTC. At 21:30 in Manaus it is already 01:30 UTC the *next*
day; defaulting a run to the UTC date would silently analyse tomorrow. Manaus is
UTC-4 all year (no DST), but we go through the tz database rather than hardcode
-4 so it stays correct if that ever changes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

MANAUS = ZoneInfo("America/Manaus")
TZ_NAME = "America/Manaus"
TZ_LABEL = "GMT-4 (Manaus)"


def _as_aware(reference: datetime | None) -> datetime:
    """Return an aware datetime: the given reference (naive treated as UTC) or now."""
    ref = reference or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    return ref


def now() -> datetime:
    """Current instant as an aware America/Manaus datetime."""
    return datetime.now(MANAUS)


def today(reference: datetime | None = None) -> str:
    """Current calendar date (YYYY-MM-DD) in Manaus.

    ``reference`` (any aware datetime, or naive treated as UTC) lets tests pin
    the instant; it defaults to the real now.
    """
    return _as_aware(reference).astimezone(MANAUS).strftime("%Y-%m-%d")


def stamp(reference: datetime | None = None) -> str:
    """Offset-aware ISO-8601 Manaus timestamp, e.g. ``2026-08-23T21:30:00-04:00``."""
    return _as_aware(reference).astimezone(MANAUS).isoformat(timespec="seconds")


def run_id_stamp(reference: datetime | None = None) -> str:
    """Compact Manaus timestamp for run ids, e.g. ``20260823-213000``."""
    return _as_aware(reference).astimezone(MANAUS).strftime("%Y%m%d-%H%M%S")

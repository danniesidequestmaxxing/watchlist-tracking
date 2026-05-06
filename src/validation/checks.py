"""Generic validation primitives per spec §5.5.

Tests in `tests/test_validation.py` must achieve 100% line coverage here;
this module is the gate that prevents stale or bogus data from reaching
Telegram (spec §7 rule 7).
"""

import logging
import math
from collections.abc import Iterable
from datetime import UTC, date, datetime

logger = logging.getLogger(__name__)


class ValidationError(ValueError):
    """Raised when a record fails a validation check."""


def is_future(event_date: date | datetime, today: date | None = None) -> bool:
    """Return True if `event_date` is today or later.

    Datetimes are reduced to their date component before comparison. `today`
    defaults to today's UTC date; pass an explicit value for deterministic
    tests or non-UTC reference points.
    """
    if isinstance(event_date, datetime):
        event_date = event_date.date()
    if today is None:
        today = datetime.now(UTC).date()
    return event_date >= today


def is_fresh(pulled_at: datetime, ttl_seconds: int) -> bool:
    """Return True if `pulled_at` is in [now - ttl_seconds, now] UTC.

    Naive datetimes are assumed UTC. Negative TTLs are rejected.
    """
    if ttl_seconds < 0:
        raise ValidationError(f"ttl_seconds must be non-negative, got {ttl_seconds}")
    if pulled_at.tzinfo is None:
        pulled_at = pulled_at.replace(tzinfo=UTC)
    delta = (datetime.now(UTC) - pulled_at).total_seconds()
    return 0 <= delta <= ttl_seconds


def in_range(value: float | int | None, lo: float, hi: float) -> bool:
    """Return True if `value` is finite and lies within [lo, hi]."""
    if value is None:
        return False
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(f):
        return False
    return lo <= f <= hi


def corroborate(events_by_source: dict[str, Iterable[date]]) -> bool:
    """Return True if at least one date is reported by ≥2 distinct sources.

    Used for high-stakes events (e.g. FOMC) where a single-source date
    is too thin a signal to ship.
    """
    if len(events_by_source) < 2:
        return False
    by_date: dict[date, set[str]] = {}
    for source, dates in events_by_source.items():
        for d in dates:
            by_date.setdefault(d, set()).add(source)
    return any(len(srcs) >= 2 for srcs in by_date.values())

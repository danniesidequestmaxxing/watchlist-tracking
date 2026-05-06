"""Market-hours predicates for the scheduler."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from src.utils.timestamps import (
    ET,
    KL,
    is_my_market_open,
    is_us_market_open,
    is_weekday,
)


def _et(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ET)


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# ---------------------------------------------------------------------------
# is_us_market_open
# ---------------------------------------------------------------------------


def test_us_market_open_at_930_et() -> None:
    """Tuesday May 5 2026 09:30 ET — open."""
    assert is_us_market_open(_et(2026, 5, 5, 9, 30)) is True


def test_us_market_open_just_before_open_et() -> None:
    assert is_us_market_open(_et(2026, 5, 5, 9, 29)) is False


def test_us_market_closed_at_close_et() -> None:
    """16:00 ET is the close — not open."""
    assert is_us_market_open(_et(2026, 5, 5, 16, 0)) is False


def test_us_market_open_at_1559_et() -> None:
    assert is_us_market_open(_et(2026, 5, 5, 15, 59)) is True


def test_us_market_closed_weekend() -> None:
    """Saturday May 9 2026 mid-session ET — closed."""
    assert is_us_market_open(_et(2026, 5, 9, 12, 0)) is False
    assert is_us_market_open(_et(2026, 5, 10, 12, 0)) is False  # Sunday


def test_us_market_open_handles_utc_input() -> None:
    """Tuesday May 5 2026 14:00 UTC = 10:00 ET (during DST) — open."""
    assert is_us_market_open(_utc(2026, 5, 5, 14, 0)) is True


def test_us_market_open_default_now() -> None:
    """No-arg call should not crash; result is a bool."""
    assert isinstance(is_us_market_open(), bool)


# ---------------------------------------------------------------------------
# is_my_market_open
# ---------------------------------------------------------------------------


def test_my_market_open_in_window() -> None:
    """Tuesday 03:00 UTC weekday — open."""
    assert is_my_market_open(_utc(2026, 5, 5, 3, 0)) is True


def test_my_market_at_open_boundary() -> None:
    """01:00 UTC inclusive."""
    assert is_my_market_open(_utc(2026, 5, 5, 1, 0)) is True


def test_my_market_at_close_boundary() -> None:
    """09:00 UTC exclusive."""
    assert is_my_market_open(_utc(2026, 5, 5, 9, 0)) is False


def test_my_market_before_open() -> None:
    assert is_my_market_open(_utc(2026, 5, 5, 0, 30)) is False


def test_my_market_after_close() -> None:
    assert is_my_market_open(_utc(2026, 5, 5, 9, 30)) is False


def test_my_market_weekend_closed() -> None:
    """Saturday during the would-be window."""
    assert is_my_market_open(_utc(2026, 5, 9, 3, 0)) is False


def test_my_market_default_now() -> None:
    assert isinstance(is_my_market_open(), bool)


# ---------------------------------------------------------------------------
# is_weekday
# ---------------------------------------------------------------------------


def test_is_weekday_monday_to_friday() -> None:
    assert is_weekday(_utc(2026, 5, 4, 12, 0)) is True  # Mon
    assert is_weekday(_utc(2026, 5, 8, 12, 0)) is True  # Fri


def test_is_weekday_weekend() -> None:
    assert is_weekday(_utc(2026, 5, 9, 12, 0)) is False  # Sat
    assert is_weekday(_utc(2026, 5, 10, 12, 0)) is False  # Sun


# ---------------------------------------------------------------------------
# Sanity on the timezone constants
# ---------------------------------------------------------------------------


def test_timezone_constants_match_iana() -> None:
    assert ZoneInfo("America/New_York") == ET
    assert ZoneInfo("Asia/Kuala_Lumpur") == KL

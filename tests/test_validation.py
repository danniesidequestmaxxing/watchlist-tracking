"""100% line coverage of src/validation/checks.py per spec §7 rule 7."""

from datetime import UTC, date, datetime, timedelta

import pytest

from src.validation.checks import (
    ValidationError,
    corroborate,
    in_range,
    is_fresh,
    is_future,
)

# ---------------------------------------------------------------------------
# is_future
# ---------------------------------------------------------------------------


def test_is_future_today_inclusive() -> None:
    today = date(2026, 5, 6)
    assert is_future(today, today=today) is True


def test_is_future_past_rejected() -> None:
    today = date(2026, 5, 6)
    assert is_future(date(2026, 5, 5), today=today) is False


def test_is_future_future_accepted() -> None:
    today = date(2026, 5, 6)
    assert is_future(date(2026, 5, 7), today=today) is True


def test_is_future_accepts_datetime() -> None:
    today = date(2026, 5, 6)
    assert is_future(datetime(2026, 5, 6, 8, 30), today=today) is True
    assert is_future(datetime(2026, 5, 5, 23, 59), today=today) is False


def test_is_future_default_today() -> None:
    """Default `today` argument falls back to today's UTC date."""
    assert is_future(date.today() + timedelta(days=1)) is True


# ---------------------------------------------------------------------------
# is_fresh
# ---------------------------------------------------------------------------


def test_is_fresh_within_ttl() -> None:
    pulled = datetime.now(UTC) - timedelta(seconds=30)
    assert is_fresh(pulled, ttl_seconds=60) is True


def test_is_fresh_at_now_boundary() -> None:
    pulled = datetime.now(UTC)
    assert is_fresh(pulled, ttl_seconds=60) is True


def test_is_fresh_naive_datetime_assumed_utc() -> None:
    pulled = datetime.utcnow() - timedelta(seconds=10)  # naive
    assert is_fresh(pulled, ttl_seconds=60) is True


def test_is_fresh_outside_ttl() -> None:
    pulled = datetime.now(UTC) - timedelta(hours=2)
    assert is_fresh(pulled, ttl_seconds=60 * 60) is False


def test_is_fresh_future_pulled_at_is_stale() -> None:
    """A pulled_at that lies in the future means clock skew or bad data
    — treat as not fresh rather than as 'fresher than now'."""
    pulled = datetime.now(UTC) + timedelta(seconds=60)
    assert is_fresh(pulled, ttl_seconds=60 * 60) is False


def test_is_fresh_negative_ttl_raises() -> None:
    with pytest.raises(ValidationError):
        is_fresh(datetime.now(UTC), ttl_seconds=-1)


# ---------------------------------------------------------------------------
# in_range
# ---------------------------------------------------------------------------


def test_in_range_inside() -> None:
    assert in_range(50.0, 0, 100) is True


def test_in_range_at_bounds() -> None:
    assert in_range(0, 0, 100) is True
    assert in_range(100, 0, 100) is True


def test_in_range_outside() -> None:
    assert in_range(150.0, 0, 100) is False
    assert in_range(-1.0, 0, 100) is False


def test_in_range_none_returns_false() -> None:
    assert in_range(None, 0, 100) is False


def test_in_range_nan_returns_false() -> None:
    assert in_range(float("nan"), 0, 100) is False


def test_in_range_inf_returns_false() -> None:
    assert in_range(float("inf"), 0, 100) is False
    assert in_range(float("-inf"), 0, 100) is False


def test_in_range_non_numeric_returns_false() -> None:
    assert in_range("x", 0, 100) is False  # type: ignore[arg-type]


def test_in_range_accepts_int() -> None:
    assert in_range(42, 0, 100) is True


# ---------------------------------------------------------------------------
# corroborate
# ---------------------------------------------------------------------------


def test_corroborate_two_sources_agree() -> None:
    assert (
        corroborate(
            {
                "tradingeconomics": [date(2026, 5, 7)],
                "fed": [date(2026, 5, 7)],
            }
        )
        is True
    )


def test_corroborate_two_sources_disagree() -> None:
    assert (
        corroborate(
            {
                "tradingeconomics": [date(2026, 5, 7)],
                "fed": [date(2026, 5, 8)],
            }
        )
        is False
    )


def test_corroborate_single_source_returns_false() -> None:
    assert corroborate({"only": [date(2026, 5, 7)]}) is False


def test_corroborate_empty_returns_false() -> None:
    assert corroborate({}) is False


def test_corroborate_three_sources_partial_overlap() -> None:
    """Two of three sources agreeing on a date is enough."""
    assert (
        corroborate(
            {
                "a": [date(2026, 5, 7), date(2026, 5, 9)],
                "b": [date(2026, 5, 8)],
                "c": [date(2026, 5, 9)],
            }
        )
        is True
    )


def test_corroborate_handles_iterables() -> None:
    """`events_by_source` accepts any iterable of dates (not just lists)."""
    assert (
        corroborate(
            {
                "a": iter([date(2026, 5, 7)]),
                "b": iter([date(2026, 5, 7)]),
            }
        )
        is True
    )


# ---------------------------------------------------------------------------
# ValidationError surface area
# ---------------------------------------------------------------------------


def test_validation_error_is_value_error_subclass() -> None:
    assert issubclass(ValidationError, ValueError)

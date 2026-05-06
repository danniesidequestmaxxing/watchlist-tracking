"""KL/UTC helpers + market-hours predicates per spec §5.7.

Holiday calendars are not modelled here — we treat every weekday as a trading
day. Edge cases (Federal holidays, Bursa holidays) currently accept the date
and rely on the upstream data adapter to return an empty payload, which the
TA pipeline then drops via `validate_ta_snapshot`.
"""

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
KL = ZoneInfo("Asia/Kuala_Lumpur")

US_OPEN = time(9, 30)
US_CLOSE = time(16, 0)


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_kl() -> datetime:
    return datetime.now(KL)


def is_weekday(dt: datetime) -> bool:
    """Mon=0..Fri=4 are trading weekdays."""
    return dt.weekday() < 5


def is_us_market_open(now: datetime | None = None) -> bool:
    """Regular NYSE/NASDAQ session, 9:30-16:00 ET, Mon-Fri.

    Holiday calendar is not enforced; relies on yfinance returning sparse
    data on holidays.
    """
    if now is None:
        now = now_utc()
    et = now.astimezone(ET)
    if not is_weekday(et):
        return False
    return US_OPEN <= et.time() < US_CLOSE


def is_my_market_open(now: datetime | None = None) -> bool:
    """Bursa Malaysia open window per spec §5.7: 01:00-09:00 UTC weekdays."""
    if now is None:
        now = now_utc()
    utc = now.astimezone(UTC)
    if not is_weekday(utc):
        return False
    t = utc.time()
    return time(1, 0) <= t < time(9, 0)

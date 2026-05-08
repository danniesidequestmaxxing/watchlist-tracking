"""KL/UTC helpers + market-hours predicates per spec §5.7.

Holiday calendars are not modelled here — we treat every weekday as a trading
day. Edge cases (Federal holidays, Bursa holidays) currently accept the date
and rely on the upstream data adapter to return an empty payload, which the
TA pipeline then drops via `validate_ta_snapshot`.
"""

import re
from datetime import UTC, datetime, time, timedelta
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


def is_kr_market_open(now: datetime | None = None) -> bool:
    """KRX (KOSPI/KOSDAQ): 09:00-15:30 KST = 00:00-06:30 UTC weekdays."""
    if now is None:
        now = now_utc()
    utc = now.astimezone(UTC)
    if not is_weekday(utc):
        return False
    t = utc.time()
    return time(0, 0) <= t < time(6, 30)


def is_jp_market_open(now: datetime | None = None) -> bool:
    """TSE: 09:00-15:00 JST with a 11:30-12:30 lunch break = 00:00-06:00 UTC weekdays.

    The lunch break is intentionally not modelled — yfinance returns sparse data
    over it and the TA validator drops the snapshot.
    """
    if now is None:
        now = now_utc()
    utc = now.astimezone(UTC)
    if not is_weekday(utc):
        return False
    t = utc.time()
    return time(0, 0) <= t < time(6, 0)


def is_tw_market_open(now: datetime | None = None) -> bool:
    """TWSE: 09:00-13:30 Taipei = 01:00-05:30 UTC weekdays."""
    if now is None:
        now = now_utc()
    utc = now.astimezone(UTC)
    if not is_weekday(utc):
        return False
    t = utc.time()
    return time(1, 0) <= t < time(5, 30)


def is_cn_market_open(now: datetime | None = None) -> bool:
    """SSE/SZSE: 09:30-11:30 + 13:00-15:00 China = 01:30-03:30 + 05:00-07:00 UTC.

    Modelled as the wider 01:30-07:00 UTC window; the lunch break gap is left
    to the data adapter (sparse rows get dropped by validation).
    """
    if now is None:
        now = now_utc()
    utc = now.astimezone(UTC)
    if not is_weekday(utc):
        return False
    t = utc.time()
    return time(1, 30) <= t < time(7, 0)


_DURATION_RE = re.compile(r"^(\d+)\s*([mhd])$", re.IGNORECASE)
_UNIT_TO_KW = {"m": "minutes", "h": "hours", "d": "days"}


def parse_duration(raw: str) -> timedelta | None:
    """Parse `30m`, `24h`, `7d` (case-insensitive) into a timedelta.

    Returns None for any other shape so callers can surface a usage error.
    """
    if not raw:
        return None
    m = _DURATION_RE.match(raw.strip())
    if not m:
        return None
    n = int(m.group(1))
    if n <= 0:
        return None
    unit = m.group(2).lower()
    return timedelta(**{_UNIT_TO_KW[unit]: n})


def fmt_duration(delta: timedelta) -> str:
    """Render a timedelta as `Nm` / `Nh` / `Nd` (smallest unit that fits)."""
    secs = max(0, int(delta.total_seconds()))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"

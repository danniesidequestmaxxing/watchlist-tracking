import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from src.validation.checks import ValidationError, is_fresh

logger = logging.getLogger(__name__)


TTL_SECONDS: dict[str, int] = {
    "ohlcv_1h": 60 * 60,
    "ta_1h": 60 * 60,
    "live_price": 60,
    "token_unlocks": 24 * 60 * 60,
    "earnings_calendar": 6 * 60 * 60,
    "macro_events": 24 * 60 * 60,
    "news": 30 * 60,
    "sec_filings": 60 * 60,
    "options_expiry": 60 * 60,
}

# Tolerated clock skew when accepting a `pulled_at` from an external source.
WRITE_FUTURE_SKEW = timedelta(seconds=60)


def _parse_ts(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


async def write(
    db_path: Path,
    cache_key: str,
    data_type: str,
    payload: dict[str, Any],
    pulled_at: datetime,
    source_url: str | None,
    ticker: str | None = None,
    source_published_at: datetime | None = None,
) -> None:
    """Insert or replace a cache entry.

    Rejects writes whose `pulled_at` is in the future beyond a 60-second clock
    skew, or whose payload is empty. Rejects unknown `data_type` values to
    avoid orphan rows that downstream readers can't TTL.
    """
    if not payload:
        raise ValidationError(f"refusing to cache empty payload for {cache_key}")
    if data_type not in TTL_SECONDS:
        raise ValidationError(f"unknown data_type {data_type!r} for {cache_key}")
    if pulled_at.tzinfo is None:
        pulled_at = pulled_at.replace(tzinfo=UTC)
    if pulled_at - datetime.now(UTC) > WRITE_FUTURE_SKEW:
        raise ValidationError(
            f"refusing to cache {cache_key}: pulled_at {pulled_at.isoformat()} is in the future"
        )

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO last_fetched
              (cache_key, data_type, ticker, pulled_at, payload, source_url, source_published_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
              data_type=excluded.data_type,
              ticker=excluded.ticker,
              pulled_at=excluded.pulled_at,
              payload=excluded.payload,
              source_url=excluded.source_url,
              source_published_at=excluded.source_published_at
            """,
            (
                cache_key,
                data_type,
                ticker,
                pulled_at.isoformat(),
                json.dumps(payload, default=str),
                source_url,
                source_published_at.isoformat() if source_published_at else None,
            ),
        )
        await db.commit()


async def read_if_fresh(
    db_path: Path,
    cache_key: str,
    ttl_seconds: int | None = None,
) -> dict[str, Any] | None:
    """Return parsed payload if the cache row exists and is within TTL, else None.

    If `ttl_seconds` is omitted, the TTL is looked up by `data_type` from the
    `TTL_SECONDS` map. Rows with an unknown `data_type` are treated as stale.
    """
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT data_type, pulled_at, payload FROM last_fetched WHERE cache_key = ?",
            (cache_key,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    data_type, pulled_at_s, payload_s = row
    ttl = ttl_seconds if ttl_seconds is not None else TTL_SECONDS.get(data_type)
    if ttl is None:
        logger.warning(
            "Cache key %s has unknown data_type %s; treating as stale", cache_key, data_type
        )
        return None
    if not is_fresh(_parse_ts(pulled_at_s), ttl):
        return None
    return json.loads(payload_s)


async def read_meta(db_path: Path, cache_key: str) -> dict[str, Any] | None:
    """Return cache metadata (data_type, pulled_at, source_url) without TTL check."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT data_type, pulled_at, source_url FROM last_fetched WHERE cache_key = ?",
            (cache_key,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return {
        "data_type": row[0],
        "pulled_at": _parse_ts(row[1]),
        "source_url": row[2],
    }

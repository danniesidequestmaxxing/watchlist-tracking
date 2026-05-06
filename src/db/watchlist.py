import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class WatchlistEntry(BaseModel):
    id: int
    user_id: int
    ticker: str
    asset_class: str
    exchange: str | None = None
    notes: str | None = None
    added_at: str
    ta_enabled: bool = True
    catalyst_enabled: bool = True
    alert_thresholds: str | None = None
    muted_until: str | None = None


def _parse_muted_until(entry: WatchlistEntry) -> datetime | None:
    if not entry.muted_until:
        return None
    try:
        until = datetime.fromisoformat(entry.muted_until)
    except (TypeError, ValueError):
        logger.warning("Invalid muted_until on %s: %r", entry.ticker, entry.muted_until)
        return None
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    return until


def is_muted(entry: WatchlistEntry) -> bool:
    """Return True if `entry.muted_until` parses to a future timestamp."""
    until = _parse_muted_until(entry)
    return until is not None and until > datetime.now(UTC)


def mute_remaining(entry: WatchlistEntry) -> timedelta | None:
    """Return how long the mute has left, or None if not muted."""
    until = _parse_muted_until(entry)
    if until is None:
        return None
    remaining = until - datetime.now(UTC)
    return remaining if remaining.total_seconds() > 0 else None


async def set_mute(
    db_path: Path,
    user_id: int,
    ticker: str,
    until: datetime,
) -> int:
    """Set muted_until for `ticker`. Returns rows affected (0 if not on watchlist)."""
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "UPDATE watchlist SET muted_until = ? WHERE user_id = ? AND UPPER(ticker) = UPPER(?)",
            (until.isoformat(), user_id, ticker),
        )
        await db.commit()
        return cursor.rowcount


async def clear_mute(db_path: Path, user_id: int, ticker: str) -> int:
    """Clear muted_until for `ticker`. Returns rows affected (0 if not on watchlist)."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "UPDATE watchlist SET muted_until = NULL "
            "WHERE user_id = ? AND UPPER(ticker) = UPPER(?)",
            (user_id, ticker),
        )
        await db.commit()
        return cursor.rowcount


async def add_entry(
    db_path: Path,
    user_id: int,
    ticker: str,
    asset_class: str,
    exchange: str | None = None,
    notes: str | None = None,
) -> int | None:
    """Insert a row. Returns new id, or None if it would violate the UNIQUE constraint.

    Empty string is stored when `exchange` is None so the (user_id, ticker,
    exchange) UNIQUE constraint actually rejects duplicate non-crypto entries
    — SQLite treats NULL as distinct from NULL in UNIQUE indexes.
    """
    exchange_val = exchange or ""
    async with aiosqlite.connect(db_path) as db:
        try:
            cursor = await db.execute(
                """
                INSERT INTO watchlist (user_id, ticker, asset_class, exchange, notes)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, ticker, asset_class, exchange_val, notes),
            )
            await db.commit()
            return cursor.lastrowid
        except aiosqlite.IntegrityError as exc:
            logger.info("Duplicate watchlist entry rejected: %s", exc)
            return None


async def remove_entry(db_path: Path, user_id: int, ticker: str) -> int:
    """Delete by ticker (case-insensitive). Returns rows affected."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "DELETE FROM watchlist WHERE user_id = ? AND UPPER(ticker) = UPPER(?)",
            (user_id, ticker),
        )
        await db.commit()
        return cursor.rowcount


async def list_entries(db_path: Path, user_id: int) -> list[WatchlistEntry]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, user_id, ticker, asset_class, exchange, notes, added_at,
                   ta_enabled, catalyst_enabled, alert_thresholds, muted_until
            FROM watchlist
            WHERE user_id = ?
            ORDER BY added_at ASC
            """,
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [WatchlistEntry.model_validate(dict(row)) for row in rows]


async def find_entry(
    db_path: Path,
    user_id: int,
    ticker: str,
) -> WatchlistEntry | None:
    """Return the first entry matching (user_id, ticker) case-insensitively."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, user_id, ticker, asset_class, exchange, notes, added_at,
                   ta_enabled, catalyst_enabled, alert_thresholds, muted_until
            FROM watchlist
            WHERE user_id = ? AND UPPER(ticker) = UPPER(?)
            ORDER BY added_at ASC
            LIMIT 1
            """,
            (user_id, ticker),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return WatchlistEntry.model_validate(dict(row))


async def count_entries(db_path: Path, user_id: int) -> int:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM watchlist WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

import logging
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


async def add_entry(
    db_path: Path,
    user_id: int,
    ticker: str,
    asset_class: str,
    exchange: str | None = None,
    notes: str | None = None,
) -> int | None:
    """Insert a row. Returns new id, or None if it would violate the UNIQUE constraint."""
    async with aiosqlite.connect(db_path) as db:
        try:
            cursor = await db.execute(
                """
                INSERT INTO watchlist (user_id, ticker, asset_class, exchange, notes)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, ticker, asset_class, exchange, notes),
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


async def count_entries(db_path: Path, user_id: int) -> int:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM watchlist WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

"""Forward-target chat IDs that receive every scheduled push from the bot.

Auto-pushes (TA cron, daily digest, EDGAR alerts, TradingView webhook) fan
out to OWNER_TELEGRAM_ID plus every chat_id in this table. Interactive
command output (/snapshot, /catalyst, etc.) still replies only to the
originating chat.
"""

import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)


async def add_target(db_path: Path, chat_id: int, label: str | None = None) -> bool:
    """Insert a forward target. Returns True if inserted, False if it already existed."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO forward_targets (chat_id, label) VALUES (?, ?)",
            (chat_id, label),
        )
        await db.commit()
        return cursor.rowcount > 0


async def remove_target(db_path: Path, chat_id: int) -> int:
    """Delete by chat_id. Returns rows affected."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "DELETE FROM forward_targets WHERE chat_id = ?",
            (chat_id,),
        )
        await db.commit()
        return cursor.rowcount


async def list_targets(db_path: Path) -> list[dict]:
    """Return [{chat_id, label, added_at}, ...] ordered by added_at."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT chat_id, label, added_at FROM forward_targets ORDER BY added_at ASC"
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def target_chat_ids(db_path: Path) -> list[int]:
    """Return just the chat_ids — used by the broadcast fan-out."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT chat_id FROM forward_targets")
        rows = await cursor.fetchall()
    return [int(row[0]) for row in rows]

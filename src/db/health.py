import logging
from pathlib import Path
from typing import Literal

import aiosqlite

logger = logging.getLogger(__name__)


HealthStatus = Literal["ok", "error", "rate_limited"]


async def record(
    db_path: Path,
    source: str,
    status: HealthStatus,
    details: str | None = None,
) -> None:
    """Append a row to `health_log`. Never raises — the bot keeps running."""
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO health_log (source, status, details) VALUES (?, ?, ?)",
                (source, status, details),
            )
            await db.commit()
    except Exception as exc:
        logger.warning("Failed to write health_log row for %s: %s", source, exc)

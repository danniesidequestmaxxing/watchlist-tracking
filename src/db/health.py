import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

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


def _parse_ts(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


async def latest_per_source(db_path: Path) -> dict[str, dict[str, Any]]:
    """Return the latest health_log row for every distinct source.

    Each value is a dict with `status`, `details`, `recorded_at` (datetime).
    """
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT source, status, details, recorded_at
            FROM health_log
            WHERE id IN (SELECT MAX(id) FROM health_log GROUP BY source)
            ORDER BY source ASC
            """
        )
        rows = await cursor.fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        out[row["source"]] = {
            "status": row["status"],
            "details": row["details"],
            "recorded_at": _parse_ts(row["recorded_at"]),
        }
    return out

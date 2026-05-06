"""Catalyst event persistence to the `catalyst_events` table."""

import logging
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)


async def save_events(
    db_path: Path,
    ticker: str,
    confirmed: Iterable,
    expected: Iterable,
    speculative: Iterable,
) -> int:
    """Insert one row per event across all three confidence tiers.

    Each event is duck-typed: must expose `event_type`, `event_date`,
    `description`, `source_url`, `source_pulled_at`.
    """
    rows: list[tuple] = []
    for tier_label, tier in (
        ("confirmed", confirmed),
        ("expected", expected),
        ("speculative", speculative),
    ):
        for event in tier:
            rows.append(
                (
                    ticker.upper(),
                    event.event_type,
                    event.event_date.isoformat(),
                    tier_label,
                    event.description,
                    str(event.source_url),
                    event.source_pulled_at.isoformat(),
                )
            )
    if not rows:
        return 0
    async with aiosqlite.connect(db_path) as db:
        await db.executemany(
            """
            INSERT INTO catalyst_events
              (ticker, event_type, event_date, confidence,
               description, source_url, source_pulled_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        await db.commit()
    logger.info("Persisted %d catalyst events for %s", len(rows), ticker)
    return len(rows)


async def next_event_for(db_path: Path, ticker: str, today: str | None = None) -> dict | None:
    """Return the soonest future catalyst row for `ticker`, or None."""
    today_iso = today or datetime.utcnow().date().isoformat()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT event_type, event_date, confidence, description,
                   source_url, source_pulled_at
            FROM catalyst_events
            WHERE UPPER(ticker) = UPPER(?) AND event_date >= ?
            ORDER BY event_date ASC, ingested_at DESC
            LIMIT 1
            """,
            (ticker, today_iso),
        )
        row = await cursor.fetchone()
    return dict(row) if row else None

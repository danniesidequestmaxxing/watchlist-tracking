"""Scheduled jobs per spec §5.7.

Phase 4 wires `ta_refresh_crypto` and `ta_refresh_equity_us`. Other jobs
(catalyst refresh, daily digest, health heartbeat) are registered by later
phases.
"""

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.constants import ParseMode

from src.config import DB_PATH, OWNER_TELEGRAM_ID
from src.db import health
from src.db.watchlist import WatchlistEntry, list_entries
from src.ta.pipeline import get_crypto_snapshot, get_equity_snapshot
from src.telegram.formatters import format_ta_snapshot
from src.utils.timestamps import is_us_market_open

if TYPE_CHECKING:
    from telegram.ext import Application

logger = logging.getLogger(__name__)


def _is_muted(entry: WatchlistEntry) -> bool:
    if not entry.muted_until:
        return False
    try:
        until = datetime.fromisoformat(entry.muted_until)
    except ValueError:
        logger.warning("Invalid muted_until on %s: %r", entry.ticker, entry.muted_until)
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    return until > datetime.now(UTC)


async def _push_snapshot(app: "Application", entry: WatchlistEntry) -> None:
    """Fetch a fresh snapshot for one entry and DM it to the owner."""
    try:
        if entry.asset_class == "crypto":
            assert entry.exchange is not None
            snap = await get_crypto_snapshot(
                DB_PATH, entry.ticker, entry.exchange, force_refresh=True
            )
            source_label = f"ccxt:{entry.exchange}"
        elif entry.asset_class == "equity_us":
            snap = await get_equity_snapshot(DB_PATH, entry.ticker, force_refresh=True)
            source_label = "yfinance"
        else:
            return
    except Exception as exc:
        logger.exception("TA refresh failed for %s", entry.ticker)
        await health.record(
            DB_PATH,
            source="yfinance" if entry.asset_class == "equity_us" else f"ccxt:{entry.exchange}",
            status="error",
            details=f"{entry.ticker}: {exc}",
        )
        return

    await health.record(DB_PATH, source=source_label, status="ok", details=entry.ticker)
    text = format_ta_snapshot(snap)
    try:
        await app.bot.send_message(chat_id=OWNER_TELEGRAM_ID, text=text, parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.warning("Telegram send failed for %s: %s", entry.ticker, exc)


async def refresh_crypto_ta(app: "Application") -> None:
    """Hourly fetch for every active crypto ticker on the watchlist."""
    logger.info("ta_refresh_crypto tick")
    entries = await list_entries(DB_PATH, OWNER_TELEGRAM_ID)
    for entry in entries:
        if entry.asset_class != "crypto" or not entry.exchange:
            continue
        if not entry.ta_enabled or _is_muted(entry):
            continue
        await _push_snapshot(app, entry)


async def refresh_equity_us_ta(app: "Application") -> None:
    """Hourly equity_us fetch, suppressed outside the regular US session."""
    if not is_us_market_open():
        logger.info("ta_refresh_equity_us: US market closed, suppressing")
        return
    logger.info("ta_refresh_equity_us tick")
    entries = await list_entries(DB_PATH, OWNER_TELEGRAM_ID)
    for entry in entries:
        if entry.asset_class != "equity_us":
            continue
        if not entry.ta_enabled or _is_muted(entry):
            continue
        await _push_snapshot(app, entry)


def register_jobs(scheduler: AsyncIOScheduler, app: "Application") -> None:
    """Register Phase 4 jobs on the given scheduler.

    Cron offsets per spec §5.7: crypto :05, equity_us :10. 60s of jitter
    per spec §5.5 to avoid synchronized API hits.
    """
    scheduler.add_job(
        refresh_crypto_ta,
        args=[app],
        trigger="cron",
        minute=5,
        jitter=60,
        id="ta_refresh_crypto",
        replace_existing=True,
    )
    scheduler.add_job(
        refresh_equity_us_ta,
        args=[app],
        trigger="cron",
        minute=10,
        jitter=60,
        id="ta_refresh_equity_us",
        replace_existing=True,
    )
    logger.info("Scheduler jobs registered: %s", [j.id for j in scheduler.get_jobs()])

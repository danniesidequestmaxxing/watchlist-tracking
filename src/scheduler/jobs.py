"""Scheduled jobs per spec §5.7.

Phase 4 wired ta_refresh_crypto + ta_refresh_equity_us. Phase 7 adds the
catalyst refresh trio and the 07:00 KL daily_digest. The Bursa equity_my
refresh is gated on Phase 10 (no adapter yet); the health heartbeat is
gated on Phase 8.
"""

import logging
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.constants import ParseMode

from src.adapters import finnhub, token_unlocks, trading_economics
from src.catalyst.agent import run_catalyst_agent
from src.config import DB_PATH, KL_TZ, OWNER_TELEGRAM_ID
from src.db import catalysts, health
from src.db.watchlist import WatchlistEntry, is_muted, list_entries
from src.ta.pipeline import get_crypto_snapshot, get_equity_snapshot
from src.telegram.formatters import format_catalyst_output, format_ta_snapshot
from src.utils.timestamps import is_us_market_open, now_kl

if TYPE_CHECKING:
    from telegram.ext import Application

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _send(app: "Application", text: str) -> None:
    try:
        await app.bot.send_message(chat_id=OWNER_TELEGRAM_ID, text=text, parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.warning("Telegram send failed: %s", exc)


async def _push_snapshot(app: "Application", entry: WatchlistEntry) -> None:
    """Fetch a fresh TA snapshot and DM it to the owner."""
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
    await _send(app, format_ta_snapshot(snap))


async def _push_catalyst(app: "Application", entry: WatchlistEntry) -> None:
    """Run the catalyst agent for one entry, persist events, and DM the digest."""
    try:
        out = await run_catalyst_agent(entry.ticker, entry.asset_class, DB_PATH)
    except Exception as exc:
        logger.exception("Catalyst fetch failed for %s", entry.ticker)
        await health.record(
            DB_PATH,
            source="catalyst-agent",
            status="error",
            details=f"{entry.ticker}: {exc}",
        )
        return

    try:
        await catalysts.save_events(
            DB_PATH, entry.ticker, out.confirmed, out.expected, out.speculative
        )
    except Exception:
        logger.exception("Failed to persist catalyst events for %s", entry.ticker)

    await _send(app, format_catalyst_output(out))


# ---------------------------------------------------------------------------
# TA jobs (Phase 4)
# ---------------------------------------------------------------------------


async def refresh_crypto_ta(app: "Application") -> None:
    """Hourly fetch for every active crypto ticker on the watchlist."""
    logger.info("ta_refresh_crypto tick")
    entries = await list_entries(DB_PATH, OWNER_TELEGRAM_ID)
    for entry in entries:
        if entry.asset_class != "crypto" or not entry.exchange:
            continue
        if not entry.ta_enabled or is_muted(entry):
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
        if not entry.ta_enabled or is_muted(entry):
            continue
        await _push_snapshot(app, entry)


# ---------------------------------------------------------------------------
# Phase 7 — daily digest + per-source catalyst refresh
# ---------------------------------------------------------------------------


async def daily_digest(app: "Application") -> None:
    """07:00 KL — catalyst pull for the entire watchlist."""
    logger.info("daily_digest tick")
    entries = await list_entries(DB_PATH, OWNER_TELEGRAM_ID)
    if not entries:
        return
    header = f"🌅 <b>Morning catalyst digest</b> — {now_kl().strftime('%a %b %d')}"
    await _send(app, header)
    for entry in entries:
        if not entry.catalyst_enabled or is_muted(entry):
            continue
        await _push_catalyst(app, entry)


async def catalyst_refresh_unlocks(app: "Application") -> None:
    """06:00 KL — pre-warm token-unlock cache for crypto entries."""
    logger.info("catalyst_refresh_unlocks tick")
    entries = await list_entries(DB_PATH, OWNER_TELEGRAM_ID)
    for entry in entries:
        if entry.asset_class != "crypto":
            continue
        try:
            await token_unlocks.get_token_unlocks(DB_PATH, entry.ticker, days_ahead=30)
        except Exception:
            logger.exception("token_unlocks refresh failed for %s", entry.ticker)


async def catalyst_refresh_earnings(app: "Application") -> None:
    """18:00 KL — pre-warm earnings cache for US equities."""
    logger.info("catalyst_refresh_earnings tick")
    entries = await list_entries(DB_PATH, OWNER_TELEGRAM_ID)
    for entry in entries:
        if entry.asset_class != "equity_us":
            continue
        try:
            await finnhub.get_earnings_calendar(DB_PATH, entry.ticker, days_ahead=60)
        except Exception:
            logger.exception("earnings refresh failed for %s", entry.ticker)


async def catalyst_refresh_macro(app: "Application") -> None:
    """06:00 + 18:00 KL — refresh the macro calendar (one global pull)."""
    logger.info("catalyst_refresh_macro tick")
    try:
        await trading_economics.get_macro_events(DB_PATH, days_ahead=14)
    except Exception:
        logger.exception("macro refresh failed")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_jobs(scheduler: AsyncIOScheduler, app: "Application") -> None:
    """Wire every job from spec §5.7 (minus equity_my and health heartbeat).

    All jitter values are 60s per §5.5; KL-anchored jobs use the configured
    `KL_TZ` so they fire at the correct local-clock time regardless of host
    timezone.
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
    scheduler.add_job(
        catalyst_refresh_unlocks,
        args=[app],
        trigger="cron",
        hour=6,
        minute=0,
        timezone=KL_TZ,
        jitter=60,
        id="catalyst_refresh_unlocks",
        replace_existing=True,
    )
    scheduler.add_job(
        catalyst_refresh_macro,
        args=[app],
        trigger="cron",
        hour="6,18",
        minute=0,
        timezone=KL_TZ,
        jitter=60,
        id="catalyst_refresh_macro",
        replace_existing=True,
    )
    scheduler.add_job(
        catalyst_refresh_earnings,
        args=[app],
        trigger="cron",
        hour=18,
        minute=0,
        timezone=KL_TZ,
        jitter=60,
        id="catalyst_refresh_earnings",
        replace_existing=True,
    )
    scheduler.add_job(
        daily_digest,
        args=[app],
        trigger="cron",
        hour=7,
        minute=0,
        timezone=KL_TZ,
        jitter=60,
        id="daily_digest",
        replace_existing=True,
    )
    logger.info("Scheduler jobs registered: %s", [j.id for j in scheduler.get_jobs()])

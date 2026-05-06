"""Scheduled jobs per spec §5.7.

Phase 4 wired ta_refresh_crypto + ta_refresh_equity_us. Phase 7 adds the
catalyst refresh trio and the 07:00 KL daily_digest. Phase 8 adds the
6-hourly health_heartbeat. The Bursa equity_my refresh waits on Phase 10.
"""

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.constants import ParseMode

from src.adapters import finnhub, sec_edgar, token_unlocks, trading_economics
from src.catalyst.agent import run_catalyst_agent
from src.config import DB_PATH, KL_TZ, OWNER_TELEGRAM_ID
from src.db import cache, catalysts, health
from src.db.watchlist import WatchlistEntry, is_muted, list_entries
from src.ta.pipeline import get_crypto_snapshot, get_equity_snapshot
from src.telegram.formatters import (
    SOURCE_TTL_HOURS,
    format_catalyst_output,
    format_edgar_alert,
    format_ta_snapshot,
)
from src.utils.timestamps import is_my_market_open, is_us_market_open, now_kl

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
    is_equity = entry.asset_class in ("equity_us", "equity_my", "equity_sg")
    source_label = "yfinance" if is_equity else f"ccxt:{entry.exchange}"
    try:
        if entry.asset_class == "crypto":
            assert entry.exchange is not None
            snap = await get_crypto_snapshot(
                DB_PATH, entry.ticker, entry.exchange, force_refresh=True
            )
        elif is_equity:
            snap = await get_equity_snapshot(
                DB_PATH, entry.ticker, entry.asset_class, force_refresh=True
            )
        else:
            return
    except Exception as exc:
        logger.exception("TA refresh failed for %s", entry.ticker)
        await health.record(
            DB_PATH,
            source=source_label,
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


async def refresh_equity_my_ta(app: "Application") -> None:
    """Hourly Bursa fetch, suppressed outside the 01:00-09:00 UTC window."""
    if not is_my_market_open():
        logger.info("ta_refresh_equity_my: Bursa closed, suppressing")
        return
    logger.info("ta_refresh_equity_my tick")
    entries = await list_entries(DB_PATH, OWNER_TELEGRAM_ID)
    for entry in entries:
        if entry.asset_class != "equity_my":
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
# Phase 8 — health heartbeat
# ---------------------------------------------------------------------------

# A source that hasn't logged anything within this window is considered silent
# and the heartbeat writes an "error" row so /health surfaces a ❌.
SILENCE_THRESHOLD_HOURS = 24


# ---------------------------------------------------------------------------
# Phase 9 — EDGAR RSS poller (instant 8-K push notifications)
# ---------------------------------------------------------------------------

EDGAR_SEEN_TTL = 90 * 24 * 60 * 60  # 90 days; we never want this entry to expire


async def _get_seen_accessions(db_path, ticker: str) -> set[str]:
    payload = await cache.read_if_fresh(
        db_path, f"edgar_seen:{ticker.upper()}", ttl_seconds=EDGAR_SEEN_TTL
    )
    if not payload:
        return set()
    return set(payload.get("accessions") or [])


async def _set_seen_accessions(db_path, ticker: str, accessions: set[str]) -> None:
    await cache.write(
        db_path,
        cache_key=f"edgar_seen:{ticker.upper()}",
        data_type="edgar_seen",
        payload={"accessions": sorted(accessions)},
        pulled_at=datetime.now(UTC),
        source_url="https://www.sec.gov",
        ticker=ticker.upper(),
    )


async def edgar_rss_poll(app: "Application") -> None:
    """Every 10 minutes — push a Telegram alert for new 8-Ks on watchlist equities.

    First run for a ticker initializes the watermark without firing alerts (so
    we don't spam the owner with a backlog of pre-existing filings).
    """
    logger.info("edgar_rss_poll tick")
    entries = await list_entries(DB_PATH, OWNER_TELEGRAM_ID)
    for entry in entries:
        if entry.asset_class != "equity_us" or is_muted(entry):
            continue
        try:
            result = await sec_edgar.read_sec_filings(
                DB_PATH, entry.ticker, form_types=["8-K"], days_back=2
            )
        except Exception:
            logger.exception("edgar_rss_poll fetch failed for %s", entry.ticker)
            continue
        items = result.get("items") or []
        if not items:
            continue

        seen = await _get_seen_accessions(DB_PATH, entry.ticker)
        first_run = not seen
        new_items = [it for it in items if it.get("accession") and it["accession"] not in seen]
        merged = seen | {it["accession"] for it in items if it.get("accession")}
        await _set_seen_accessions(DB_PATH, entry.ticker, merged)

        if first_run or not new_items:
            continue

        for item in new_items:
            try:
                await app.bot.send_message(
                    chat_id=OWNER_TELEGRAM_ID,
                    text=format_edgar_alert(item),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False,
                )
            except Exception as exc:
                logger.warning("edgar alert send failed for %s: %s", entry.ticker, exc)


# ---------------------------------------------------------------------------
# Phase 8 — health heartbeat
# ---------------------------------------------------------------------------


async def health_heartbeat(app: "Application") -> None:
    """Every 6h — flag any tracked source that hasn't checked in for >24h.

    Skips sources with a recent ok/error row; only writes when a source has
    been silent past the threshold or has never been seen at all.
    """
    logger.info("health_heartbeat tick")
    rows = await health.latest_per_source(DB_PATH)
    now = datetime.now(UTC)
    threshold_seconds = SILENCE_THRESHOLD_HOURS * 3600
    for source in SOURCE_TTL_HOURS:
        info = rows.get(source)
        if info is None:
            await health.record(DB_PATH, source=source, status="error", details="never seen")
            continue
        age = (now - info["recorded_at"]).total_seconds()
        if age > threshold_seconds:
            await health.record(
                DB_PATH,
                source=source,
                status="error",
                details=f"silent for {age / 3600:.1f}h",
            )


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
        refresh_equity_my_ta,
        args=[app],
        trigger="cron",
        minute=15,
        jitter=60,
        id="ta_refresh_equity_my",
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
    scheduler.add_job(
        health_heartbeat,
        args=[app],
        trigger="cron",
        hour="0,6,12,18",
        minute=30,
        timezone=KL_TZ,
        jitter=60,
        id="health_heartbeat",
        replace_existing=True,
    )
    scheduler.add_job(
        edgar_rss_poll,
        args=[app],
        trigger="cron",
        minute="*/10",
        jitter=60,
        id="edgar_rss_poll",
        replace_existing=True,
    )
    logger.info("Scheduler jobs registered: %s", [j.id for j in scheduler.get_jobs()])

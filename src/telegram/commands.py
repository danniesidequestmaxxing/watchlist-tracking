import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from src.adapters import bursa
from src.catalyst.agent import run_catalyst_agent
from src.config import DB_PATH, OWNER_TELEGRAM_ID, WATCHLIST_LIMIT
from src.db import catalysts, health
from src.db.watchlist import (
    WatchlistEntry,
    add_entry,
    count_entries,
    find_entry,
    is_muted,
    list_entries,
    remove_entry,
)
from src.ta.pipeline import get_crypto_snapshot, get_equity_snapshot
from src.telegram.auth import require_owner
from src.telegram.formatters import (
    format_bursa_catalyst,
    format_catalyst_output,
    format_health,
    format_ta_snapshot,
    format_watchlist,
)
from src.utils.asset_class import detect_asset_class

logger = logging.getLogger(__name__)


@require_owner
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    await msg.reply_text(
        "Bot online. Commands: /add /remove /list /snapshot /catalyst /digest /health."
    )


@require_owner
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    args = context.args or []
    if not args:
        await msg.reply_text("Usage: /add <ticker> [exchange]")
        return

    ticker = args[0].upper().strip()
    exchange = args[1].lower().strip() if len(args) > 1 else None

    asset_class = detect_asset_class(ticker)
    if asset_class is None:
        await msg.reply_text(
            f"Could not classify {ticker}. "
            f"Try an explicit form (e.g. {ticker}USDT for crypto, {ticker}.SI for SGX)."
        )
        return

    current = await count_entries(DB_PATH, OWNER_TELEGRAM_ID)
    if current >= WATCHLIST_LIMIT:
        await msg.reply_text(
            f"Watchlist is full ({current}/{WATCHLIST_LIMIT}). Remove an entry first."
        )
        return

    new_id = await add_entry(DB_PATH, OWNER_TELEGRAM_ID, ticker, asset_class, exchange)
    if new_id is None:
        suffix = f" on {exchange}" if exchange else ""
        await msg.reply_text(f"{ticker}{suffix} is already on the watchlist.")
        return

    suffix = f" ({exchange})" if exchange else ""
    await msg.reply_text(f"Added {ticker}{suffix} as {asset_class}.")


@require_owner
async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    args = context.args or []
    if not args:
        await msg.reply_text("Usage: /remove <ticker>")
        return

    ticker = args[0].upper().strip()
    rows = await remove_entry(DB_PATH, OWNER_TELEGRAM_ID, ticker)
    if rows == 0:
        await msg.reply_text(f"{ticker} not found on watchlist.")
    elif rows == 1:
        await msg.reply_text(f"Removed {ticker}.")
    else:
        await msg.reply_text(f"Removed {rows} entries for {ticker}.")


@require_owner
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    entries = await list_entries(DB_PATH, OWNER_TELEGRAM_ID)
    text = format_watchlist(entries)
    await msg.reply_text(text, parse_mode=ParseMode.HTML)


@require_owner
async def cmd_snapshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    args = context.args or []
    if not args:
        await msg.reply_text("Usage: /snapshot <ticker>")
        return

    ticker = args[0].upper().strip()
    entry = await find_entry(DB_PATH, OWNER_TELEGRAM_ID, ticker)
    if entry is None:
        await msg.reply_text(f"{ticker} is not on the watchlist. /add it first.")
        return

    try:
        if entry.asset_class == "crypto":
            if not entry.exchange:
                await msg.reply_text(
                    f"{ticker} has no exchange set. /remove and re-add with "
                    f"`/add {ticker} <exchange>`."
                )
                return
            snapshot = await get_crypto_snapshot(DB_PATH, ticker, entry.exchange)
        elif entry.asset_class in ("equity_us", "equity_my", "equity_sg"):
            snapshot = await get_equity_snapshot(DB_PATH, ticker, entry.asset_class)
        else:
            await msg.reply_text(f"/snapshot for {entry.asset_class} is not implemented yet.")
            return
    except Exception as exc:
        logger.exception("Snapshot fetch failed for %s (%s)", ticker, entry.asset_class)
        await msg.reply_text(f"Failed to fetch snapshot for {ticker}: {exc}")
        return

    await msg.reply_text(format_ta_snapshot(snapshot), parse_mode=ParseMode.HTML)


@require_owner
async def cmd_catalyst(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    args = context.args or []
    if not args:
        await msg.reply_text("Usage: /catalyst <ticker>")
        return

    ticker = args[0].upper().strip()
    entry = await find_entry(DB_PATH, OWNER_TELEGRAM_ID, ticker)
    if entry is None:
        await msg.reply_text(f"{ticker} is not on the watchlist. /add it first.")
        return

    if entry.asset_class == "equity_my":
        # Bursa isn't covered by the catalyst agent's prompt rules; fetch
        # announcements directly and render them in the catalyst layout.
        await msg.reply_text(f"Pulling Bursa announcements for {ticker}…")
        try:
            result = await bursa.fetch_announcements(DB_PATH, ticker, days_back=30)
        except Exception as exc:
            logger.exception("Bursa fetch failed for %s", ticker)
            await msg.reply_text(f"Bursa lookup failed for {ticker}: {exc}")
            return
        await msg.reply_text(
            format_bursa_catalyst(ticker, result.get("items", []), result.get("pulled_at")),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    await msg.reply_text(f"Pulling catalysts for {ticker}…")
    try:
        output = await run_catalyst_agent(ticker, entry.asset_class, DB_PATH)
    except Exception as exc:
        logger.exception("Catalyst agent failed for %s", ticker)
        await msg.reply_text(f"Catalyst lookup failed for {ticker}: {exc}")
        return

    try:
        saved = await catalysts.save_events(
            DB_PATH, ticker, output.confirmed, output.expected, output.speculative
        )
        if saved:
            logger.info("Saved %d catalyst events for %s", saved, ticker)
    except Exception:
        logger.exception("Failed to persist catalyst events for %s", ticker)

    await msg.reply_text(format_catalyst_output(output), parse_mode=ParseMode.HTML)


async def _ta_for(entry: WatchlistEntry) -> str | None:
    """Return formatted TA for one entry, or None if unsupported / failed."""
    try:
        if entry.asset_class == "crypto" and entry.exchange:
            snap = await get_crypto_snapshot(DB_PATH, entry.ticker, entry.exchange)
        elif entry.asset_class in ("equity_us", "equity_my", "equity_sg"):
            snap = await get_equity_snapshot(DB_PATH, entry.ticker, entry.asset_class)
        else:
            return None
    except Exception as exc:
        logger.warning("Digest TA fetch failed for %s: %s", entry.ticker, exc)
        return None
    return format_ta_snapshot(snap)


async def _catalyst_for(entry: WatchlistEntry) -> str | None:
    if entry.asset_class == "equity_my":
        try:
            result = await bursa.fetch_announcements(DB_PATH, entry.ticker, days_back=30)
        except Exception as exc:
            logger.warning("Digest Bursa fetch failed for %s: %s", entry.ticker, exc)
            return None
        return format_bursa_catalyst(entry.ticker, result.get("items", []), result.get("pulled_at"))

    try:
        out = await run_catalyst_agent(entry.ticker, entry.asset_class, DB_PATH)
    except Exception as exc:
        logger.warning("Digest catalyst fetch failed for %s: %s", entry.ticker, exc)
        return None
    try:
        await catalysts.save_events(
            DB_PATH, entry.ticker, out.confirmed, out.expected, out.speculative
        )
    except Exception:
        logger.exception("Failed to persist catalyst events for %s", entry.ticker)
    return format_catalyst_output(out)


@require_owner
async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Full TA + catalyst pull for the entire watchlist (spec §5.1)."""
    msg = update.effective_message
    if msg is None:
        return

    entries = await list_entries(DB_PATH, OWNER_TELEGRAM_ID)
    active = [e for e in entries if not is_muted(e)]
    if not entries:
        await msg.reply_text("Watchlist empty. Add entries with /add first.")
        return

    skipped = len(entries) - len(active)
    note = f" ({skipped} muted)" if skipped else ""
    await msg.reply_text(f"📊 Pulling digest for {len(active)} entries{note}…")

    for entry in active:
        ta = await _ta_for(entry)
        if ta:
            await msg.reply_text(ta, parse_mode=ParseMode.HTML)
        if entry.catalyst_enabled:
            catalyst = await _catalyst_for(entry)
            if catalyst:
                await msg.reply_text(catalyst, parse_mode=ParseMode.HTML)


@require_owner
async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Per-source freshness from health_log (spec §5.6)."""
    msg = update.effective_message
    if msg is None:
        return
    rows = await health.latest_per_source(DB_PATH)
    await msg.reply_text(format_health(rows), parse_mode=ParseMode.HTML)

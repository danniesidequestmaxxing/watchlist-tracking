import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from src.config import DB_PATH, OWNER_TELEGRAM_ID, WATCHLIST_LIMIT
from src.db.watchlist import add_entry, count_entries, find_entry, list_entries, remove_entry
from src.ta.pipeline import get_crypto_snapshot
from src.telegram.auth import require_owner
from src.telegram.formatters import format_ta_snapshot, format_watchlist
from src.utils.asset_class import detect_asset_class

logger = logging.getLogger(__name__)


@require_owner
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    await msg.reply_text("Bot online. Commands: /add /remove /list /snapshot.")


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

    if entry.asset_class != "crypto":
        await msg.reply_text(
            f"/snapshot for {entry.asset_class} is not implemented yet (Phase 4 will add equities)."
        )
        return

    if not entry.exchange:
        await msg.reply_text(
            f"{ticker} has no exchange set. /remove it and re-add with `/add {ticker} <exchange>`."
        )
        return

    try:
        snapshot = await get_crypto_snapshot(DB_PATH, ticker, entry.exchange)
    except Exception as exc:
        logger.exception("Snapshot fetch failed for %s on %s", ticker, entry.exchange)
        await msg.reply_text(f"Failed to fetch snapshot for {ticker}: {exc}")
        return

    await msg.reply_text(format_ta_snapshot(snapshot), parse_mode=ParseMode.HTML)

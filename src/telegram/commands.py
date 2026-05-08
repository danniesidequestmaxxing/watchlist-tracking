import logging
from datetime import UTC, datetime

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from src.adapters import bursa, cninfo, dart, edinet, mops
from src.catalyst.agent import run_catalyst_agent
from src.config import DB_PATH, OWNER_TELEGRAM_ID, WATCHLIST_LIMIT
from src.db import catalysts, forwards, health
from src.db.watchlist import (
    WatchlistEntry,
    add_entry,
    clear_mute,
    count_entries,
    find_entry,
    is_muted,
    list_entries,
    remove_entry,
    set_mute,
)
from src.ta.pipeline import get_crypto_snapshot, get_equity_snapshot
from src.telegram.auth import require_owner
from src.telegram.formatters import (
    format_bursa_catalyst,
    format_catalyst_output,
    format_disclosure_catalyst,
    format_health,
    format_ta_snapshot,
    format_watchlist,
)
from src.utils.asset_class import detect_asset_class
from src.utils.timestamps import fmt_duration, parse_duration

logger = logging.getLogger(__name__)


# Phase 13 — Asia disclosures (KR/JP/TW/CN) bypass the catalyst agent (whose
# prompt rules don't cover them) and go straight to the local primary source.
_ASIA_ADAPTERS: dict[str, dict] = {
    "equity_kr": {
        "fetch": dart.fetch_disclosures,
        "label": "DART",
        "domain": "opendart.fss.or.kr",
    },
    "equity_jp": {
        "fetch": edinet.fetch_disclosures,
        "label": "EDINET",
        "domain": "disclosure.edinet-fsa.go.jp",
    },
    "equity_tw": {
        "fetch": mops.fetch_disclosures,
        "label": "MOPS",
        "domain": "mops.twse.com.tw",
    },
    "equity_cn": {
        "fetch": cninfo.fetch_disclosures,
        "label": "CNINFO",
        "domain": "cninfo.com.cn",
    },
}


@require_owner
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    await msg.reply_text(
        "Bot online. Commands: /add /addmany /remove /list /mute /unmute "
        "/snapshot /catalyst /digest /health /grant /revoke /forwards."
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


def _parse_bulk_token(token: str) -> tuple[str, str | None]:
    """Split `TICKER` or `TICKER:exchange` into (ticker, exchange|None)."""
    parts = token.split(":", 1)
    ticker = parts[0].upper().strip()
    exchange = parts[1].lower().strip() if len(parts) == 2 and parts[1].strip() else None
    return ticker, exchange


@require_owner
async def cmd_addmany(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bulk-add multiple tickers in one command.

    Token format: `TICKER` or `TICKER:exchange`. Crypto must include the
    exchange (`BTCUSDT:binance`); equities don't take one (`NVDA`, `5347`).
    """
    msg = update.effective_message
    if msg is None:
        return
    args = context.args or []
    if not args:
        await msg.reply_text(
            "Usage: /addmany BTCUSDT:binance ETHUSDT:binance SOLUSDT:okx NVDA META 5347"
        )
        return

    current = await count_entries(DB_PATH, OWNER_TELEGRAM_ID)
    capacity = WATCHLIST_LIMIT - current

    added: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    capped: list[str] = []

    for raw in args:
        ticker, exchange = _parse_bulk_token(raw)
        if not ticker:
            failed.append(f"❌ <code>{raw}</code> empty ticker")
            continue
        asset_class = detect_asset_class(ticker)
        if asset_class is None:
            failed.append(f"❌ <code>{ticker}</code> couldn't classify")
            continue
        if len(added) >= capacity:
            capped.append(f"⏸ <code>{ticker}</code> watchlist full")
            continue
        new_id = await add_entry(DB_PATH, OWNER_TELEGRAM_ID, ticker, asset_class, exchange)
        if new_id is None:
            label = f"{ticker}" + (f" on {exchange}" if exchange else "")
            skipped.append(f"⏭ <code>{label}</code> already on watchlist")
            continue
        suffix = f" ({exchange})" if exchange else ""
        added.append(f"🟢 <code>{ticker}{suffix}</code> {asset_class}")

    summary = (
        f"<b>Bulk add</b>: {len(added)} added · {len(skipped)} skipped · "
        f"{len(failed)} failed · {len(capped)} capped "
        f"({current + len(added)}/{WATCHLIST_LIMIT} used)"
    )
    lines = [summary, ""]
    lines.extend(added)
    lines.extend(skipped)
    lines.extend(failed)
    lines.extend(capped)
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


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
        elif entry.asset_class in (
            "equity_us",
            "equity_my",
            "equity_sg",
            "equity_kr",
            "equity_jp",
            "equity_tw",
            "equity_cn",
        ):
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

    if entry.asset_class in _ASIA_ADAPTERS:
        adapter = _ASIA_ADAPTERS[entry.asset_class]
        await msg.reply_text(f"Pulling {adapter['label']} disclosures for {ticker}…")
        try:
            result = await adapter["fetch"](DB_PATH, ticker, days_back=30)
        except Exception as exc:
            logger.exception("%s fetch failed for %s", adapter["label"], ticker)
            await msg.reply_text(f"{adapter['label']} lookup failed for {ticker}: {exc}")
            return
        if result.get("error"):
            await msg.reply_text(
                f"{adapter['label']} returned no data for {ticker}: {result['error']}"
            )
            return
        await msg.reply_text(
            format_disclosure_catalyst(
                ticker,
                result.get("items", []),
                result.get("pulled_at"),
                source_label=adapter["label"],
                source_domain=adapter["domain"],
            ),
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


_EQUITY_ASSET_CLASSES = (
    "equity_us",
    "equity_my",
    "equity_sg",
    "equity_kr",
    "equity_jp",
    "equity_tw",
    "equity_cn",
)


async def _ta_for(entry: WatchlistEntry) -> str | None:
    """Return formatted TA for one entry, or None if unsupported / failed."""
    try:
        if entry.asset_class == "crypto" and entry.exchange:
            snap = await get_crypto_snapshot(DB_PATH, entry.ticker, entry.exchange)
        elif entry.asset_class in _EQUITY_ASSET_CLASSES:
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

    if entry.asset_class in _ASIA_ADAPTERS:
        adapter = _ASIA_ADAPTERS[entry.asset_class]
        try:
            result = await adapter["fetch"](DB_PATH, entry.ticker, days_back=30)
        except Exception as exc:
            logger.warning("Digest %s fetch failed for %s: %s", adapter["label"], entry.ticker, exc)
            return None
        return format_disclosure_catalyst(
            entry.ticker,
            result.get("items", []),
            result.get("pulled_at"),
            source_label=adapter["label"],
            source_domain=adapter["domain"],
        )

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


# ---------------------------------------------------------------------------
# Forward-target management (Phase 11 group fan-out)
# ---------------------------------------------------------------------------


@require_owner
async def cmd_grant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/grant <chat_id> [label]` — add a chat to the broadcast fan-out."""
    msg = update.effective_message
    if msg is None:
        return
    args = context.args or []
    if not args:
        await msg.reply_text(
            "Usage: /grant <chat_id> [label]\n"
            "Add the bot to the chat first; it will DM you the chat_id."
        )
        return
    try:
        chat_id = int(args[0])
    except ValueError:
        await msg.reply_text(f"Bad chat_id: {args[0]!r} (must be an integer).")
        return
    label = " ".join(args[1:]).strip() or None
    inserted = await forwards.add_target(DB_PATH, chat_id, label)
    if not inserted:
        await msg.reply_text(
            f"Chat <code>{chat_id}</code> is already a forward target.", parse_mode=ParseMode.HTML
        )
        return
    suffix = f" ({label})" if label else ""
    await msg.reply_text(
        f"Granted: chat <code>{chat_id}</code>{suffix} will now receive every push.",
        parse_mode=ParseMode.HTML,
    )


@require_owner
async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/revoke <chat_id>` — remove a chat from the broadcast fan-out."""
    msg = update.effective_message
    if msg is None:
        return
    args = context.args or []
    if not args:
        await msg.reply_text("Usage: /revoke <chat_id>")
        return
    try:
        chat_id = int(args[0])
    except ValueError:
        await msg.reply_text(f"Bad chat_id: {args[0]!r}.")
        return
    rows = await forwards.remove_target(DB_PATH, chat_id)
    if rows == 0:
        await msg.reply_text(
            f"Chat <code>{chat_id}</code> wasn't a forward target.", parse_mode=ParseMode.HTML
        )
    else:
        await msg.reply_text(
            f"Revoked: chat <code>{chat_id}</code> no longer receives pushes.",
            parse_mode=ParseMode.HTML,
        )


@require_owner
async def cmd_forwards(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List the current forward targets."""
    msg = update.effective_message
    if msg is None:
        return
    targets = await forwards.list_targets(DB_PATH)
    if not targets:
        await msg.reply_text(
            "📡 No additional forward targets — pushes go to owner DM only.\n"
            "Add the bot to a chat, then /grant &lt;chat_id&gt;.",
            parse_mode=ParseMode.HTML,
        )
        return
    lines = ["📡 <b>Forward targets</b> (in addition to owner DM)", ""]
    for t in targets:
        label = f" — {t['label']}" if t.get("label") else ""
        lines.append(f"• <code>{t['chat_id']}</code>{label}")
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# Update the StatusUpdate handler — fires when the bot is added to a new chat,
# so the owner gets the chat_id without having to look it up manually.


async def cmd_on_new_chat_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """When the bot itself is added to a chat, DM the owner with the chat_id."""
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    new_members = msg.new_chat_members or []
    bot_id = context.bot.id
    if not any(m.id == bot_id for m in new_members):
        return
    title = chat.title or chat.username or chat.full_name or "(unnamed chat)"
    try:
        await context.bot.send_message(
            chat_id=OWNER_TELEGRAM_ID,
            text=(
                f"🤖 Bot added to <b>{title}</b> (chat_id <code>{chat.id}</code>).\n"
                f"To start broadcasting here: /grant {chat.id} {title}"
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        logger.warning("Failed to notify owner of new chat membership: %s", exc)


# ---------------------------------------------------------------------------
# /mute and /unmute (spec §5.1)
# ---------------------------------------------------------------------------


@require_owner
async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/mute <ticker> <duration>` — silence pushes for a period.

    Duration uses Nm/Nh/Nd, e.g. `/mute BTCUSDT 24h`. The scheduler, EDGAR
    poller, and /digest all honor `muted_until`.
    """
    msg = update.effective_message
    if msg is None:
        return
    args = context.args or []
    if len(args) < 2:
        await msg.reply_text("Usage: /mute <ticker> <duration>\n  e.g. /mute BTCUSDT 24h")
        return

    ticker = args[0].upper().strip()
    duration_str = args[1].strip()

    delta = parse_duration(duration_str)
    if delta is None:
        await msg.reply_text(f"Bad duration: {duration_str!r}. Use Nm/Nh/Nd (e.g. 30m, 24h, 7d).")
        return

    until = datetime.now(UTC) + delta
    rows = await set_mute(DB_PATH, OWNER_TELEGRAM_ID, ticker, until)
    if rows == 0:
        await msg.reply_text(f"{ticker} is not on the watchlist. /add it first.")
        return
    await msg.reply_text(
        f"🔴 Muted <b>{ticker}</b> for {fmt_duration(delta)} "
        f"(until {until.strftime('%a %b %d %H:%M UTC')}).",
        parse_mode=ParseMode.HTML,
    )


@require_owner
async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/unmute <ticker>` — clear an active mute."""
    msg = update.effective_message
    if msg is None:
        return
    args = context.args or []
    if not args:
        await msg.reply_text("Usage: /unmute <ticker>")
        return

    ticker = args[0].upper().strip()
    rows = await clear_mute(DB_PATH, OWNER_TELEGRAM_ID, ticker)
    if rows == 0:
        await msg.reply_text(f"{ticker} is not on the watchlist.")
    else:
        await msg.reply_text(f"🟢 Unmuted <b>{ticker}</b>.", parse_mode=ParseMode.HTML)

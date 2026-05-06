"""Multi-recipient send for scheduled pushes (Phase 11 — group fan-out).

Auto-pushes go to OWNER_TELEGRAM_ID plus every chat in `forward_targets`.
Per-target failures are logged but never propagate — one bad recipient
must not block the rest of the broadcast.
"""

import logging
from typing import TYPE_CHECKING, Any

from telegram.constants import ParseMode

from src.config import DB_PATH, OWNER_TELEGRAM_ID
from src.db import forwards

if TYPE_CHECKING:
    from telegram import Bot

logger = logging.getLogger(__name__)


async def _recipient_chat_ids() -> list[int]:
    """Return the deduped list of chat IDs that should receive a push.

    A missing `forward_targets` table (e.g. an old DB that hasn't been
    re-initialized yet, or a test fixture) is treated as "no extra
    recipients" rather than as an error.
    """
    try:
        extras = await forwards.target_chat_ids(DB_PATH)
    except Exception as exc:
        logger.warning("forward_targets lookup failed; OWNER-only broadcast: %s", exc)
        extras = []
    seen: set[int] = {OWNER_TELEGRAM_ID}
    ordered: list[int] = [OWNER_TELEGRAM_ID]
    for cid in extras:
        if cid in seen:
            continue
        seen.add(cid)
        ordered.append(cid)
    return ordered


async def broadcast(
    bot: "Bot",
    text: str,
    *,
    parse_mode: str | None = ParseMode.HTML,
    **send_kwargs: Any,
) -> int:
    """Send `text` to OWNER + all forward targets. Returns successful sends."""
    sent = 0
    for chat_id in await _recipient_chat_ids():
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, **send_kwargs)
            sent += 1
        except Exception as exc:
            logger.warning("broadcast to chat %s failed: %s", chat_id, exc)
    return sent

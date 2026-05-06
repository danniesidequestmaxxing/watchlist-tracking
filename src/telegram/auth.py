import logging
from collections.abc import Awaitable, Callable
from functools import wraps

from telegram import Update
from telegram.ext import ContextTypes

from src.config import OWNER_TELEGRAM_ID

logger = logging.getLogger(__name__)

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


def require_owner(handler: Handler) -> Handler:
    """Decorator that drops messages from anyone other than OWNER_TELEGRAM_ID."""

    @wraps(handler)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or user.id != OWNER_TELEGRAM_ID:
            uid = user.id if user else "unknown"
            logger.warning("Rejecting non-owner Telegram user %s", uid)
            if update.effective_message is not None:
                await update.effective_message.reply_text("Not authorized.")
            return
        await handler(update, context)

    return wrapped

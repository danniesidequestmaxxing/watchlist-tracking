import logging

from telegram.ext import Application, CommandHandler

from src.config import DB_PATH, TELEGRAM_BOT_TOKEN
from src.db.schema import init_db
from src.telegram.commands import cmd_add, cmd_list, cmd_remove, cmd_snapshot, cmd_start

logger = logging.getLogger(__name__)


async def _post_init(application: Application) -> None:
    await init_db(DB_PATH)
    logger.info("Telegram bot ready")


def build_application() -> Application:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("snapshot", cmd_snapshot))
    return app

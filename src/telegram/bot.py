import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.ext import Application, CommandHandler

from src.config import DB_PATH, TELEGRAM_BOT_TOKEN
from src.db.schema import init_db
from src.scheduler.jobs import register_jobs
from src.telegram.commands import (
    cmd_add,
    cmd_catalyst,
    cmd_digest,
    cmd_health,
    cmd_list,
    cmd_remove,
    cmd_snapshot,
    cmd_start,
)

logger = logging.getLogger(__name__)


_scheduler: AsyncIOScheduler | None = None


async def _post_init(application: Application) -> None:
    await init_db(DB_PATH)

    global _scheduler
    _scheduler = AsyncIOScheduler()
    register_jobs(_scheduler, application)
    _scheduler.start()
    logger.info("Telegram bot + scheduler ready")


async def _post_shutdown(application: Application) -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler stopped")


def build_application() -> Application:
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("snapshot", cmd_snapshot))
    app.add_handler(CommandHandler("catalyst", cmd_catalyst))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("health", cmd_health))
    return app

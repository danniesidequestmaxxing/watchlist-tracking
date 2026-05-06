import logging

from src.config import configure_logging
from src.telegram.bot import build_application

logger = logging.getLogger(__name__)


def main() -> None:
    configure_logging()
    logger.info("Booting trading-bot")
    app = build_application()
    app.run_polling()


if __name__ == "__main__":
    main()

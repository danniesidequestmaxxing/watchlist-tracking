import logging
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    pass


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ConfigError(f"Required environment variable {name} is missing")
    return value


TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")
OWNER_TELEGRAM_ID: int = int(_require("OWNER_TELEGRAM_ID"))
ANTHROPIC_API_KEY: str = _require("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

TOKEN_UNLOCKS_API_KEY: str | None = os.getenv("TOKEN_UNLOCKS_API_KEY")
COINMARKETCAL_API_KEY: str | None = os.getenv("COINMARKETCAL_API_KEY")
FINNHUB_API_KEY: str | None = os.getenv("FINNHUB_API_KEY")
TRADING_ECONOMICS_API_KEY: str | None = os.getenv("TRADING_ECONOMICS_API_KEY")
TWELVE_DATA_API_KEY: str | None = os.getenv("TWELVE_DATA_API_KEY")
POLYGON_API_KEY: str | None = os.getenv("POLYGON_API_KEY")

_DATA_SOURCE_KEYS = (
    TOKEN_UNLOCKS_API_KEY,
    COINMARKETCAL_API_KEY,
    FINNHUB_API_KEY,
    TRADING_ECONOMICS_API_KEY,
    TWELVE_DATA_API_KEY,
    POLYGON_API_KEY,
)
if not any(_DATA_SOURCE_KEYS):
    raise ConfigError(
        "At least one data source API key must be set "
        "(TOKEN_UNLOCKS_API_KEY, COINMARKETCAL_API_KEY, FINNHUB_API_KEY, "
        "TRADING_ECONOMICS_API_KEY, TWELVE_DATA_API_KEY, or POLYGON_API_KEY)."
    )

SEC_EDGAR_UA: str = os.getenv("SEC_EDGAR_UA", "trading-bot contact@example.com")

TIMEZONE: str = os.getenv("TIMEZONE", "Asia/Kuala_Lumpur")
KL_TZ = ZoneInfo(TIMEZONE)
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
DB_PATH: Path = Path(os.getenv("DB_PATH", "data/bot.db"))

WATCHLIST_LIMIT: int = 25

# Phase 9 — TradingView webhook ingress. Optional; if WEBHOOK_TOKEN is unset,
# the webhook server is not started.
WEBHOOK_TOKEN: str | None = os.getenv("WEBHOOK_TOKEN")
WEBHOOK_PORT: int = int(os.getenv("WEBHOOK_PORT") or os.getenv("PORT") or "8080")


def configure_logging() -> None:
    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

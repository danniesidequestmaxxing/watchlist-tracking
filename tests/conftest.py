"""Pytest setup — populate fake env vars before any `src.*` import happens.

`src.config` validates required env vars at module import per spec §8, so
tests need them in place before the first `from src...` line resolves.
Pytest loads conftest.py before any test file is collected.
"""

import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-bot-token")
os.environ.setdefault("OWNER_TELEGRAM_ID", "12345")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
os.environ.setdefault("FINNHUB_API_KEY", "test-finnhub-key")
os.environ.setdefault("DB_PATH", "data/test.db")

import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  ticker TEXT NOT NULL,
  asset_class TEXT NOT NULL CHECK(asset_class IN
    ('crypto','equity_us','equity_my','equity_sg','equity_kr','equity_jp',
     'equity_tw','equity_cn','fx','commodity')),
  exchange TEXT,
  notes TEXT,
  added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  ta_enabled BOOLEAN DEFAULT 1,
  catalyst_enabled BOOLEAN DEFAULT 1,
  alert_thresholds TEXT,
  muted_until TIMESTAMP,
  UNIQUE(user_id, ticker, exchange)
);

CREATE TABLE IF NOT EXISTS last_fetched (
  cache_key TEXT PRIMARY KEY,
  data_type TEXT NOT NULL,
  ticker TEXT,
  pulled_at TIMESTAMP NOT NULL,
  payload TEXT NOT NULL,
  source_url TEXT,
  source_published_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_cache_lookup ON last_fetched(data_type, ticker);

CREATE TABLE IF NOT EXISTS catalyst_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticker TEXT NOT NULL,
  event_type TEXT NOT NULL,
  event_date DATE NOT NULL,
  confidence TEXT NOT NULL CHECK(confidence IN ('confirmed','expected','speculative')),
  description TEXT,
  source_url TEXT NOT NULL,
  source_pulled_at TIMESTAMP NOT NULL,
  ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_catalyst_lookup ON catalyst_events(ticker, event_date);
CREATE UNIQUE INDEX IF NOT EXISTS idx_catalyst_unique
  ON catalyst_events(ticker, event_type, event_date, confidence);

CREATE TABLE IF NOT EXISTS health_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('ok','error','rate_limited')),
  details TEXT,
  recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_health_recent ON health_log(source, recorded_at);

CREATE TABLE IF NOT EXISTS forward_targets (
  chat_id INTEGER PRIMARY KEY,
  label TEXT,
  added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


async def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(SCHEMA)
        await db.commit()
    logger.info("Database initialized at %s", db_path)

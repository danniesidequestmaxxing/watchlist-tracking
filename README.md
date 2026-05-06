# trading-bot

Personal Telegram bot that tracks a watchlist of crypto and equity tickers,
sends hourly technical-analysis updates, and produces a daily catalyst digest
grounded in primary data sources. Single user, deployed on Railway.

## Status

Phase 1 — watchlist scaffold. `/add`, `/remove`, `/list` working against a
local SQLite database. Owner-only via `OWNER_TELEGRAM_ID`.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # fill in TELEGRAM_BOT_TOKEN, OWNER_TELEGRAM_ID, ANTHROPIC_API_KEY, and at least one data source key
python -m src.main
```

## Tests

```bash
pytest
```

## Layout

```
src/
  config.py             environment + constants
  main.py               entrypoint
  db/                   sqlite schema + watchlist CRUD
  utils/                ticker -> asset class detection
  telegram/             bot + commands + auth
tests/                  pytest suite
data/                   sqlite db lives here at runtime (gitignored)
```

## Deployment

Railway worker. The `Procfile` runs `python -m src.main`. Set environment
variables in the Railway dashboard.

## Known limits

- Asset class detection is pattern-based only; it does not call yfinance to
  confirm US equities yet (Phase 4 will tighten this).
- Bursa Malaysia adapter not implemented (Phase 10).

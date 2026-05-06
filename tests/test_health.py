"""Phase 8 — /health output, latest_per_source query, and heartbeat behavior."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.db import health
from src.db.schema import init_db
from src.scheduler import jobs as jobs_mod
from src.telegram.formatters import SOURCE_TTL_HOURS, format_health


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)


class _FakeApp:
    def __init__(self) -> None:
        self.bot = _FakeBot()


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "health_test.db"
    await init_db(p)
    return p


# ---------------------------------------------------------------------------
# health.latest_per_source
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_latest_per_source_returns_most_recent_row(db_path: Path) -> None:
    await health.record(db_path, "ccxt:binance", "ok", "first")
    await health.record(db_path, "ccxt:binance", "error", "rate limit")
    await health.record(db_path, "yfinance", "ok", "ok")
    rows = await health.latest_per_source(db_path)
    assert rows["ccxt:binance"]["status"] == "error"
    assert rows["ccxt:binance"]["details"] == "rate limit"
    assert rows["yfinance"]["status"] == "ok"


@pytest.mark.asyncio
async def test_latest_per_source_empty(db_path: Path) -> None:
    assert await health.latest_per_source(db_path) == {}


@pytest.mark.asyncio
async def test_record_swallows_db_failures(tmp_path: Path) -> None:
    """A bad path must not crash the bot."""
    bogus = tmp_path / "no_such_dir" / "x.db"
    # Should log a warning internally but not raise.
    await health.record(bogus, "ccxt:binance", "ok", "x")


# ---------------------------------------------------------------------------
# format_health
# ---------------------------------------------------------------------------


def test_format_health_renders_known_sources_in_order() -> None:
    now = datetime.now(UTC)
    per_source = {
        "ccxt:binance": {"status": "ok", "details": "BTCUSDT", "recorded_at": now},
        "yfinance": {"status": "ok", "details": "NVDA", "recorded_at": now - timedelta(minutes=4)},
        "trading-economics": {
            "status": "error",
            "details": "429",
            "recorded_at": now - timedelta(hours=31),
        },
    }
    text = format_health(per_source)
    # Header
    assert "<b>Source health</b>" in text
    # Known-source ordering: ccxt:binance before yfinance before trading-economics
    bin_idx = text.find("ccxt:binance")
    yf_idx = text.find("yfinance")
    te_idx = text.find("trading-economics")
    assert 0 < bin_idx < yf_idx < te_idx


def test_format_health_status_icons() -> None:
    now = datetime.now(UTC)
    per_source = {
        "ccxt:binance": {"status": "ok", "details": "x", "recorded_at": now},
        "yfinance": {  # ok but stale: ttl is 4h, age 5h
            "status": "ok",
            "details": "x",
            "recorded_at": now - timedelta(hours=5),
        },
        "trading-economics": {
            "status": "error",
            "details": "429 rate limited",
            "recorded_at": now - timedelta(hours=31),
        },
        "finnhub": {
            "status": "rate_limited",
            "details": "429",
            "recorded_at": now - timedelta(minutes=10),
        },
    }
    text = format_health(per_source)
    # Find each line and check icon
    lines = text.split("\n")

    def line_for(src: str) -> str:
        return next(line for line in lines if src in line)

    assert "✅" in line_for("ccxt:binance")
    assert "⚠" in line_for("yfinance")
    assert "stale" in line_for("yfinance")
    assert "❌" in line_for("trading-economics")
    assert "429" in line_for("trading-economics")
    assert "⚠" in line_for("finnhub")


def test_format_health_no_data_for_unseen_sources() -> None:
    text = format_health({})
    for source in SOURCE_TTL_HOURS:
        line = next(line for line in text.split("\n") if source in line)
        assert "❌" in line
        assert "no data" in line


def test_format_health_unknown_extra_source_appended() -> None:
    now = datetime.now(UTC)
    text = format_health(
        {"some-future-adapter": {"status": "ok", "details": "x", "recorded_at": now}}
    )
    # Known sources still appear first
    known_index = text.find("ccxt:binance")
    extra_index = text.find("some-future-adapter")
    assert 0 < known_index < extra_index


# ---------------------------------------------------------------------------
# health_heartbeat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_writes_never_seen_for_missing_sources(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs_mod, "DB_PATH", db_path)
    await jobs_mod.health_heartbeat(_FakeApp())
    rows = await health.latest_per_source(db_path)
    # Every source in SOURCE_TTL_HOURS must now have a "never seen" row.
    for source in SOURCE_TTL_HOURS:
        assert source in rows
        assert rows[source]["status"] == "error"
        assert "never seen" in (rows[source]["details"] or "")


@pytest.mark.asyncio
async def test_heartbeat_does_not_overwrite_recent_ok(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs_mod, "DB_PATH", db_path)
    await health.record(db_path, "ccxt:binance", "ok", "BTCUSDT")
    await jobs_mod.health_heartbeat(_FakeApp())
    rows = await health.latest_per_source(db_path)
    # Recent ok stays the latest
    assert rows["ccxt:binance"]["status"] == "ok"


@pytest.mark.asyncio
async def test_heartbeat_flags_old_silent_source(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the latest row is past the silence threshold, write a fresh error row."""
    monkeypatch.setattr(jobs_mod, "DB_PATH", db_path)

    # Backdate a row > 24h old by inserting directly with a specific recorded_at
    import aiosqlite

    old_ts = (datetime.now(UTC) - timedelta(hours=30)).isoformat()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO health_log (source, status, details, recorded_at) VALUES (?, ?, ?, ?)",
            ("ccxt:binance", "ok", "old run", old_ts),
        )
        await db.commit()

    await jobs_mod.health_heartbeat(_FakeApp())

    rows = await health.latest_per_source(db_path)
    binance = rows["ccxt:binance"]
    assert binance["status"] == "error"
    assert "silent for" in (binance["details"] or "")


# ---------------------------------------------------------------------------
# register_jobs includes heartbeat
# ---------------------------------------------------------------------------


def test_register_jobs_includes_health_heartbeat() -> None:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    sched = AsyncIOScheduler()
    jobs_mod.register_jobs(sched, _FakeApp())
    job = sched.get_job("health_heartbeat")
    assert job is not None
    assert str(job.trigger.timezone) == "Asia/Kuala_Lumpur"
    # 4 firings per day means the cron has hours 0,6,12,18
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields.get("hour") == "0,6,12,18"

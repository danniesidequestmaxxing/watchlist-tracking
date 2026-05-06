"""Phase 7 — verify register_jobs schedules everything from spec §5.7
and that `is_muted` correctly blocks scheduled work for muted entries.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.db.schema import init_db
from src.db.watchlist import WatchlistEntry, is_muted
from src.scheduler import jobs as jobs_mod


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
    p = tmp_path / "scheduler_test.db"
    await init_db(p)
    return p


# ---------------------------------------------------------------------------
# is_muted
# ---------------------------------------------------------------------------


def _entry(**overrides) -> WatchlistEntry:
    base = {
        "id": 1,
        "user_id": 1,
        "ticker": "BTCUSDT",
        "asset_class": "crypto",
        "exchange": "binance",
        "added_at": "2026-01-01 00:00:00",
        "ta_enabled": True,
        "catalyst_enabled": True,
    }
    base.update(overrides)
    return WatchlistEntry.model_validate(base)


def test_is_muted_none() -> None:
    assert is_muted(_entry(muted_until=None)) is False


def test_is_muted_in_future() -> None:
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert is_muted(_entry(muted_until=future)) is True


def test_is_muted_in_past() -> None:
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    assert is_muted(_entry(muted_until=past)) is False


def test_is_muted_invalid_string() -> None:
    assert is_muted(_entry(muted_until="not a date")) is False


# ---------------------------------------------------------------------------
# register_jobs schedules every required Phase 7 job
# ---------------------------------------------------------------------------


def test_register_jobs_creates_all_phase_7_ids() -> None:
    sched = AsyncIOScheduler()
    register = jobs_mod.register_jobs
    register(sched, _FakeApp())
    job_ids = {j.id for j in sched.get_jobs()}
    assert {
        "ta_refresh_crypto",
        "ta_refresh_equity_us",
        "catalyst_refresh_unlocks",
        "catalyst_refresh_macro",
        "catalyst_refresh_earnings",
        "daily_digest",
    } <= job_ids


def test_register_jobs_assigns_kl_timezone_to_kl_jobs() -> None:
    sched = AsyncIOScheduler()
    jobs_mod.register_jobs(sched, _FakeApp())
    for job_id in (
        "daily_digest",
        "catalyst_refresh_unlocks",
        "catalyst_refresh_earnings",
        "catalyst_refresh_macro",
    ):
        job = sched.get_job(job_id)
        assert job is not None
        assert str(job.trigger.timezone) == "Asia/Kuala_Lumpur"


def test_register_jobs_jitter_on_every_job() -> None:
    sched = AsyncIOScheduler()
    jobs_mod.register_jobs(sched, _FakeApp())
    for job in sched.get_jobs():
        assert job.trigger.jitter is not None and job.trigger.jitter > 0


# ---------------------------------------------------------------------------
# daily_digest flow: catalyst per entry, skips muted/disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daily_digest_runs_catalyst_for_each_active_entry(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.db.watchlist import add_entry

    await add_entry(db_path, 1, "BTCUSDT", "crypto", "binance")
    await add_entry(db_path, 1, "NVDA", "equity_us")

    monkeypatch.setattr(jobs_mod, "DB_PATH", db_path)
    monkeypatch.setattr(jobs_mod, "OWNER_TELEGRAM_ID", 1)

    fetched: list[str] = []

    async def fake_run(ticker, asset_class, db_path_arg, **_):
        fetched.append(ticker)
        from src.catalyst.agent import CatalystOutput

        return CatalystOutput(
            ticker=ticker,
            as_of=datetime.now(UTC),
            no_known_catalysts=True,
        )

    monkeypatch.setattr(jobs_mod, "run_catalyst_agent", fake_run)

    app = _FakeApp()
    await jobs_mod.daily_digest(app)

    assert sorted(fetched) == ["BTCUSDT", "NVDA"]
    # Header + one message per ticker
    assert len(app.bot.sent) == 1 + 2


@pytest.mark.asyncio
async def test_daily_digest_skips_muted_and_disabled(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aiosqlite

    from src.db.watchlist import add_entry

    future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    await add_entry(db_path, 1, "BTCUSDT", "crypto", "binance")
    await add_entry(db_path, 1, "ETHUSDT", "crypto", "binance")
    await add_entry(db_path, 1, "NVDA", "equity_us")

    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE watchlist SET muted_until = ? WHERE ticker = 'BTCUSDT'", (future,))
        await db.execute("UPDATE watchlist SET catalyst_enabled = 0 WHERE ticker = 'NVDA'")
        await db.commit()

    monkeypatch.setattr(jobs_mod, "DB_PATH", db_path)
    monkeypatch.setattr(jobs_mod, "OWNER_TELEGRAM_ID", 1)

    fetched: list[str] = []

    async def fake_run(ticker, asset_class, db_path_arg, **_):
        fetched.append(ticker)
        from src.catalyst.agent import CatalystOutput

        return CatalystOutput(ticker=ticker, as_of=datetime.now(UTC), no_known_catalysts=True)

    monkeypatch.setattr(jobs_mod, "run_catalyst_agent", fake_run)

    app = _FakeApp()
    await jobs_mod.daily_digest(app)

    assert fetched == ["ETHUSDT"]


@pytest.mark.asyncio
async def test_daily_digest_no_entries_no_messages(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs_mod, "DB_PATH", db_path)
    monkeypatch.setattr(jobs_mod, "OWNER_TELEGRAM_ID", 1)

    fake_run = AsyncMock()
    monkeypatch.setattr(jobs_mod, "run_catalyst_agent", fake_run)

    app = _FakeApp()
    await jobs_mod.daily_digest(app)

    assert app.bot.sent == []
    fake_run.assert_not_called()


# ---------------------------------------------------------------------------
# Cache-warming refresh jobs only call adapters for the relevant asset class
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_catalyst_refresh_unlocks_only_crypto(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.db.watchlist import add_entry

    await add_entry(db_path, 1, "SOLUSDT", "crypto", "binance")
    await add_entry(db_path, 1, "NVDA", "equity_us")

    monkeypatch.setattr(jobs_mod, "DB_PATH", db_path)
    monkeypatch.setattr(jobs_mod, "OWNER_TELEGRAM_ID", 1)

    seen: list[str] = []

    async def fake_unlocks(_db_path, ticker, **_):
        seen.append(ticker)
        return {"items": [], "pulled_at": "", "source_url": ""}

    monkeypatch.setattr(jobs_mod.token_unlocks, "get_token_unlocks", fake_unlocks)

    await jobs_mod.catalyst_refresh_unlocks(_FakeApp())
    assert seen == ["SOLUSDT"]


@pytest.mark.asyncio
async def test_catalyst_refresh_earnings_only_equity_us(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.db.watchlist import add_entry

    await add_entry(db_path, 1, "SOLUSDT", "crypto", "binance")
    await add_entry(db_path, 1, "NVDA", "equity_us")
    await add_entry(db_path, 1, "META", "equity_us")

    monkeypatch.setattr(jobs_mod, "DB_PATH", db_path)
    monkeypatch.setattr(jobs_mod, "OWNER_TELEGRAM_ID", 1)

    seen: list[str] = []

    async def fake_earnings(_db_path, ticker, **_):
        seen.append(ticker)
        return {"items": [], "pulled_at": "", "source_url": ""}

    monkeypatch.setattr(jobs_mod.finnhub, "get_earnings_calendar", fake_earnings)

    await jobs_mod.catalyst_refresh_earnings(_FakeApp())
    assert sorted(seen) == ["META", "NVDA"]


@pytest.mark.asyncio
async def test_catalyst_refresh_macro_one_call(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Macro is global, not per-ticker — one call per tick."""
    monkeypatch.setattr(jobs_mod, "DB_PATH", db_path)

    calls = 0

    async def fake_macro(_db_path, **_):
        nonlocal calls
        calls += 1
        return {"items": [], "pulled_at": "", "source_url": ""}

    monkeypatch.setattr(jobs_mod.trading_economics, "get_macro_events", fake_macro)

    await jobs_mod.catalyst_refresh_macro(_FakeApp())
    assert calls == 1


# ---------------------------------------------------------------------------
# Adapter exception in a refresh job is swallowed (job should not crash)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_job_swallows_adapter_exceptions(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.db.watchlist import add_entry

    await add_entry(db_path, 1, "SOLUSDT", "crypto", "binance")
    monkeypatch.setattr(jobs_mod, "DB_PATH", db_path)
    monkeypatch.setattr(jobs_mod, "OWNER_TELEGRAM_ID", 1)

    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(jobs_mod.token_unlocks, "get_token_unlocks", boom)

    # Must not raise
    await jobs_mod.catalyst_refresh_unlocks(_FakeApp())


# ---------------------------------------------------------------------------
# Sanity: scheduler can start and stop with these jobs in place
# ---------------------------------------------------------------------------


def test_scheduler_lifecycle_with_jobs() -> None:
    sched = AsyncIOScheduler()
    jobs_mod.register_jobs(sched, _FakeApp())

    async def lifecycle():
        sched.start()
        # Each job has a computed next_run_time
        for job in sched.get_jobs():
            assert job.next_run_time is not None
        sched.shutdown(wait=False)

    asyncio.run(lifecycle())

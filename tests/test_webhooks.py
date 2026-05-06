"""Phase 9 — TradingView webhook ingress + EDGAR push poller."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from src.db.schema import init_db
from src.scheduler import jobs as jobs_mod
from src.telegram.formatters import format_edgar_alert, format_tradingview_alert
from src.webhooks import server as webhook_server


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._raise = False

    async def send_message(self, **kwargs):
        if self._raise:
            raise RuntimeError("simulated telegram failure")
        self.sent.append(kwargs)


class _FakeApp:
    def __init__(self) -> None:
        self.bot = _FakeBot()


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "webhook_test.db"
    await init_db(p)
    return p


# ---------------------------------------------------------------------------
# format_tradingview_alert
# ---------------------------------------------------------------------------


def test_format_tradingview_full_payload() -> None:
    text = format_tradingview_alert(
        {
            "ticker": "BTCUSDT",
            "exchange": "BINANCE",
            "alert": "RSI overbought",
            "price": 67420.5,
            "message": "BTCUSDT crossed RSI 70",
        }
    )
    assert "BTCUSDT" in text
    assert "BINANCE" in text
    assert "RSI overbought" in text
    assert "67,420" in text or "67420" in text  # price formatter
    assert "RSI 70" in text


def test_format_tradingview_minimal_payload() -> None:
    text = format_tradingview_alert({"symbol": "AAPL"})
    assert "AAPL" in text
    assert "TradingView" in text


def test_format_tradingview_string_payload() -> None:
    text = format_tradingview_alert("just a raw alert text")
    assert "just a raw alert text" in text


def test_format_tradingview_none_payload() -> None:
    text = format_tradingview_alert(None)
    assert "TradingView" in text
    assert "empty" in text


def test_format_tradingview_non_numeric_price() -> None:
    text = format_tradingview_alert({"ticker": "X", "price": "n/a"})
    assert "n/a" in text


# ---------------------------------------------------------------------------
# format_edgar_alert
# ---------------------------------------------------------------------------


def test_format_edgar_alert_basic() -> None:
    text = format_edgar_alert(
        {
            "ticker": "NVDA",
            "form": "8-K",
            "filing_date": "2026-05-06",
            "source_url": "https://www.sec.gov/Archives/edgar/data/1045810/x.htm",
        }
    )
    assert "NVDA" in text
    assert "8-K" in text
    assert "2026-05-06" in text
    assert "View filing" in text


# ---------------------------------------------------------------------------
# aiohttp webhook server
# ---------------------------------------------------------------------------


@pytest.fixture
async def http_client():
    bot = _FakeBot()
    app = webhook_server.build_app(bot, token="test-token")
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client, bot
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_webhook_accepts_valid_token_and_dispatches(http_client) -> None:
    client, bot = http_client
    resp = await client.post(
        "/webhooks/tradingview/test-token",
        json={"ticker": "BTCUSDT", "alert": "RSI", "price": 67000},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["delivered"] >= 1
    assert len(bot.sent) == 1
    assert "BTCUSDT" in bot.sent[0]["text"]


@pytest.mark.asyncio
async def test_webhook_rejects_bad_token(http_client) -> None:
    client, bot = http_client
    resp = await client.post(
        "/webhooks/tradingview/wrong",
        json={"ticker": "X"},
    )
    assert resp.status == 403
    assert bot.sent == []


@pytest.mark.asyncio
async def test_webhook_handles_invalid_json(http_client) -> None:
    """Non-JSON body still gets dispatched as a raw text alert."""
    client, bot = http_client
    resp = await client.post(
        "/webhooks/tradingview/test-token",
        data="raw alert string",
        headers={"Content-Type": "text/plain"},
    )
    assert resp.status == 200
    assert len(bot.sent) == 1
    assert "raw alert string" in bot.sent[0]["text"]


@pytest.mark.asyncio
async def test_webhook_returns_502_on_telegram_failure(http_client) -> None:
    client, bot = http_client
    bot._raise = True
    resp = await client.post(
        "/webhooks/tradingview/test-token",
        json={"ticker": "X"},
    )
    assert resp.status == 502


@pytest.mark.asyncio
async def test_webhook_healthz(http_client) -> None:
    client, _ = http_client
    resp = await client.get("/healthz")
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True


@pytest.mark.asyncio
async def test_webhook_start_refuses_without_token() -> None:
    bot = _FakeBot()
    with pytest.raises(RuntimeError):
        await webhook_server.start(bot, port=0, token=None)


# ---------------------------------------------------------------------------
# edgar_rss_poll
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edgar_poll_first_run_initializes_watermark_silently(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first time we poll a ticker, we record what we see but don't send
    alerts — otherwise the user gets spammed by the historical backlog."""
    from src.db.watchlist import add_entry

    await add_entry(db_path, 1, "NVDA", "equity_us")

    monkeypatch.setattr(jobs_mod, "DB_PATH", db_path)
    monkeypatch.setattr(jobs_mod, "OWNER_TELEGRAM_ID", 1)

    async def fake_filings(*args, **kwargs):
        return {
            "items": [
                {
                    "ticker": "NVDA",
                    "form": "8-K",
                    "filing_date": "2026-05-04",
                    "accession": "A1",
                    "source_url": "https://x",
                },
                {
                    "ticker": "NVDA",
                    "form": "8-K",
                    "filing_date": "2026-05-05",
                    "accession": "A2",
                    "source_url": "https://y",
                },
            ],
            "pulled_at": datetime.now(UTC).isoformat(),
            "source_url": "https://www.sec.gov",
        }

    monkeypatch.setattr(jobs_mod.sec_edgar, "read_sec_filings", fake_filings)

    app = _FakeApp()
    await jobs_mod.edgar_rss_poll(app)
    assert app.bot.sent == []  # no alerts on first run

    seen = await jobs_mod._get_seen_accessions(db_path, "NVDA")
    assert seen == {"A1", "A2"}


@pytest.mark.asyncio
async def test_edgar_poll_alerts_only_on_new_filings(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.db.watchlist import add_entry

    await add_entry(db_path, 1, "NVDA", "equity_us")

    monkeypatch.setattr(jobs_mod, "DB_PATH", db_path)
    monkeypatch.setattr(jobs_mod, "OWNER_TELEGRAM_ID", 1)

    # Pre-seed watermark with an existing accession
    await jobs_mod._set_seen_accessions(db_path, "NVDA", {"A1"})

    async def fake_filings(*args, **kwargs):
        return {
            "items": [
                {
                    "ticker": "NVDA",
                    "form": "8-K",
                    "filing_date": "2026-05-04",
                    "accession": "A1",
                    "source_url": "https://x",
                },
                {
                    "ticker": "NVDA",
                    "form": "8-K",
                    "filing_date": "2026-05-06",
                    "accession": "A2",
                    "source_url": "https://y",
                },
            ],
            "pulled_at": datetime.now(UTC).isoformat(),
            "source_url": "https://www.sec.gov",
        }

    monkeypatch.setattr(jobs_mod.sec_edgar, "read_sec_filings", fake_filings)

    app = _FakeApp()
    await jobs_mod.edgar_rss_poll(app)

    # One alert for the new A2; A1 already in watermark
    assert len(app.bot.sent) == 1
    assert "8-K" in app.bot.sent[0]["text"]
    seen = await jobs_mod._get_seen_accessions(db_path, "NVDA")
    assert seen == {"A1", "A2"}


@pytest.mark.asyncio
async def test_edgar_poll_skips_non_equity_us(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.db.watchlist import add_entry

    await add_entry(db_path, 1, "BTCUSDT", "crypto", "binance")

    monkeypatch.setattr(jobs_mod, "DB_PATH", db_path)
    monkeypatch.setattr(jobs_mod, "OWNER_TELEGRAM_ID", 1)

    called = []

    async def fake_filings(*args, **kwargs):
        called.append(args)
        return {"items": [], "pulled_at": "", "source_url": ""}

    monkeypatch.setattr(jobs_mod.sec_edgar, "read_sec_filings", fake_filings)

    app = _FakeApp()
    await jobs_mod.edgar_rss_poll(app)
    assert called == []


@pytest.mark.asyncio
async def test_edgar_poll_skips_muted(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import aiosqlite

    from src.db.watchlist import add_entry

    future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    await add_entry(db_path, 1, "NVDA", "equity_us")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE watchlist SET muted_until = ? WHERE ticker = 'NVDA'", (future,))
        await db.commit()

    monkeypatch.setattr(jobs_mod, "DB_PATH", db_path)
    monkeypatch.setattr(jobs_mod, "OWNER_TELEGRAM_ID", 1)

    called = []

    async def fake_filings(*args, **kwargs):
        called.append(args)
        return {"items": [], "pulled_at": "", "source_url": ""}

    monkeypatch.setattr(jobs_mod.sec_edgar, "read_sec_filings", fake_filings)

    await jobs_mod.edgar_rss_poll(_FakeApp())
    assert called == []


@pytest.mark.asyncio
async def test_edgar_poll_swallows_adapter_errors(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.db.watchlist import add_entry

    await add_entry(db_path, 1, "NVDA", "equity_us")
    monkeypatch.setattr(jobs_mod, "DB_PATH", db_path)
    monkeypatch.setattr(jobs_mod, "OWNER_TELEGRAM_ID", 1)

    async def boom(*args, **kwargs):
        raise RuntimeError("network error")

    monkeypatch.setattr(jobs_mod.sec_edgar, "read_sec_filings", boom)

    # Must not raise
    await jobs_mod.edgar_rss_poll(_FakeApp())


# ---------------------------------------------------------------------------
# register_jobs includes edgar_rss_poll
# ---------------------------------------------------------------------------


def test_register_jobs_includes_edgar_rss_poll() -> None:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    sched = AsyncIOScheduler()
    jobs_mod.register_jobs(sched, _FakeApp())
    job = sched.get_job("edgar_rss_poll")
    assert job is not None
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields.get("minute") == "*/10"

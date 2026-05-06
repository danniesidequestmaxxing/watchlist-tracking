"""Phase 10 — Bursa Malaysia announcements adapter + equity_my dispatch."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from src.adapters import bursa
from src.adapters.bursa import _normalize_html, _normalize_json, _parse_date
from src.db import cache
from src.db.schema import init_db
from src.scheduler import jobs as jobs_mod
from src.telegram.formatters import SOURCE_TTL_HOURS, format_bursa_catalyst


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
    p = tmp_path / "bursa_test.db"
    await init_db(p)
    return p


# ---------------------------------------------------------------------------
# Date parser
# ---------------------------------------------------------------------------


def test_parse_date_iso() -> None:
    assert _parse_date("2026-05-06") == date(2026, 5, 6)


def test_parse_date_iso_with_time() -> None:
    assert _parse_date("2026-05-06T08:30:00Z") == date(2026, 5, 6)


def test_parse_date_dd_mon_yyyy() -> None:
    assert _parse_date("06 May 2026") == date(2026, 5, 6)


def test_parse_date_dd_full_month() -> None:
    assert _parse_date("06 January 2026") == date(2026, 1, 6)


def test_parse_date_dd_slash() -> None:
    assert _parse_date("06/05/2026") == date(2026, 5, 6)


def test_parse_date_returns_none_for_garbage() -> None:
    assert _parse_date("not a date") is None
    assert _parse_date(None) is None
    assert _parse_date("") is None


# ---------------------------------------------------------------------------
# JSON normalization
# ---------------------------------------------------------------------------


def test_normalize_json_keeps_recent_drops_old() -> None:
    today = date(2026, 5, 6)
    cutoff = today - timedelta(days=30)
    raw = {
        "data": [
            {"date": "2026-05-04", "title": "Quarterly results", "url": "https://x"},
            {"date": "2026-04-01", "title": "Old", "url": "https://y"},  # past cutoff
            {"date": "bad-date", "title": "Skip"},
            {"announcement_date": "2026-05-01", "subject": "Dividend declared"},
        ]
    }
    items = _normalize_json(raw, "5347", cutoff)
    titles = [it["description"] for it in items]
    assert "Quarterly results" in titles
    assert "Dividend declared" in titles
    assert "Old" not in titles
    # ordered most-recent first
    assert items[0]["filing_date"] >= items[-1]["filing_date"]


def test_normalize_json_handles_list_root() -> None:
    cutoff = date(2026, 4, 1)
    raw = [{"date": "2026-05-01", "title": "X", "url": "https://x"}]
    items = _normalize_json(raw, "5347", cutoff)
    assert len(items) == 1


def test_normalize_json_handles_empty() -> None:
    assert _normalize_json({}, "5347", date(2026, 1, 1)) == []
    assert _normalize_json({"data": []}, "5347", date(2026, 1, 1)) == []


# ---------------------------------------------------------------------------
# HTML normalization
# ---------------------------------------------------------------------------


SAMPLE_HTML = """
<table>
<tr><td>06 May 2026</td><td>5347</td><td><a href="/announcements/123">Quarterly Report</a></td></tr>
<tr><td>04 Apr 2026</td><td>5347</td><td><a href="/announcements/122">Old filing</a></td></tr>
<tr><td>06 May 2026</td><td>1234</td><td><a href="/announcements/124">Other ticker</a></td></tr>
</table>
"""


def test_normalize_html_filters_by_ticker_and_cutoff() -> None:
    today = date(2026, 5, 6)
    cutoff = today - timedelta(days=30)
    items = _normalize_html(SAMPLE_HTML, "5347", cutoff)
    titles = [it["description"] for it in items]
    assert "Quarterly Report" in titles
    assert "Old filing" not in titles
    assert "Other ticker" not in titles


def test_normalize_html_resolves_relative_urls() -> None:
    items = _normalize_html(SAMPLE_HTML, "5347", date(2026, 5, 1))
    assert items[0]["source_url"].startswith("https://www.bursamalaysia.com/")


# ---------------------------------------------------------------------------
# fetch_announcements end-to-end (mocked httpx)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_announcements_json_path_and_cache(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    today = datetime.now(UTC).date()
    payload = {
        "data": [
            {
                "date": today.isoformat(),
                "title": "Q3 results",
                "url": "https://www.bursamalaysia.com/announcements/abc",
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "src.adapters.bursa.httpx.AsyncClient",
        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )

    out = await bursa.fetch_announcements(db_path, "5347", days_back=30)
    assert out["items"] and out["items"][0]["description"] == "Q3 results"

    # Second call comes from cache (handler not invoked)
    calls = []

    def trap(req):
        calls.append(req)
        return httpx.Response(500)

    monkeypatch.setattr(
        "src.adapters.bursa.httpx.AsyncClient",
        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(trap)}),
    )
    out2 = await bursa.fetch_announcements(db_path, "5347", days_back=30)
    assert out2["items"] == out["items"]
    assert calls == []

    meta = await cache.read_meta(db_path, "bursa_announcements:5347:30")
    assert meta is not None
    assert meta["data_type"] == "bursa_announcements"


@pytest.mark.asyncio
async def test_fetch_announcements_falls_back_to_html(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    today = datetime.now(UTC).date()
    html = (
        f"<table><tr><td>{today.strftime('%d %b %Y')}</td><td>5347</td>"
        f'<td><a href="/announcements/abc">HTML title</a></td></tr></table>'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "api.bursamalaysia.com" in str(request.url):
            return httpx.Response(500, text="upstream error")
        return httpx.Response(200, text=html)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "src.adapters.bursa.httpx.AsyncClient",
        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )

    out = await bursa.fetch_announcements(db_path, "5347", days_back=10)
    assert out["items"] and out["items"][0]["description"] == "HTML title"


@pytest.mark.asyncio
async def test_fetch_announcements_both_endpoints_fail(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "src.adapters.bursa.httpx.AsyncClient",
        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )

    out = await bursa.fetch_announcements(db_path, "5347", days_back=10)
    assert out["items"] == []
    assert "error" in out


# ---------------------------------------------------------------------------
# format_bursa_catalyst
# ---------------------------------------------------------------------------


def test_format_bursa_catalyst_lists_items() -> None:
    pulled = datetime.now(UTC).isoformat()
    text = format_bursa_catalyst(
        "5347",
        [
            {
                "filing_date": "2026-05-04",
                "form": "results",
                "description": "Quarterly results",
                "source_url": "https://www.bursamalaysia.com/announcements/abc",
            }
        ],
        pulled,
    )
    assert "5347" in text
    assert "Quarterly results" in text
    assert "results" in text
    assert "bursamalaysia.com" in text


def test_format_bursa_catalyst_empty() -> None:
    text = format_bursa_catalyst("5347", [], None)
    assert "no recent" in text


def test_bursa_appears_in_health_source_list() -> None:
    assert "bursa" in SOURCE_TTL_HOURS


# ---------------------------------------------------------------------------
# Scheduler: refresh_equity_my_ta + register_jobs
# ---------------------------------------------------------------------------


def test_register_jobs_includes_equity_my() -> None:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    sched = AsyncIOScheduler()
    jobs_mod.register_jobs(sched, _FakeApp())
    job = sched.get_job("ta_refresh_equity_my")
    assert job is not None
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields.get("minute") == "15"


@pytest.mark.asyncio
async def test_refresh_equity_my_ta_skips_when_market_closed(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.db.watchlist import add_entry

    await add_entry(db_path, 1, "5347", "equity_my")
    monkeypatch.setattr(jobs_mod, "DB_PATH", db_path)
    monkeypatch.setattr(jobs_mod, "OWNER_TELEGRAM_ID", 1)
    monkeypatch.setattr(jobs_mod, "is_my_market_open", lambda *_: False)

    called = []

    async def fake_push(*args, **kwargs):
        called.append(args)

    monkeypatch.setattr(jobs_mod, "_push_snapshot", fake_push)

    await jobs_mod.refresh_equity_my_ta(_FakeApp())
    assert called == []


@pytest.mark.asyncio
async def test_refresh_equity_my_ta_fires_when_market_open(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.db.watchlist import add_entry

    await add_entry(db_path, 1, "5347", "equity_my")
    await add_entry(db_path, 1, "NVDA", "equity_us")  # should be skipped
    monkeypatch.setattr(jobs_mod, "DB_PATH", db_path)
    monkeypatch.setattr(jobs_mod, "OWNER_TELEGRAM_ID", 1)
    monkeypatch.setattr(jobs_mod, "is_my_market_open", lambda *_: True)

    pushed: list[str] = []

    async def fake_push(app, entry):
        pushed.append(entry.ticker)

    monkeypatch.setattr(jobs_mod, "_push_snapshot", fake_push)

    await jobs_mod.refresh_equity_my_ta(_FakeApp())
    assert pushed == ["5347"]


# ---------------------------------------------------------------------------
# get_equity_snapshot routes equity_my via .KL suffix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_equity_snapshot_appends_kl_suffix_for_equity_my(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.adapters.base import OHLCVBundle
    from src.ta import pipeline

    fetched_with: list[str] = []

    class FakeYf:
        async def fetch_ohlcv(self, ticker, timeframe="1h", limit=200, exchange=None):
            fetched_with.append(ticker)
            base_ts = 1_700_000_000_000
            bars = []
            for i in range(60):
                p = 10.0 + i * 0.1
                bars.append([base_ts + i * 3_600_000, p, p + 1, p - 1, p + 0.5, 100.0])
            return OHLCVBundle(
                ticker=ticker,
                timeframe=timeframe,
                exchange=None,
                pulled_at=datetime.now(UTC),
                source_url="https://finance.yahoo.com",
                bars=bars,
            )

    monkeypatch.setattr(pipeline, "EquityYFAdapter", FakeYf)

    snap = await pipeline.get_equity_snapshot(db_path, "5347", "equity_my")
    assert fetched_with == ["5347.KL"]
    # Display ticker is the canonical Bursa code
    assert snap.ticker == "5347"

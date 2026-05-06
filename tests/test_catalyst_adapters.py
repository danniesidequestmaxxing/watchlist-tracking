"""Phase 6 adapters — Finnhub earnings/news + SEC EDGAR filings.

Network calls are mocked via httpx.MockTransport. Each test exercises the
normalize → cache → dispatch result shape that the catalyst agent will see.
"""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from src.adapters import finnhub, sec_edgar
from src.adapters.finnhub import _normalize_earnings, _normalize_news
from src.adapters.sec_edgar import _normalize_filings
from src.db import cache
from src.db.schema import init_db


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "adapters_test.db"
    await init_db(p)
    return p


# ---------------------------------------------------------------------------
# Finnhub normalize helpers
# ---------------------------------------------------------------------------


def test_normalize_earnings_filters_past_dates() -> None:
    today = date(2026, 5, 6)
    raw = {
        "earningsCalendar": [
            {"date": "2026-04-30", "symbol": "NVDA", "hour": "amc"},  # past
            {
                "date": "2026-05-22",
                "symbol": "NVDA",
                "hour": "amc",
                "epsEstimate": 1.2,
                "revenueEstimate": 30_000_000_000,
                "quarter": 1,
                "year": 2027,
            },
            {"date": "not-a-date", "symbol": "NVDA"},  # bad
            {"date": "2026-08-19", "symbol": "NVDA", "hour": "amc"},
            "junk",
        ]
    }
    items = _normalize_earnings(raw, "NVDA", today)
    dates = [i["earnings_date"] for i in items]
    assert "2026-04-30" not in dates
    assert "2026-05-22" in dates
    assert "2026-08-19" in dates
    assert all(i["ticker"] == "NVDA" for i in items)
    assert all(i["event_date"] == i["earnings_date"] for i in items)


def test_normalize_news_drops_old_and_future() -> None:
    today = date(2026, 5, 6)
    epoch = datetime(2026, 5, 5, 12, tzinfo=UTC).timestamp()
    far_past = datetime(2026, 4, 1, tzinfo=UTC).timestamp()
    future = datetime(2026, 5, 10, tzinfo=UTC).timestamp()
    raw = [
        {"datetime": epoch, "headline": "fresh", "url": "https://x", "source": "src"},
        {"datetime": far_past, "headline": "old"},
        {"datetime": future, "headline": "future"},
        {"datetime": "not-a-number", "headline": "bad ts"},
        {"datetime": 0, "headline": "zero ts"},
        "junk",
    ]
    items = _normalize_news(raw, today, days_back=7)
    headlines = [i["headline"] for i in items]
    assert "fresh" in headlines
    assert "old" not in headlines
    assert "future" not in headlines


def test_normalize_news_handles_non_list() -> None:
    assert _normalize_news(None, date(2026, 5, 6), 7) == []
    assert _normalize_news({"oops": 1}, date(2026, 5, 6), 7) == []


# ---------------------------------------------------------------------------
# Finnhub HTTP-level: mock httpx transport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_earnings_calendar_end_to_end(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(finnhub, "FINNHUB_API_KEY", "fake-key")

    today = datetime.now(UTC).date()
    earnings_day = today + timedelta(days=14)
    raw_payload = {
        "earningsCalendar": [
            {
                "date": earnings_day.isoformat(),
                "symbol": "NVDA",
                "hour": "amc",
                "epsEstimate": 1.2,
                "revenueEstimate": 30_000_000_000,
                "quarter": 1,
                "year": 2027,
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert "calendar/earnings" in str(request.url)
        assert request.url.params["symbol"] == "NVDA"
        # Token is now in a header, not the URL — confirms no key leakage in
        # the request line / error logs.
        assert "token" not in request.url.params
        assert request.headers.get("X-Finnhub-Token") == "fake-key"
        return httpx.Response(200, json=raw_payload)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("src.adapters.finnhub.httpx.AsyncClient", patched_async_client)

    out = await finnhub.get_earnings_calendar(db_path, "NVDA", days_ahead=30)
    assert "error" not in out
    assert len(out["items"]) == 1
    assert out["items"][0]["earnings_date"] == earnings_day.isoformat()

    # Cache hit on second call: handler must NOT fire
    handler_calls = []
    transport2 = httpx.MockTransport(lambda r: handler_calls.append(r) or httpx.Response(500))
    monkeypatch.setattr(
        "src.adapters.finnhub.httpx.AsyncClient",
        lambda *a, **k: real_async_client(*a, **{**k, "transport": transport2}),
    )
    out2 = await finnhub.get_earnings_calendar(db_path, "NVDA", days_ahead=30)
    assert out2["items"] == out["items"]
    assert handler_calls == []


@pytest.mark.asyncio
async def test_get_earnings_calendar_no_api_key(db_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(finnhub, "FINNHUB_API_KEY", None)
    out = await finnhub.get_earnings_calendar(db_path, "NVDA")
    assert out["items"] == []
    assert "FINNHUB_API_KEY" in out["error"]


@pytest.mark.asyncio
async def test_search_news_end_to_end(db_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(finnhub, "FINNHUB_API_KEY", "fake-key")
    yesterday = (datetime.now(UTC) - timedelta(days=1)).timestamp()
    payload = [
        {
            "datetime": yesterday,
            "headline": "NVDA hits new high",
            "summary": "Nvidia stock hit a new ATH",
            "url": "https://news.example.com/nvda",
            "source": "Example News",
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert "company-news" in str(request.url)
        return httpx.Response(200, json=payload)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "src.adapters.finnhub.httpx.AsyncClient",
        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    out = await finnhub.search_news(db_path, "NVDA", days_back=7)
    assert len(out["items"]) == 1
    assert out["items"][0]["headline"] == "NVDA hits new high"


# ---------------------------------------------------------------------------
# SEC EDGAR
# ---------------------------------------------------------------------------


def test_normalize_filings_filters_form_and_date() -> None:
    today = date(2026, 5, 6)
    cutoff = today - timedelta(days=30)
    raw = {
        "filings": {
            "recent": {
                "accessionNumber": ["0001-25-000001", "0001-25-000002", "0001-25-000003"],
                "form": ["8-K", "10-K", "8-K"],
                "filingDate": ["2026-05-01", "2026-04-30", "2026-01-01"],  # last is past cutoff
                "primaryDocument": ["a.htm", "b.htm", "c.htm"],
            }
        }
    }
    items = _normalize_filings(raw, cik=320193, ticker="NVDA", form_types=["8-K"], cutoff=cutoff)
    forms = [i["form"] for i in items]
    assert forms == ["8-K"]  # only the May 1 8-K (May 2026 - 30d cutoff = April 6)
    assert items[0]["filing_date"] == "2026-05-01"
    assert items[0]["accession"] == "0001-25-000001"
    assert "Archives/edgar/data/320193" in items[0]["source_url"]


def test_normalize_filings_handles_missing_recent() -> None:
    today = date(2026, 5, 6)
    assert _normalize_filings({}, 1, "X", ["8-K"], today) == []
    assert _normalize_filings({"filings": {}}, 1, "X", ["8-K"], today) == []


@pytest.mark.asyncio
async def test_read_sec_filings_end_to_end(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    today = datetime.now(UTC).date()
    recent_filing = today - timedelta(days=3)

    tickers_payload = {
        "0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
    }
    submissions_payload = {
        "cik": "0001045810",
        "name": "NVIDIA CORP",
        "filings": {
            "recent": {
                "accessionNumber": ["0001045810-25-000001"],
                "form": ["8-K"],
                "filingDate": [recent_filing.isoformat()],
                "primaryDocument": ["nvda8k.htm"],
            }
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "company_tickers.json" in url:
            return httpx.Response(200, json=tickers_payload)
        if "submissions/CIK" in url:
            return httpx.Response(200, json=submissions_payload)
        return httpx.Response(404)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "src.adapters.sec_edgar.httpx.AsyncClient",
        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )

    out = await sec_edgar.read_sec_filings(db_path, "NVDA", form_types=["8-K"], days_back=10)
    assert "error" not in out
    assert len(out["items"]) == 1
    item = out["items"][0]
    assert item["form"] == "8-K"
    assert item["filing_date"] == recent_filing.isoformat()
    assert item["event_date"] == recent_filing.isoformat()


@pytest.mark.asyncio
async def test_read_sec_filings_unknown_ticker(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "company_tickers.json" in str(request.url):
            return httpx.Response(200, json={})
        return httpx.Response(404)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "src.adapters.sec_edgar.httpx.AsyncClient",
        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    out = await sec_edgar.read_sec_filings(db_path, "NOSUCH", form_types=["8-K"])
    assert out["items"] == []
    assert "no CIK" in out["error"]


@pytest.mark.asyncio
async def test_read_sec_filings_caches_ticker_map(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second call doesn't refetch company_tickers.json."""
    today = datetime.now(UTC).date()

    tickers_payload = {
        "0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
    }
    submissions_payload = {
        "filings": {
            "recent": {
                "accessionNumber": ["0001045810-25-000001"],
                "form": ["8-K"],
                "filingDate": [today.isoformat()],
                "primaryDocument": ["nvda8k.htm"],
            }
        }
    }

    counts = {"tickers": 0, "subs": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "company_tickers.json" in url:
            counts["tickers"] += 1
            return httpx.Response(200, json=tickers_payload)
        if "submissions/CIK" in url:
            counts["subs"] += 1
            return httpx.Response(200, json=submissions_payload)
        return httpx.Response(404)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "src.adapters.sec_edgar.httpx.AsyncClient",
        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )

    await sec_edgar.read_sec_filings(db_path, "NVDA", form_types=["8-K"], days_back=10)
    # Different cache key (different days_back) so submissions endpoint gets hit
    # but ticker map should be cached.
    await sec_edgar.read_sec_filings(db_path, "NVDA", form_types=["8-K"], days_back=20)

    assert counts["tickers"] == 1
    assert counts["subs"] == 2


# ---------------------------------------------------------------------------
# Cache writes use the right data_type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_earnings_cache_uses_correct_data_type(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(finnhub, "FINNHUB_API_KEY", "fake-key")

    def handler(request):
        return httpx.Response(200, json={"earningsCalendar": []})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "src.adapters.finnhub.httpx.AsyncClient",
        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )

    await finnhub.get_earnings_calendar(db_path, "AAPL", days_ahead=30)
    meta = await cache.read_meta(db_path, "earnings:AAPL:30")
    assert meta is not None
    assert meta["data_type"] == "earnings_calendar"

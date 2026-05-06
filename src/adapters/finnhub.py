"""Finnhub adapter — earnings calendar + company news (Phase 6 catalyst)."""

import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from src.config import FINNHUB_API_KEY
from src.db import cache, health

logger = logging.getLogger(__name__)


SOURCE_URL = "https://finnhub.io"
API_BASE = "https://finnhub.io/api/v1"
HEALTH_SOURCE = "finnhub"


def _empty(pulled_at: datetime, error: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "items": [],
        "pulled_at": pulled_at.isoformat(),
        "source_url": SOURCE_URL,
    }
    if error:
        payload["error"] = error
    return payload


def _normalize_earnings(raw: dict[str, Any], ticker: str, today: date) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for entry in raw.get("earningsCalendar") or []:
        if not isinstance(entry, dict):
            continue
        try:
            ev_date = date.fromisoformat(entry.get("date") or "")
        except (TypeError, ValueError):
            continue
        if ev_date < today:
            continue
        hour = entry.get("hour") or ""
        items.append(
            {
                "ticker": ticker,
                "earnings_date": ev_date.isoformat(),
                "event_date": ev_date.isoformat(),
                "hour": hour,
                "quarter": entry.get("quarter"),
                "year": entry.get("year"),
                "eps_estimate": entry.get("epsEstimate"),
                "revenue_estimate": entry.get("revenueEstimate"),
                "description": (f"{ticker} earnings" + (f" ({hour})" if hour else "")),
                "source_url": SOURCE_URL,
            }
        )
    return items


async def get_earnings_calendar(
    db_path: Path,
    ticker: str,
    days_ahead: int = 60,
) -> dict[str, Any]:
    """Tool dispatcher for `get_earnings_calendar`. Returns {items, pulled_at, source_url}."""
    sym = ticker.upper()
    cache_key = f"earnings:{sym}:{days_ahead}"

    cached = await cache.read_if_fresh(db_path, cache_key, cache.TTL_SECONDS["earnings_calendar"])
    if cached is not None:
        return cached

    pulled_at = datetime.now(UTC)
    if not FINNHUB_API_KEY:
        return _empty(pulled_at, error="FINNHUB_API_KEY not configured")

    end = pulled_at + timedelta(days=days_ahead)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{API_BASE}/calendar/earnings",
                params={
                    "symbol": sym,
                    "from": pulled_at.date().isoformat(),
                    "to": end.date().isoformat(),
                    "token": FINNHUB_API_KEY,
                },
            )
            resp.raise_for_status()
            raw = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("finnhub earnings fetch failed for %s: %s", sym, exc)
        await health.record(db_path, source=HEALTH_SOURCE, status="error", details=str(exc))
        return _empty(pulled_at, error=str(exc))

    if not isinstance(raw, dict):
        return _empty(pulled_at, error="unexpected earnings response shape")

    items = _normalize_earnings(raw, sym, pulled_at.date())
    result: dict[str, Any] = {
        "items": items,
        "pulled_at": pulled_at.isoformat(),
        "source_url": SOURCE_URL,
    }

    await cache.write(
        db_path,
        cache_key=cache_key,
        data_type="earnings_calendar",
        payload=result,
        pulled_at=pulled_at,
        source_url=SOURCE_URL,
        ticker=sym,
    )
    await health.record(
        db_path,
        source=HEALTH_SOURCE,
        status="ok",
        details=f"earnings {sym}: {len(items)} items",
    )
    return result


def _normalize_news(
    raw: list[dict[str, Any]] | None,
    today: date,
    days_back: int,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return items
    cutoff = today - timedelta(days=days_back)
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        ts = entry.get("datetime")
        if not isinstance(ts, (int, float)) or ts <= 0:
            continue
        try:
            item_date = datetime.fromtimestamp(ts, UTC).date()
        except (OSError, OverflowError, ValueError):
            continue
        if item_date < cutoff or item_date > today:
            continue
        items.append(
            {
                "headline": entry.get("headline") or "",
                "summary": entry.get("summary") or "",
                "date": item_date.isoformat(),
                "source": entry.get("source") or "",
                "source_url": entry.get("url") or SOURCE_URL,
            }
        )
    items.sort(key=lambda x: x["date"], reverse=True)
    return items[:limit]


async def search_news(
    db_path: Path,
    query: str,
    days_back: int = 7,
) -> dict[str, Any]:
    """Tool dispatcher for `search_news`. `query` is treated as a ticker symbol."""
    sym = query.upper()
    cache_key = f"news:{sym}:{days_back}"

    cached = await cache.read_if_fresh(db_path, cache_key, cache.TTL_SECONDS["news"])
    if cached is not None:
        return cached

    pulled_at = datetime.now(UTC)
    if not FINNHUB_API_KEY:
        return _empty(pulled_at, error="FINNHUB_API_KEY not configured")

    today = pulled_at.date()
    start = today - timedelta(days=days_back)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{API_BASE}/company-news",
                params={
                    "symbol": sym,
                    "from": start.isoformat(),
                    "to": today.isoformat(),
                    "token": FINNHUB_API_KEY,
                },
            )
            resp.raise_for_status()
            raw = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("finnhub news fetch failed for %s: %s", sym, exc)
        await health.record(db_path, source=HEALTH_SOURCE, status="error", details=str(exc))
        return _empty(pulled_at, error=str(exc))

    items = _normalize_news(raw, today, days_back)
    result: dict[str, Any] = {
        "items": items,
        "pulled_at": pulled_at.isoformat(),
        "source_url": SOURCE_URL,
    }

    await cache.write(
        db_path,
        cache_key=cache_key,
        data_type="news",
        payload=result,
        pulled_at=pulled_at,
        source_url=SOURCE_URL,
        ticker=sym,
    )
    await health.record(
        db_path, source=HEALTH_SOURCE, status="ok", details=f"news {sym}: {len(items)} items"
    )
    return result

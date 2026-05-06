"""Trading Economics adapter — macro calendar (FOMC, CPI, NFP, etc.)."""

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from src.config import TRADING_ECONOMICS_API_KEY
from src.db import cache, health

logger = logging.getLogger(__name__)


SOURCE_URL = "https://tradingeconomics.com"
API_BASE = "https://api.tradingeconomics.com"
HEALTH_SOURCE = "trading-economics"


def _empty(pulled_at: datetime, error: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "items": [],
        "pulled_at": pulled_at.isoformat(),
        "source_url": SOURCE_URL,
    }
    if error:
        payload["error"] = error
    return payload


def _parse_event_date(raw: Any) -> datetime | None:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _normalize(
    raw: list[dict[str, Any]],
    today: datetime,
    cutoff: datetime,
    importance_min: int,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for entry in raw:
        importance = int(entry.get("Importance") or 0)
        if importance < importance_min:
            continue
        ev_dt = _parse_event_date(entry.get("Date") or entry.get("DateEnd"))
        if ev_dt is None:
            continue
        if ev_dt.tzinfo is None:
            ev_dt = ev_dt.replace(tzinfo=UTC)
        if ev_dt < today or ev_dt > cutoff:
            continue
        items.append(
            {
                "event_type": entry.get("Category") or "macro",
                "event_date": ev_dt.date().isoformat(),
                "country": entry.get("Country"),
                "description": entry.get("Event") or "",
                "importance": importance,
                "actual": entry.get("Actual"),
                "previous": entry.get("Previous"),
                "forecast": entry.get("Forecast"),
                "source_url": entry.get("URL") or SOURCE_URL,
            }
        )
    return items


async def get_macro_events(
    db_path: Path,
    days_ahead: int = 14,
    importance_min: int = 2,
) -> dict[str, Any]:
    """Tool dispatcher for `get_macro_events`.

    Falls back to the public `c=guest:guest` credential when no API key is
    set. Errors return {"items": [], "error": "..."}.
    """
    cache_key = f"macro_events:{days_ahead}:{importance_min}"

    cached = await cache.read_if_fresh(db_path, cache_key, cache.TTL_SECONDS["macro_events"])
    if cached is not None:
        return cached

    pulled_at = datetime.now(UTC)
    end = pulled_at + timedelta(days=days_ahead)

    api_key = TRADING_ECONOMICS_API_KEY or "guest:guest"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{API_BASE}/calendar",
                params={
                    "c": api_key,
                    "d1": pulled_at.date().isoformat(),
                    "d2": end.date().isoformat(),
                    "format": "json",
                },
            )
            resp.raise_for_status()
            raw = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("trading_economics fetch failed: %s", exc)
        await health.record(db_path, source=HEALTH_SOURCE, status="error", details=str(exc))
        return _empty(pulled_at, error=str(exc))

    if not isinstance(raw, list):
        logger.warning("trading_economics returned unexpected payload: %r", type(raw).__name__)
        return _empty(pulled_at, error="unexpected response shape")

    items = _normalize(raw, pulled_at, end, importance_min)
    result: dict[str, Any] = {
        "items": items,
        "pulled_at": pulled_at.isoformat(),
        "source_url": SOURCE_URL,
    }

    await cache.write(
        db_path,
        cache_key=cache_key,
        data_type="macro_events",
        payload=result,
        pulled_at=pulled_at,
        source_url=SOURCE_URL,
    )
    await health.record(db_path, source=HEALTH_SOURCE, status="ok", details=f"{len(items)} items")
    return result

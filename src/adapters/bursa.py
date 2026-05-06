"""Bursa Malaysia announcements adapter (Phase 10).

Bursa doesn't publish a stable, documented public REST API. The bot tries
the JSON announcement endpoint first and falls back to the public HTML
listing if that fails. The HTML parser is permissive — Bursa changes the
markup occasionally, so structural drift is logged to `health_log` rather
than blowing up.
"""

import logging
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from src.db import cache, health

logger = logging.getLogger(__name__)


SOURCE_URL = "https://www.bursamalaysia.com"
JSON_URL = "https://api.bursamalaysia.com/v1/announcements/company-announcements"
HTML_URL = "https://www.bursamalaysia.com/market_information/announcements/company_announcement"
HEALTH_SOURCE = "bursa"

# Browser-ish UA so the site doesn't 403 us as a bot.
_USER_AGENT = (
    "Mozilla/5.0 (compatible; trading-bot; "
    "+https://github.com/danniesidequestmaxxing/watchlist-tracking)"
)


def _empty(pulled_at: datetime, error: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "items": [],
        "pulled_at": pulled_at.isoformat(),
        "source_url": SOURCE_URL,
    }
    if error:
        payload["error"] = error
    return payload


def _parse_date(raw: Any) -> date | None:
    if not raw:
        return None
    if isinstance(raw, date):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, str):
        for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(raw[:20], fmt).date()
            except ValueError:
                continue
        # ISO with time
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def _normalize_json(raw: Any, ticker: str, cutoff: date) -> list[dict[str, Any]]:
    """Best-effort parse of the JSON endpoint."""
    items_raw: list[Any] = []
    if isinstance(raw, dict):
        items_raw = raw.get("data") or raw.get("announcements") or raw.get("items") or []
    elif isinstance(raw, list):
        items_raw = raw

    items: list[dict[str, Any]] = []
    for entry in items_raw:
        if not isinstance(entry, dict):
            continue
        ann_date = _parse_date(
            entry.get("date") or entry.get("announcement_date") or entry.get("created_at")
        )
        if ann_date is None or ann_date < cutoff:
            continue
        title = (
            entry.get("title")
            or entry.get("subject")
            or entry.get("description")
            or "Bursa announcement"
        )
        url = entry.get("url") or entry.get("link") or HTML_URL
        items.append(
            {
                "ticker": ticker,
                "form": entry.get("category") or "announcement",
                "filing_date": ann_date.isoformat(),
                "event_date": ann_date.isoformat(),
                "description": str(title)[:300],
                "source_url": str(url),
            }
        )
    items.sort(key=lambda x: x["filing_date"], reverse=True)
    return items


_HTML_ROW_RE = re.compile(
    # Match a table row with a date cell, a stock-code cell containing the
    # ticker, and a title cell linking to the announcement detail. Permissive
    # because Bursa's table layout shifts.
    r"<tr[^>]*>.*?"
    r"(\d{2}\s+\w{3}\s+\d{4}).*?"
    r"<td[^>]*>\s*(\d{4,5})\s*</td>.*?"
    r"<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)


def _normalize_html(html: str, ticker: str, cutoff: date) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for match in _HTML_ROW_RE.finditer(html):
        date_str, code, href, title = match.groups()
        if code.lstrip("0") != ticker.lstrip("0"):
            continue
        ann_date = _parse_date(date_str)
        if ann_date is None or ann_date < cutoff:
            continue
        clean_title = re.sub(r"<[^>]+>", "", title).strip()
        url = href if href.startswith("http") else f"{SOURCE_URL}{href}"
        items.append(
            {
                "ticker": ticker,
                "form": "announcement",
                "filing_date": ann_date.isoformat(),
                "event_date": ann_date.isoformat(),
                "description": clean_title[:300] or "Bursa announcement",
                "source_url": url,
            }
        )
    items.sort(key=lambda x: x["filing_date"], reverse=True)
    return items


async def fetch_announcements(
    db_path: Path,
    ticker: str,
    days_back: int = 30,
) -> dict[str, Any]:
    """Tool-style return shape: {items, pulled_at, source_url, [error]}."""
    sym = ticker.strip()
    cache_key = f"bursa_announcements:{sym}:{days_back}"
    cached = await cache.read_if_fresh(db_path, cache_key, cache.TTL_SECONDS["bursa_announcements"])
    if cached is not None:
        return cached

    pulled_at = datetime.now(UTC)
    cutoff = pulled_at.date() - timedelta(days=days_back)
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json, text/html"}

    items: list[dict[str, Any]] = []
    error: str | None = None

    try:
        async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
            try:
                resp = await client.get(JSON_URL, params={"company": sym})
                resp.raise_for_status()
                items = _normalize_json(resp.json(), sym, cutoff)
            except (httpx.HTTPError, ValueError) as exc:
                logger.info("bursa JSON endpoint failed for %s: %s; falling back to HTML", sym, exc)
                resp = await client.get(HTML_URL, params={"company": sym})
                resp.raise_for_status()
                items = _normalize_html(resp.text, sym, cutoff)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("bursa fetch failed for %s: %s", sym, exc)
        error = str(exc)
        await health.record(db_path, source=HEALTH_SOURCE, status="error", details=error)
        return _empty(pulled_at, error=error)

    result: dict[str, Any] = {
        "items": items,
        "pulled_at": pulled_at.isoformat(),
        "source_url": SOURCE_URL,
    }
    await cache.write(
        db_path,
        cache_key=cache_key,
        data_type="bursa_announcements",
        payload=result,
        pulled_at=pulled_at,
        source_url=SOURCE_URL,
        ticker=sym,
    )
    await health.record(
        db_path, source=HEALTH_SOURCE, status="ok", details=f"{sym}: {len(items)} items"
    )
    return result

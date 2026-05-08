"""China CNINFO (cninfo.com.cn) — corporate announcements adapter for SSE/SZSE.

CNINFO has a JSON-ish search endpoint at /new/hisAnnouncement/query that the
public site uses; we POST to it with the stock code prefixed by `gssh` (SSE)
or `gssz` (SZSE) and parse the announcements list. No API key needed.
"""

import logging
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from src.db import cache, health

logger = logging.getLogger(__name__)


SOURCE_URL = "http://www.cninfo.com.cn"
SEARCH_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
ANN_VIEW_URL = (
    "http://www.cninfo.com.cn/new/disclosure/detail?stockCode={code}&announcementId={ann_id}"
)
HEALTH_SOURCE = "cninfo"

_USER_AGENT = (
    "Mozilla/5.0 (compatible; trading-bot; "
    "+https://github.com/danniesidequestmaxxing/watchlist-tracking)"
)

_TICKER_RE = re.compile(r"^(\d{6})\.(SS|SZ)$", re.IGNORECASE)


def _empty(pulled_at: datetime, error: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "items": [],
        "pulled_at": pulled_at.isoformat(),
        "source_url": SOURCE_URL,
    }
    if error:
        payload["error"] = error
    return payload


def _ticker_to_market(ticker: str) -> tuple[str, str] | None:
    """Return (stock_code, plate_prefix) where plate_prefix is gssh or gssz."""
    m = _TICKER_RE.match(ticker.strip())
    if not m:
        return None
    code = m.group(1)
    plate = "gssh" if m.group(2).upper() == "SS" else "gssz"
    return code, plate


def _parse_announcement_time(value: Any) -> date | None:
    """Cninfo returns announcementTime as either a unix-millis int or a string."""
    if isinstance(value, (int, float)) and value > 0:
        try:
            return datetime.fromtimestamp(value / 1000, UTC).date()
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except (TypeError, ValueError):
            return None
    return None


def _normalize(raw: dict[str, Any], code: str, ticker: str, cutoff: date) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        return []
    rows = raw.get("announcements") or []
    items: list[dict[str, Any]] = []
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        filing_date = _parse_announcement_time(entry.get("announcementTime"))
        if filing_date is None or filing_date < cutoff:
            continue
        title = entry.get("announcementTitle") or "CNINFO announcement"
        ann_id = entry.get("announcementId")
        url = ANN_VIEW_URL.format(code=code, ann_id=ann_id) if ann_id else SOURCE_URL
        items.append(
            {
                "ticker": ticker,
                "form": entry.get("announcementType") or "announcement",
                "filing_date": filing_date.isoformat(),
                "event_date": filing_date.isoformat(),
                "description": str(title)[:300],
                "source_url": url,
            }
        )
    items.sort(key=lambda x: x["filing_date"], reverse=True)
    return items


async def fetch_disclosures(
    db_path: Path,
    ticker: str,
    days_back: int = 30,
) -> dict[str, Any]:
    pulled_at = datetime.now(UTC)
    parsed = _ticker_to_market(ticker)
    if parsed is None:
        return _empty(pulled_at, error=f"invalid CN ticker shape: {ticker!r}")
    code, plate = parsed

    cache_key = f"cninfo_announcements:{code}:{days_back}"
    cached = await cache.read_if_fresh(
        db_path, cache_key, cache.TTL_SECONDS["cninfo_announcements"]
    )
    if cached is not None:
        return cached

    cutoff = pulled_at.date() - timedelta(days=days_back)
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
            resp = await client.post(
                SEARCH_URL,
                data={
                    "stock": f"{code},{plate}{code}",
                    "tabName": "fulltext",
                    "pageSize": "30",
                    "pageNum": "1",
                    "column": "szse" if plate == "gssz" else "sse",
                    "category": "",
                    "plate": plate,
                    "seDate": f"{cutoff.isoformat()}~{pulled_at.date().isoformat()}",
                    "searchkey": "",
                    "secid": "",
                    "sortName": "",
                    "sortType": "",
                    "isHLtitle": "true",
                },
            )
            resp.raise_for_status()
            raw = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("cninfo fetch failed for %s: %s", ticker, exc)
        await health.record(db_path, source=HEALTH_SOURCE, status="error", details=str(exc))
        return _empty(pulled_at, error=str(exc))

    items = _normalize(raw, code, ticker.upper(), cutoff)
    result: dict[str, Any] = {
        "items": items,
        "pulled_at": pulled_at.isoformat(),
        "source_url": SOURCE_URL,
    }
    await cache.write(
        db_path,
        cache_key=cache_key,
        data_type="cninfo_announcements",
        payload=result,
        pulled_at=pulled_at,
        source_url=SOURCE_URL,
        ticker=ticker.upper(),
    )
    await health.record(
        db_path, source=HEALTH_SOURCE, status="ok", details=f"{ticker}: {len(items)} items"
    )
    return result

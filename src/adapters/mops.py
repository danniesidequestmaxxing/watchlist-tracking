"""Taiwan MOPS (mops.twse.com.tw) — corporate announcements adapter.

MOPS doesn't publish a stable JSON API; we POST to the public material-information
search and parse the resulting HTML table. Permissive parser because MOPS
markup shifts; structural drift is logged to `health_log` rather than raising.
"""

import logging
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from src.db import cache, health

logger = logging.getLogger(__name__)


SOURCE_URL = "https://mops.twse.com.tw"
SEARCH_URL = "https://mops.twse.com.tw/mops/web/ajax_t05st02"
HEALTH_SOURCE = "mops"

_USER_AGENT = (
    "Mozilla/5.0 (compatible; trading-bot; "
    "+https://github.com/danniesidequestmaxxing/watchlist-tracking)"
)

_TICKER_RE = re.compile(r"^(\d{4})(?:\.TWO?)?$", re.IGNORECASE)

# Permissive row regex — date cell, ticker cell, title cell with anchor.
_ROW_RE = re.compile(
    r"<tr[^>]*>.*?"
    # Year may come ROC-style (1XX/MM/DD) or western (YYYY/MM/DD or YYYY-MM-DD).
    r"(\d{2,4}[/-]\d{1,2}[/-]\d{1,2}).*?"
    r"<td[^>]*>\s*(\d{4})\s*</td>.*?"
    r"<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
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


def _ticker_to_code(ticker: str) -> str | None:
    m = _TICKER_RE.match(ticker.strip())
    return m.group(1) if m else None


def _parse_date(raw: str) -> date | None:
    """Accept ROC-year (1130506) and western dates."""
    raw = raw.strip().replace("-", "/")
    parts = raw.split("/")
    if len(parts) != 3:
        return None
    try:
        y, m, d = (int(p) for p in parts)
    except ValueError:
        return None
    if y < 200:
        # ROC year (民國) — add 1911 to convert to AD
        y += 1911
    if not (1900 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31):
        return None
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _normalize_html(html: str, code: str, ticker: str, cutoff: date) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for match in _ROW_RE.finditer(html):
        date_str, row_code, href, title = match.groups()
        if row_code != code:
            continue
        filing_date = _parse_date(date_str)
        if filing_date is None or filing_date < cutoff:
            continue
        clean_title = re.sub(r"<[^>]+>", "", title).strip()
        url = href if href.startswith("http") else f"{SOURCE_URL}{href}"
        items.append(
            {
                "ticker": ticker,
                "form": "announcement",
                "filing_date": filing_date.isoformat(),
                "event_date": filing_date.isoformat(),
                "description": clean_title[:300] or "MOPS announcement",
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
    code = _ticker_to_code(ticker)
    if code is None:
        return _empty(pulled_at, error=f"invalid TW ticker shape: {ticker!r}")

    cache_key = f"mops_announcements:{code}:{days_back}"
    cached = await cache.read_if_fresh(db_path, cache_key, cache.TTL_SECONDS["mops_announcements"])
    if cached is not None:
        return cached

    cutoff = pulled_at.date() - timedelta(days=days_back)
    headers = {"User-Agent": _USER_AGENT, "Accept": "text/html, */*"}

    try:
        async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
            resp = await client.post(
                SEARCH_URL,
                data={
                    "encodeURIComponent": "1",
                    "step": "1",
                    "firstin": "1",
                    "off": "1",
                    "co_id": code,
                    "year": "",
                    "month": "",
                    "b_date": "",
                    "e_date": "",
                },
            )
            resp.raise_for_status()
            html = resp.text
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("mops fetch failed for %s: %s", ticker, exc)
        await health.record(db_path, source=HEALTH_SOURCE, status="error", details=str(exc))
        return _empty(pulled_at, error=str(exc))

    items = _normalize_html(html, code, ticker.upper(), cutoff)
    result: dict[str, Any] = {
        "items": items,
        "pulled_at": pulled_at.isoformat(),
        "source_url": SOURCE_URL,
    }
    await cache.write(
        db_path,
        cache_key=cache_key,
        data_type="mops_announcements",
        payload=result,
        pulled_at=pulled_at,
        source_url=SOURCE_URL,
        ticker=ticker.upper(),
    )
    await health.record(
        db_path, source=HEALTH_SOURCE, status="ok", details=f"{ticker}: {len(items)} items"
    )
    return result

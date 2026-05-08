"""Korea DART (opendart.fss.or.kr) — corporate filings adapter.

DART has a free official API but its `list.json` endpoint expects an 8-digit
`corp_code` rather than the 6-digit stock ticker. We download the master
corp_code XML once (lives in a zipped file at /api/corpCode.xml), build the
ticker→corp_code map, cache it for 7 days, then query disclosures by ticker.
"""

import io
import logging
import re
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from src.config import DART_API_KEY
from src.db import cache, health

logger = logging.getLogger(__name__)


SOURCE_URL = "https://opendart.fss.or.kr"
API_BASE = "https://opendart.fss.or.kr/api"
HEALTH_SOURCE = "dart"
CORP_CODE_CACHE_KEY = "dart:corp_code_map"
CORP_CODE_TTL = 7 * 24 * 60 * 60

# Six-digit stock code, optional .KS / .KQ suffix.
_TICKER_RE = re.compile(r"^(\d{6})(?:\.K[SQ])?$", re.IGNORECASE)
_CORP_RE = re.compile(
    r"<list>.*?<corp_code>\s*(\d+)\s*</corp_code>.*?<stock_code>\s*(\d+)\s*</stock_code>",
    re.DOTALL,
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


def _ticker_to_stock_code(ticker: str) -> str | None:
    m = _TICKER_RE.match(ticker.strip())
    return m.group(1) if m else None


async def _load_corp_code_map(db_path: Path, client: httpx.AsyncClient) -> dict[str, str]:
    """Return {stock_code: corp_code}. Cached for 7d."""
    cached = await cache.read_if_fresh(db_path, CORP_CODE_CACHE_KEY, CORP_CODE_TTL)
    if cached is not None:
        return {k: str(v) for k, v in cached.get("map", {}).items()}

    resp = await client.get(f"{API_BASE}/corpCode.xml", params={"crtfc_key": DART_API_KEY})
    resp.raise_for_status()
    # The response is a zip archive containing a single XML file.
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = zf.namelist()
        if not names:
            return {}
        xml_bytes = zf.read(names[0])
    text = xml_bytes.decode("utf-8", errors="replace")

    mapping: dict[str, str] = {}
    for corp_code, stock_code in _CORP_RE.findall(text):
        if stock_code and stock_code != "0":
            mapping[stock_code.zfill(6)] = corp_code

    await cache.write(
        db_path,
        cache_key=CORP_CODE_CACHE_KEY,
        data_type="dart_corp_code",
        payload={"map": mapping},
        pulled_at=datetime.now(UTC),
        source_url=f"{API_BASE}/corpCode.xml",
    )
    return mapping


def _normalize(raw: dict[str, Any], ticker: str, cutoff: date) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        return []
    if raw.get("status") and raw.get("status") not in ("000",):
        # DART error code; e.g. 013 = "no result". Treat as empty.
        return []
    rows = raw.get("list") or []
    items: list[dict[str, Any]] = []
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        rcept_dt = entry.get("rcept_dt") or ""
        try:
            filing_date = datetime.strptime(rcept_dt[:8], "%Y%m%d").date()
        except (TypeError, ValueError):
            continue
        if filing_date < cutoff:
            continue
        rcept_no = entry.get("rcept_no")
        url = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}" if rcept_no else SOURCE_URL
        items.append(
            {
                "ticker": ticker,
                "form": entry.get("report_nm") or "filing",
                "filing_date": filing_date.isoformat(),
                "event_date": filing_date.isoformat(),
                "description": entry.get("report_nm") or "DART filing",
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
    """Tool-shape return: {items, pulled_at, source_url, [error]}."""
    pulled_at = datetime.now(UTC)
    if not DART_API_KEY:
        return _empty(pulled_at, error="DART_API_KEY not configured")

    stock_code = _ticker_to_stock_code(ticker)
    if stock_code is None:
        return _empty(pulled_at, error=f"invalid KR ticker shape: {ticker!r}")

    cache_key = f"dart_filings:{stock_code}:{days_back}"
    cached = await cache.read_if_fresh(db_path, cache_key, cache.TTL_SECONDS["dart_filings"])
    if cached is not None:
        return cached

    cutoff = pulled_at.date() - timedelta(days=days_back)
    bgn = cutoff.strftime("%Y%m%d")
    end = pulled_at.date().strftime("%Y%m%d")

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            corp_map = await _load_corp_code_map(db_path, client)
            corp_code = corp_map.get(stock_code)
            if not corp_code:
                return _empty(pulled_at, error=f"no DART corp_code for {stock_code}")
            resp = await client.get(
                f"{API_BASE}/list.json",
                params={
                    "crtfc_key": DART_API_KEY,
                    "corp_code": corp_code,
                    "bgn_de": bgn,
                    "end_de": end,
                    "page_count": 50,
                },
            )
            resp.raise_for_status()
            raw = resp.json()
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        # Scrub the API key from error strings before logging/persisting.
        safe = str(exc).replace(DART_API_KEY or "", "***") if DART_API_KEY else str(exc)
        logger.warning("dart fetch failed for %s: %s", ticker, safe)
        await health.record(db_path, source=HEALTH_SOURCE, status="error", details=safe)
        return _empty(pulled_at, error=safe)

    items = _normalize(raw, ticker.upper(), cutoff)
    result: dict[str, Any] = {
        "items": items,
        "pulled_at": pulled_at.isoformat(),
        "source_url": SOURCE_URL,
    }
    await cache.write(
        db_path,
        cache_key=cache_key,
        data_type="dart_filings",
        payload=result,
        pulled_at=pulled_at,
        source_url=SOURCE_URL,
        ticker=ticker.upper(),
    )
    await health.record(
        db_path, source=HEALTH_SOURCE, status="ok", details=f"{ticker}: {len(items)} items"
    )
    return result

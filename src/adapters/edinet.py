"""Japan EDINET (disclosure.edinet-fsa.go.jp) — corporate filings adapter.

EDINET v2 API is free and unauthenticated. The `/documents.json` endpoint is
**date-bucketed**: you query one calendar date and get every disclosure for
that day across all listed companies. We iterate `days_back` days, filter to
the requested ticker, and assemble. Per-day responses are cached for 6 hours.

Tickers come in the yfinance form `7203.T`; the EDINET `secCode` field is a
5-digit code where the trailing 0 is appended to the standard 4-digit symbol
(e.g. 72030 = Toyota). We strip `.T` and append `0` for matching.
"""

import logging
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from src.db import cache, health

logger = logging.getLogger(__name__)


SOURCE_URL = "https://disclosure.edinet-fsa.go.jp"
API_DOCUMENTS = "https://disclosure.edinet-fsa.go.jp/api/v2/documents.json"
DOC_VIEW = "https://disclosure.edinet-fsa.go.jp/api/v2/documents/{doc_id}"
HEALTH_SOURCE = "edinet"

# Cap how many days we walk per call so a single /catalyst doesn't fan out into
# 30 sequential network requests on a cold cache.
_MAX_DAYS_PER_CALL = 14

_TICKER_RE = re.compile(r"^(\d{4})\.T$", re.IGNORECASE)


def _empty(pulled_at: datetime, error: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "items": [],
        "pulled_at": pulled_at.isoformat(),
        "source_url": SOURCE_URL,
    }
    if error:
        payload["error"] = error
    return payload


def _ticker_to_seccode(ticker: str) -> str | None:
    m = _TICKER_RE.match(ticker.strip())
    return f"{m.group(1)}0" if m else None


def _normalize_day(raw: dict[str, Any], sec_code: str, ticker: str) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        return []
    rows = raw.get("results") or []
    items: list[dict[str, Any]] = []
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        # Match in either zero-padded form to handle leading-zero quirks.
        raw_sec = str(entry.get("secCode") or "")
        if raw_sec.lstrip("0").zfill(5) != sec_code.lstrip("0").zfill(5) and raw_sec != sec_code:
            continue
        sub_date = entry.get("submitDateTime") or entry.get("filerDate") or ""
        try:
            filing_date = datetime.fromisoformat(sub_date.replace("Z", "+00:00")).date()
        except (TypeError, ValueError):
            try:
                filing_date = date.fromisoformat(sub_date[:10])
            except (TypeError, ValueError):
                continue
        doc_id = entry.get("docID")
        url = DOC_VIEW.format(doc_id=doc_id) if doc_id else SOURCE_URL
        items.append(
            {
                "ticker": ticker,
                "form": entry.get("docTypeCode") or entry.get("ordinanceCode") or "filing",
                "filing_date": filing_date.isoformat(),
                "event_date": filing_date.isoformat(),
                "description": (
                    entry.get("docDescription") or entry.get("filerName") or "EDINET filing"
                ),
                "source_url": url,
            }
        )
    return items


async def fetch_disclosures(
    db_path: Path,
    ticker: str,
    days_back: int = 14,
) -> dict[str, Any]:
    """Tool-shape return for one Japanese ticker over `days_back` days."""
    pulled_at = datetime.now(UTC)
    sec_code = _ticker_to_seccode(ticker)
    if sec_code is None:
        return _empty(pulled_at, error=f"invalid JP ticker shape: {ticker!r}")

    cache_key = f"edinet_filings:{ticker.upper()}:{days_back}"
    cached = await cache.read_if_fresh(db_path, cache_key, cache.TTL_SECONDS["edinet_filings"])
    if cached is not None:
        return cached

    walk = min(days_back, _MAX_DAYS_PER_CALL)
    today = pulled_at.date()
    items: list[dict[str, Any]] = []
    error: str | None = None

    try:
        async with httpx.AsyncClient(
            timeout=30.0,
            headers={"User-Agent": "trading-bot (contact via SEC_EDGAR_UA)"},
        ) as client:
            for offset in range(walk + 1):
                day = today - timedelta(days=offset)
                resp = await client.get(
                    API_DOCUMENTS,
                    params={"date": day.isoformat(), "type": 2},
                )
                if resp.status_code == 404:
                    continue
                resp.raise_for_status()
                items.extend(_normalize_day(resp.json(), sec_code, ticker.upper()))
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("edinet fetch failed for %s: %s", ticker, exc)
        error = str(exc)
        await health.record(db_path, source=HEALTH_SOURCE, status="error", details=error)
        return _empty(pulled_at, error=error)

    items.sort(key=lambda x: x["filing_date"], reverse=True)
    result: dict[str, Any] = {
        "items": items,
        "pulled_at": pulled_at.isoformat(),
        "source_url": SOURCE_URL,
    }
    await cache.write(
        db_path,
        cache_key=cache_key,
        data_type="edinet_filings",
        payload=result,
        pulled_at=pulled_at,
        source_url=SOURCE_URL,
        ticker=ticker.upper(),
    )
    await health.record(
        db_path, source=HEALTH_SOURCE, status="ok", details=f"{ticker}: {len(items)} items"
    )
    return result

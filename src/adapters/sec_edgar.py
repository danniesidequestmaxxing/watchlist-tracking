"""SEC EDGAR adapter — recent filings via the data.sec.gov submissions API.

EDGAR requires a descriptive User-Agent on every request (`SEC_EDGAR_UA`).
Ticker→CIK lookup uses `company_tickers.json` and is cached for 24h to keep
request volume low.
"""

import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from src.config import SEC_EDGAR_UA
from src.db import cache, health

logger = logging.getLogger(__name__)


SOURCE_URL = "https://www.sec.gov"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
HEALTH_SOURCE = "sec-edgar"

TICKERS_CACHE_KEY = "sec:tickers"
TICKERS_TTL = 24 * 60 * 60


def _empty(pulled_at: datetime, error: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "items": [],
        "pulled_at": pulled_at.isoformat(),
        "source_url": SOURCE_URL,
    }
    if error:
        payload["error"] = error
    return payload


def _request_headers() -> dict[str, str]:
    return {"User-Agent": SEC_EDGAR_UA, "Accept": "application/json"}


async def _resolve_cik(db_path: Path, ticker: str, client: httpx.AsyncClient) -> int | None:
    """Return the CIK integer for `ticker`, or None if unknown.

    The full ticker→CIK map is cached under data_type=sec_filings (1h TTL is
    too aggressive but using a longer custom TTL keeps the cache layer happy
    by reusing an existing data_type).
    """
    cached = await cache.read_if_fresh(db_path, TICKERS_CACHE_KEY, TICKERS_TTL)
    mapping: dict[str, int] | None = None
    if cached is not None:
        mapping = {k: int(v) for k, v in cached.get("map", {}).items() if isinstance(v, int)}
        if not mapping:
            mapping = None

    if mapping is None:
        resp = await client.get(TICKERS_URL)
        resp.raise_for_status()
        raw = resp.json()
        mapping = {}
        if isinstance(raw, dict):
            for entry in raw.values():
                if not isinstance(entry, dict):
                    continue
                sym = (entry.get("ticker") or "").upper()
                cik_val = entry.get("cik_str")
                if sym and isinstance(cik_val, int):
                    mapping[sym] = cik_val
        await cache.write(
            db_path,
            cache_key=TICKERS_CACHE_KEY,
            data_type="sec_filings",
            payload={"map": mapping},
            pulled_at=datetime.now(UTC),
            source_url=TICKERS_URL,
        )

    return mapping.get(ticker.upper())


def _normalize_filings(
    raw: dict[str, Any],
    cik: int,
    ticker: str,
    form_types: list[str],
    cutoff: date,
) -> list[dict[str, Any]]:
    recent = (raw.get("filings") or {}).get("recent") or {}
    accessions = recent.get("accessionNumber") or []
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    primary_docs = recent.get("primaryDocument") or []

    items: list[dict[str, Any]] = []
    requested = {f.upper() for f in form_types}
    for acc, form, fd, doc in zip(accessions, forms, dates, primary_docs, strict=False):
        if not isinstance(form, str) or form.upper() not in requested:
            continue
        try:
            filing_date = date.fromisoformat(fd)
        except (TypeError, ValueError):
            continue
        if filing_date < cutoff:
            continue
        acc_clean = acc.replace("-", "") if isinstance(acc, str) else ""
        url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_clean}/{doc}"
            if acc_clean and doc
            else f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
        )
        items.append(
            {
                "ticker": ticker,
                "form": form,
                "filing_date": filing_date.isoformat(),
                "event_date": filing_date.isoformat(),
                "accession": acc,
                "description": f"{ticker} {form}",
                "source_url": url,
            }
        )
    items.sort(key=lambda x: x["filing_date"], reverse=True)
    return items


async def read_sec_filings(
    db_path: Path,
    ticker: str,
    form_types: list[str] | None = None,
    days_back: int = 30,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Tool dispatcher for `read_sec_filings`. Returns {items, pulled_at, source_url}.

    `force_refresh` bypasses the 1h cache; the EDGAR push poller uses this to
    keep its 10-minute detection latency.
    """
    sym = ticker.upper()
    forms = list(form_types) if form_types else ["8-K", "10-Q"]
    cache_key = f"sec_filings:{sym}:{','.join(sorted(forms))}:{days_back}"

    if not force_refresh:
        cached = await cache.read_if_fresh(db_path, cache_key, cache.TTL_SECONDS["sec_filings"])
        if cached is not None:
            return cached

    pulled_at = datetime.now(UTC)
    cutoff = pulled_at.date() - timedelta(days=days_back)

    try:
        async with httpx.AsyncClient(timeout=30.0, headers=_request_headers()) as client:
            cik = await _resolve_cik(db_path, sym, client)
            if cik is None:
                return _empty(pulled_at, error=f"no CIK found for {sym}")
            resp = await client.get(SUBMISSIONS_URL.format(cik=cik))
            resp.raise_for_status()
            raw = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("sec_edgar fetch failed for %s: %s", sym, exc)
        await health.record(db_path, source=HEALTH_SOURCE, status="error", details=str(exc))
        return _empty(pulled_at, error=str(exc))

    items = _normalize_filings(raw, cik, sym, forms, cutoff)
    result: dict[str, Any] = {
        "items": items,
        "pulled_at": pulled_at.isoformat(),
        "source_url": SOURCE_URL,
    }

    await cache.write(
        db_path,
        cache_key=cache_key,
        data_type="sec_filings",
        payload=result,
        pulled_at=pulled_at,
        source_url=SOURCE_URL,
        ticker=sym,
    )
    await health.record(
        db_path,
        source=HEALTH_SOURCE,
        status="ok",
        details=f"{sym}: {len(items)} filings ({','.join(forms)})",
    )
    return result

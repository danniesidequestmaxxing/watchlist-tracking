"""Token Unlocks adapter — Phase 5 catalyst tool source for crypto unlocks."""

import logging
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from src.config import TOKEN_UNLOCKS_API_KEY
from src.db import cache, health

logger = logging.getLogger(__name__)


SOURCE_URL = "https://token.unlocks.app"
API_BASE = "https://api.token.unlocks.app/v1"
HEALTH_SOURCE = "token-unlocks"

# Symbols are interpolated into the URL path so we restrict to a safe charset
# even though only the catalyst agent calls this with grounded ticker values.
_SYMBOL_RE = re.compile(r"^[A-Z0-9_-]{1,20}$")


def _empty(pulled_at: datetime, error: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "items": [],
        "pulled_at": pulled_at.isoformat(),
        "source_url": SOURCE_URL,
    }
    if error:
        payload["error"] = error
    return payload


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        if "T" in raw:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        return date.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def _normalize(raw: Any, symbol: str, cutoff: date, today: date) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        items_raw: list[Any] = raw
    elif isinstance(raw, dict):
        items_raw = raw.get("data") or raw.get("items") or raw.get("unlocks") or []
    else:
        return []
    items: list[dict[str, Any]] = []
    for entry in items_raw:
        if not isinstance(entry, dict):
            continue
        unlock_date = _parse_date(entry.get("date") or entry.get("unlock_date"))
        if unlock_date is None or unlock_date < today or unlock_date > cutoff:
            continue
        items.append(
            {
                "symbol": symbol,
                "unlock_date": unlock_date.isoformat(),
                "description": entry.get("description")
                or entry.get("name")
                or f"{symbol} token unlock",
                "amount_pct": entry.get("amount_pct"),
                "amount_usd": entry.get("amount_usd"),
                "source_url": entry.get("source_url") or SOURCE_URL,
            }
        )
    return items


async def get_token_unlocks(
    db_path: Path,
    symbol: str,
    days_ahead: int = 30,
) -> dict[str, Any]:
    """Tool dispatcher for `get_token_unlocks`.

    Returns a dict shaped {"items": [...], "pulled_at": iso, "source_url": str}.
    Errors return {"items": [], "error": "..."} — never raises into the LLM
    loop per spec §5.4.
    """
    sym = symbol.upper().strip()
    pulled_at = datetime.now(UTC)
    if not _SYMBOL_RE.match(sym):
        return _empty(pulled_at, error=f"invalid symbol shape: {sym!r}")

    cache_key = f"token_unlocks:{sym}:{days_ahead}"
    cached = await cache.read_if_fresh(db_path, cache_key, cache.TTL_SECONDS["token_unlocks"])
    if cached is not None:
        return cached

    cutoff = pulled_at.date() + timedelta(days=days_ahead)

    if not TOKEN_UNLOCKS_API_KEY:
        return _empty(pulled_at, error="TOKEN_UNLOCKS_API_KEY not configured")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{API_BASE}/projects/{sym}/unlocks",
                headers={
                    "X-API-KEY": TOKEN_UNLOCKS_API_KEY,
                    "Accept": "application/json",
                },
                params={"days_ahead": days_ahead},
            )
            resp.raise_for_status()
            raw = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("token_unlocks fetch failed for %s: %s", sym, exc)
        await health.record(db_path, source=HEALTH_SOURCE, status="error", details=str(exc))
        return _empty(pulled_at, error=str(exc))

    items = _normalize(raw, sym, cutoff, pulled_at.date())
    result: dict[str, Any] = {
        "items": items,
        "pulled_at": pulled_at.isoformat(),
        "source_url": SOURCE_URL,
    }

    await cache.write(
        db_path,
        cache_key=cache_key,
        data_type="token_unlocks",
        payload=result,
        pulled_at=pulled_at,
        source_url=SOURCE_URL,
        ticker=sym,
    )
    await health.record(
        db_path, source=HEALTH_SOURCE, status="ok", details=f"{sym}: {len(items)} items"
    )
    return result

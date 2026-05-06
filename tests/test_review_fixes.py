"""Regression tests for the security/quality review fixes."""

import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import aiosqlite
import httpx
import pytest

from src.adapters import sec_edgar, token_unlocks, trading_economics
from src.adapters.token_unlocks import _SYMBOL_RE, _normalize
from src.adapters.trading_economics import _redact
from src.db import catalysts, health
from src.db.schema import init_db
from src.db.watchlist import WatchlistEntry, add_entry, count_entries, list_entries
from src.scheduler import jobs as jobs_mod
from src.telegram.formatters import format_watchlist


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "review_test.db"
    await init_db(p)
    return p


# ---------------------------------------------------------------------------
# H4 — catalyst event dedup
# ---------------------------------------------------------------------------


class _Event:
    def __init__(self, t, d):
        from datetime import datetime as _dt

        self.event_type = t
        self.event_date = d
        self.description = "x"
        self.source_url = "https://example.com/"
        self.source_pulled_at = _dt.now(UTC)


@pytest.mark.asyncio
async def test_catalyst_save_events_dedupes_on_repeat(db_path: Path) -> None:
    e = _Event("earnings", date(2026, 6, 1))
    n1 = await catalysts.save_events(db_path, "NVDA", [e], [], [])
    n2 = await catalysts.save_events(db_path, "NVDA", [e], [], [])
    assert n1 == 1
    assert n2 == 0  # second insert is ignored
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM catalyst_events")
        (count,) = await cursor.fetchone()
    assert count == 1


@pytest.mark.asyncio
async def test_catalyst_save_events_distinct_tier_keeps_both(db_path: Path) -> None:
    """Same date but different confidence tier is a different row."""
    e = _Event("earnings", date(2026, 6, 1))
    n1 = await catalysts.save_events(db_path, "NVDA", [e], [], [])
    n2 = await catalysts.save_events(db_path, "NVDA", [], [e], [])
    assert n1 == 1
    assert n2 == 1


# ---------------------------------------------------------------------------
# H5 — format_watchlist treats expired mutes as not muted
# ---------------------------------------------------------------------------


def _entry(**overrides) -> WatchlistEntry:
    base = {
        "id": 1,
        "user_id": 1,
        "ticker": "BTCUSDT",
        "asset_class": "crypto",
        "exchange": "binance",
        "added_at": "2026-01-01 00:00:00",
        "ta_enabled": True,
        "catalyst_enabled": True,
    }
    base.update(overrides)
    return WatchlistEntry.model_validate(base)


def test_format_watchlist_expired_mute_renders_green() -> None:
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    text = format_watchlist([_entry(muted_until=past)])
    assert "🟢" in text
    assert "muted" not in text


def test_format_watchlist_active_mute_renders_red() -> None:
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    text = format_watchlist([_entry(muted_until=future)])
    assert "🔴" in text
    assert "muted" in text


# ---------------------------------------------------------------------------
# M1 — NULL exchange duplicates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_entry_rejects_duplicate_when_no_exchange(db_path: Path) -> None:
    n1 = await add_entry(db_path, 1, "NVDA", "equity_us")
    n2 = await add_entry(db_path, 1, "NVDA", "equity_us")
    assert n1 is not None
    assert n2 is None  # caught by UNIQUE because exchange is "" not NULL
    assert await count_entries(db_path, 1) == 1


@pytest.mark.asyncio
async def test_add_entry_distinct_exchange_keeps_both(db_path: Path) -> None:
    n1 = await add_entry(db_path, 1, "BTCUSDT", "crypto", "binance")
    n2 = await add_entry(db_path, 1, "BTCUSDT", "crypto", "okx")
    assert n1 is not None
    assert n2 is not None
    entries = await list_entries(db_path, 1)
    assert {e.exchange for e in entries} == {"binance", "okx"}


# ---------------------------------------------------------------------------
# M2 — Token Unlocks symbol shape
# ---------------------------------------------------------------------------


def test_symbol_regex_accepts_normal() -> None:
    assert _SYMBOL_RE.match("SOL")
    assert _SYMBOL_RE.match("WIF-USD")
    assert _SYMBOL_RE.match("HYPE_2")


def test_symbol_regex_rejects_path_traversal() -> None:
    assert not _SYMBOL_RE.match("../admin")
    assert not _SYMBOL_RE.match("SOL/abc")
    assert not _SYMBOL_RE.match("SOL?attack=1")
    assert not _SYMBOL_RE.match("")
    assert not _SYMBOL_RE.match("A" * 21)


@pytest.mark.asyncio
async def test_token_unlocks_rejects_bad_symbol(db_path: Path) -> None:
    out = await token_unlocks.get_token_unlocks(db_path, "../admin", days_ahead=30)
    assert out["items"] == []
    assert "invalid symbol" in out["error"]


# ---------------------------------------------------------------------------
# M3 — Token Unlocks list root tolerated
# ---------------------------------------------------------------------------


def test_normalize_handles_list_root() -> None:
    today = date(2026, 5, 6)
    cutoff = today + timedelta(days=30)
    raw = [{"unlock_date": "2026-05-10"}]  # list at the root, not dict
    out = _normalize(raw, "SOL", cutoff, today)
    assert len(out) == 1
    assert out[0]["unlock_date"] == "2026-05-10"


def test_normalize_handles_garbage_root() -> None:
    assert _normalize("garbage", "SOL", date(2026, 5, 6), date(2026, 5, 6)) == []
    assert _normalize(None, "SOL", date(2026, 5, 6), date(2026, 5, 6)) == []


# ---------------------------------------------------------------------------
# M4 — EDGAR watermark cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edgar_watermark_capped_at_200(db_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(jobs_mod, "DB_PATH", db_path)
    accessions = {f"A-{i:05d}" for i in range(500)}
    await jobs_mod._set_seen_accessions(db_path, "NVDA", accessions)
    seen = await jobs_mod._get_seen_accessions(db_path, "NVDA")
    assert len(seen) == jobs_mod.EDGAR_WATERMARK_CAP


# ---------------------------------------------------------------------------
# M5 — health_log prune
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prune_health_log_removes_old(db_path: Path) -> None:
    await health.record(db_path, "ccxt:binance", "ok", "fresh")
    # Backdate one row
    old_ts = (datetime.now(UTC) - timedelta(days=30)).isoformat(sep=" ", timespec="seconds")
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO health_log (source, status, details, recorded_at) VALUES (?, ?, ?, ?)",
            ("yfinance", "ok", "old", old_ts),
        )
        await db.commit()
    deleted = await health.prune_older_than(db_path, days=14)
    assert deleted == 1


@pytest.mark.asyncio
async def test_prune_no_op_for_zero_days(db_path: Path) -> None:
    assert await health.prune_older_than(db_path, days=0) == 0


# ---------------------------------------------------------------------------
# H1 — Finnhub token in header (not URL)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finnhub_token_in_header_not_url(db_path: Path, monkeypatch) -> None:
    from src.adapters import finnhub

    monkeypatch.setattr(finnhub, "FINNHUB_API_KEY", "secret-key")

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["header"] = request.headers.get("X-Finnhub-Token")
        return httpx.Response(200, json={"earningsCalendar": []})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "src.adapters.finnhub.httpx.AsyncClient",
        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )

    await finnhub.get_earnings_calendar(db_path, "NVDA", days_ahead=30)
    assert "secret-key" not in captured["url"]
    assert captured["header"] == "secret-key"


# ---------------------------------------------------------------------------
# H2 — Trading Economics secret redacted from error strings
# ---------------------------------------------------------------------------


def test_redact_strips_secret() -> None:
    assert _redact("oops https://api.te.com/calendar?c=SECRET-KEY", "SECRET-KEY") == (
        "oops https://api.te.com/calendar?c=***"
    )


def test_redact_no_secret_no_change() -> None:
    assert _redact("ok message", None) == "ok message"
    assert _redact("ok message", "") == "ok message"


@pytest.mark.asyncio
async def test_trading_economics_error_redacts_token(db_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(trading_economics, "TRADING_ECONOMICS_API_KEY", "PAID-TOKEN")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream rate limited")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "src.adapters.trading_economics.httpx.AsyncClient",
        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )

    out = await trading_economics.get_macro_events(db_path, days_ahead=14)
    assert "PAID-TOKEN" not in (out.get("error") or "")

    rows = await health.latest_per_source(db_path)
    te = rows.get("trading-economics")
    assert te is not None
    assert "PAID-TOKEN" not in (te["details"] or "")


# ---------------------------------------------------------------------------
# H3 — read_sec_filings honors force_refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_sec_filings_force_refresh_bypasses_cache(db_path: Path, monkeypatch) -> None:
    today = datetime.now(UTC).date()

    tickers_payload = {
        "0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
    }
    submissions_payload_v1 = {
        "filings": {
            "recent": {
                "accessionNumber": ["A1"],
                "form": ["8-K"],
                "filingDate": [today.isoformat()],
                "primaryDocument": ["a.htm"],
            }
        }
    }
    submissions_payload_v2 = {
        "filings": {
            "recent": {
                "accessionNumber": ["A1", "A2"],
                "form": ["8-K", "8-K"],
                "filingDate": [today.isoformat(), today.isoformat()],
                "primaryDocument": ["a.htm", "b.htm"],
            }
        }
    }
    state = {"version": 1}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "company_tickers.json" in url:
            return httpx.Response(200, json=tickers_payload)
        if "submissions/CIK" in url:
            payload = submissions_payload_v1 if state["version"] == 1 else submissions_payload_v2
            return httpx.Response(200, json=payload)
        return httpx.Response(404)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "src.adapters.sec_edgar.httpx.AsyncClient",
        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )

    out1 = await sec_edgar.read_sec_filings(db_path, "NVDA", form_types=["8-K"])
    assert {it["accession"] for it in out1["items"]} == {"A1"}

    # Without force_refresh, second call hits cache and stays at v1.
    state["version"] = 2
    out2 = await sec_edgar.read_sec_filings(db_path, "NVDA", form_types=["8-K"])
    assert {it["accession"] for it in out2["items"]} == {"A1"}

    # With force_refresh, the new accession appears.
    out3 = await sec_edgar.read_sec_filings(db_path, "NVDA", form_types=["8-K"], force_refresh=True)
    assert {it["accession"] for it in out3["items"]} == {"A1", "A2"}


# ---------------------------------------------------------------------------
# M6 — NaN doesn't slip through validate_ta_snapshot
# ---------------------------------------------------------------------------


def test_validate_ta_snapshot_rejects_nan_volume() -> None:
    from src.adapters.base import OHLCVBundle
    from src.ta.pipeline import compute_snapshot, validate_ta_snapshot

    base_ts = 1_700_000_000_000
    bars = [
        [base_ts + i * 3_600_000, 100.0 + i, 101 + i, 99 + i, 100.5 + i, 1000.0] for i in range(50)
    ]
    bundle = OHLCVBundle(
        ticker="TEST",
        timeframe="1h",
        exchange="binance",
        pulled_at=datetime.now(UTC),
        source_url="https://x",
        bars=bars,
    )
    snap = compute_snapshot(bundle)
    snap.volume = float("nan")
    issues = validate_ta_snapshot(snap)
    assert any("volume" in i for i in issues)


# ---------------------------------------------------------------------------
# Sanity: no Finnhub token literal left anywhere in src
# ---------------------------------------------------------------------------


def test_finnhub_source_no_query_token_literal() -> None:
    """Defensive: the source must not pass `token=` in URL params anymore."""
    src = Path("src/adapters/finnhub.py").read_text()
    # `"token":` appearing means we're back to URL-param token.
    assert not re.search(r"\"token\"\s*:\s*FINNHUB_API_KEY", src)

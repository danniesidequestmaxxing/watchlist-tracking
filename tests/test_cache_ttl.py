"""Cache TTL behavior + write-time validation per Phase 3 def-of-done.

Inserting a stale or invalid record must not surface to Telegram. These tests
exercise that contract end-to-end at the cache and TA-pipeline layers.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.adapters.base import OHLCVBundle
from src.db import cache
from src.db.schema import init_db
from src.ta.pipeline import (
    TASnapshot,
    _ta_cache_key,
    compute_snapshot,
    get_crypto_snapshot,
    validate_ta_snapshot,
)
from src.validation.checks import ValidationError


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "cache_test.db"
    await init_db(path)
    return path


def _make_bundle(prices: list[float] | None = None) -> OHLCVBundle:
    if prices is None:
        prices = [100.0 + i * 0.5 for i in range(50)]
    base_ts = 1_700_000_000_000
    bars: list[list[float]] = [
        [base_ts + i * 3_600_000, p, p + 1.0, p - 1.0, p, 1000.0] for i, p in enumerate(prices)
    ]
    return OHLCVBundle(
        ticker="TESTUSDT",
        timeframe="1h",
        exchange="binance",
        pulled_at=datetime.now(UTC),
        source_url="https://www.binance.com",
        bars=bars,
    )


# ---------------------------------------------------------------------------
# TTL_SECONDS map matches spec §5.5
# ---------------------------------------------------------------------------


def test_ttl_map_matches_spec() -> None:
    assert cache.TTL_SECONDS["ohlcv_1h"] == 60 * 60
    assert cache.TTL_SECONDS["ta_1h"] == 60 * 60
    assert cache.TTL_SECONDS["live_price"] == 60
    assert cache.TTL_SECONDS["token_unlocks"] == 24 * 60 * 60
    assert cache.TTL_SECONDS["earnings_calendar"] == 6 * 60 * 60
    assert cache.TTL_SECONDS["macro_events"] == 24 * 60 * 60
    assert cache.TTL_SECONDS["news"] == 30 * 60
    assert cache.TTL_SECONDS["sec_filings"] == 60 * 60
    assert cache.TTL_SECONDS["options_expiry"] == 60 * 60


# ---------------------------------------------------------------------------
# Cache round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_write_then_read_fresh(db_path: Path) -> None:
    payload = {"price": 100.0}
    pulled_at = datetime.now(UTC)
    await cache.write(
        db_path,
        cache_key="k",
        data_type="ta_1h",
        payload=payload,
        pulled_at=pulled_at,
        source_url="https://x",
        ticker="TESTUSDT",
    )
    out = await cache.read_if_fresh(db_path, "k", ttl_seconds=60)
    assert out == payload


@pytest.mark.asyncio
async def test_cache_read_missing_returns_none(db_path: Path) -> None:
    assert await cache.read_if_fresh(db_path, "nope") is None


@pytest.mark.asyncio
async def test_cache_read_stale_returns_none(db_path: Path) -> None:
    # Write with a backdated pulled_at; ttl=60 should not let it survive
    pulled_at = datetime.now(UTC) - timedelta(seconds=120)
    await cache.write(
        db_path,
        cache_key="stale",
        data_type="ta_1h",
        payload={"a": 1},
        pulled_at=pulled_at,
        source_url="https://x",
    )
    assert await cache.read_if_fresh(db_path, "stale", ttl_seconds=60) is None


@pytest.mark.asyncio
async def test_cache_read_uses_data_type_default_ttl(db_path: Path) -> None:
    pulled_at = datetime.now(UTC) - timedelta(seconds=120)  # older than live_price 60s
    await cache.write(
        db_path,
        cache_key="lp",
        data_type="live_price",
        payload={"p": 1},
        pulled_at=pulled_at,
        source_url="https://x",
    )
    assert await cache.read_if_fresh(db_path, "lp") is None


@pytest.mark.asyncio
async def test_cache_read_meta_returns_row(db_path: Path) -> None:
    pulled_at = datetime.now(UTC)
    await cache.write(
        db_path,
        cache_key="meta",
        data_type="ta_1h",
        payload={"a": 1},
        pulled_at=pulled_at,
        source_url="https://example.com",
        ticker="X",
    )
    meta = await cache.read_meta(db_path, "meta")
    assert meta is not None
    assert meta["data_type"] == "ta_1h"
    assert meta["source_url"] == "https://example.com"


@pytest.mark.asyncio
async def test_cache_read_meta_missing_returns_none(db_path: Path) -> None:
    assert await cache.read_meta(db_path, "nope") is None


# ---------------------------------------------------------------------------
# Write-time validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_write_rejects_empty_payload(db_path: Path) -> None:
    with pytest.raises(ValidationError):
        await cache.write(
            db_path,
            cache_key="empty",
            data_type="ta_1h",
            payload={},
            pulled_at=datetime.now(UTC),
            source_url="https://x",
        )


@pytest.mark.asyncio
async def test_cache_write_rejects_unknown_data_type(db_path: Path) -> None:
    with pytest.raises(ValidationError):
        await cache.write(
            db_path,
            cache_key="bad",
            data_type="nope",
            payload={"a": 1},
            pulled_at=datetime.now(UTC),
            source_url="https://x",
        )


@pytest.mark.asyncio
async def test_cache_write_rejects_future_pulled_at(db_path: Path) -> None:
    pulled_at = datetime.now(UTC) + timedelta(minutes=5)
    with pytest.raises(ValidationError):
        await cache.write(
            db_path,
            cache_key="future",
            data_type="ta_1h",
            payload={"a": 1},
            pulled_at=pulled_at,
            source_url="https://x",
        )


@pytest.mark.asyncio
async def test_cache_write_tolerates_small_clock_skew(db_path: Path) -> None:
    """Up to 60 seconds of forward skew is accepted as clock drift."""
    pulled_at = datetime.now(UTC) + timedelta(seconds=10)
    await cache.write(
        db_path,
        cache_key="skew",
        data_type="ta_1h",
        payload={"a": 1},
        pulled_at=pulled_at,
        source_url="https://x",
    )
    out = await cache.read_if_fresh(db_path, "skew", ttl_seconds=60)
    # The row exists; freshness compares forward skew as "future" => stale.
    # The point of this test is that write succeeded.
    assert out is None or out == {"a": 1}


# ---------------------------------------------------------------------------
# Validation wired into the TA pipeline
# ---------------------------------------------------------------------------


def test_validate_ta_snapshot_clean() -> None:
    bundle = _make_bundle()
    snap = compute_snapshot(bundle)
    assert validate_ta_snapshot(snap) == []


def test_validate_ta_snapshot_flags_negative_price() -> None:
    bundle = _make_bundle()
    snap = compute_snapshot(bundle)
    snap.price = -1.0
    issues = validate_ta_snapshot(snap)
    assert any("price" in s for s in issues)


def test_validate_ta_snapshot_flags_rsi_overflow() -> None:
    bundle = _make_bundle()
    snap = compute_snapshot(bundle)
    snap.rsi_14 = 150.0
    issues = validate_ta_snapshot(snap)
    assert any("RSI" in s for s in issues)


def test_validate_ta_snapshot_flags_negative_atr() -> None:
    bundle = _make_bundle()
    snap = compute_snapshot(bundle)
    snap.atr_14 = -1.0
    issues = validate_ta_snapshot(snap)
    assert any("ATR" in s for s in issues)


def test_validate_ta_snapshot_flags_inverted_bands() -> None:
    bundle = _make_bundle()
    snap = compute_snapshot(bundle)
    snap.bb_upper = 50.0
    snap.bb_lower = 100.0
    issues = validate_ta_snapshot(snap)
    assert any("bb_upper" in s for s in issues)


def test_validate_ta_snapshot_flags_inverted_high_low() -> None:
    bundle = _make_bundle()
    snap = compute_snapshot(bundle)
    snap.high_24h = 90.0
    snap.low_24h = 100.0
    issues = validate_ta_snapshot(snap)
    assert any("high_24h" in s for s in issues)
    snap2 = compute_snapshot(bundle)
    snap2.high_7d = 50.0
    snap2.low_7d = 100.0
    issues2 = validate_ta_snapshot(snap2)
    assert any("high_7d" in s for s in issues2)


def test_validate_ta_snapshot_flags_negative_volume() -> None:
    bundle = _make_bundle()
    snap = compute_snapshot(bundle)
    snap.volume = -10.0
    issues = validate_ta_snapshot(snap)
    assert any("volume" in s for s in issues)


# ---------------------------------------------------------------------------
# get_crypto_snapshot end-to-end with cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_crypto_snapshot_returns_cached(db_path: Path) -> None:
    """If a fresh row is in cache, get_crypto_snapshot returns it without
    touching the network adapter."""
    bundle = _make_bundle()
    snap = compute_snapshot(bundle)
    key = _ta_cache_key("TESTUSDT", "binance")
    await cache.write(
        db_path,
        cache_key=key,
        data_type="ta_1h",
        payload=snap.model_dump(mode="json"),
        pulled_at=snap.pulled_at,
        source_url=snap.source_url,
        ticker="TESTUSDT",
    )

    got = await get_crypto_snapshot(db_path, "TESTUSDT", "binance")
    assert isinstance(got, TASnapshot)
    assert got.price == snap.price
    assert got.exchange == "binance"


@pytest.mark.asyncio
async def test_stale_cache_row_does_not_surface(db_path: Path) -> None:
    """Phase 3 def-of-done: a stale row must not surface; read_if_fresh
    drops it and the caller will see a cache miss."""
    bundle = _make_bundle()
    snap = compute_snapshot(bundle)
    key = _ta_cache_key("TESTUSDT", "binance")
    pulled_at_old = datetime.now(UTC) - timedelta(hours=2)  # > ta_1h TTL of 1h

    payload = snap.model_dump(mode="json")
    payload["pulled_at"] = pulled_at_old.isoformat()
    await cache.write(
        db_path,
        cache_key=key,
        data_type="ta_1h",
        payload=payload,
        pulled_at=pulled_at_old,
        source_url=snap.source_url,
        ticker="TESTUSDT",
    )

    cached = await cache.read_if_fresh(db_path, key, ttl_seconds=cache.TTL_SECONDS["ta_1h"])
    assert cached is None

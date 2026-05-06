import math
from datetime import UTC, datetime

import pandas as pd
import pytest

from src.adapters.base import OHLCVBundle
from src.ta.indicators import (
    atr,
    bollinger_bands,
    ema,
    macd,
    returns_pct,
    rsi,
    sma,
)
from src.ta.pipeline import compute_snapshot


def _flat_series(value: float, n: int) -> pd.Series:
    return pd.Series([float(value)] * n)


def _ramp_series(start: float, step: float, n: int) -> pd.Series:
    return pd.Series([float(start + step * i) for i in range(n)])


def test_returns_pct_basic() -> None:
    s = pd.Series([100.0, 101.0, 102.0, 110.0])
    assert returns_pct(s, 1) == pytest.approx(110.0 / 102.0 * 100 - 100)
    assert returns_pct(s, 3) == pytest.approx(10.0)
    assert returns_pct(s, 4) is None


def test_returns_pct_zero_base_returns_none() -> None:
    s = pd.Series([0.0, 5.0])
    assert returns_pct(s, 1) is None


def test_sma_constant() -> None:
    s = _flat_series(10.0, 25)
    out = sma(s, 20)
    assert out.iloc[-1] == pytest.approx(10.0)
    assert out.iloc[18] != out.iloc[18]  # NaN before window full


def test_sma_known_window() -> None:
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = sma(s, 5)
    assert out.iloc[-1] == pytest.approx(3.0)


def test_ema_constant_equals_value() -> None:
    s = _flat_series(50.0, 100)
    out = ema(s, 20)
    assert out.iloc[-1] == pytest.approx(50.0)


def test_rsi_pure_gains_approaches_100() -> None:
    s = _ramp_series(100.0, 1.0, 60)
    out = rsi(s, 14).iloc[-1]
    assert out > 99.0


def test_rsi_pure_losses_approaches_zero() -> None:
    s = _ramp_series(200.0, -1.0, 60)
    out = rsi(s, 14).iloc[-1]
    assert out < 1.0


def test_macd_flat_is_zero() -> None:
    s = _flat_series(42.0, 60)
    line, sig, hist = macd(s)
    assert line.iloc[-1] == pytest.approx(0.0, abs=1e-9)
    assert sig.iloc[-1] == pytest.approx(0.0, abs=1e-9)
    assert hist.iloc[-1] == pytest.approx(0.0, abs=1e-9)


def test_atr_known_range() -> None:
    high = pd.Series([110.0] * 30)
    low = pd.Series([100.0] * 30)
    close = pd.Series([105.0] * 30)
    out = atr(high, low, close, 14)
    assert out.iloc[-1] == pytest.approx(10.0, rel=1e-6)


def test_bollinger_bands_flat_collapses() -> None:
    s = _flat_series(100.0, 30)
    upper, mid, lower = bollinger_bands(s, 20, 2.0)
    assert upper.iloc[-1] == pytest.approx(100.0)
    assert mid.iloc[-1] == pytest.approx(100.0)
    assert lower.iloc[-1] == pytest.approx(100.0)


def _fixture_bundle(prices: list[float], volumes: list[float] | None = None) -> OHLCVBundle:
    n = len(prices)
    if volumes is None:
        volumes = [100.0] * n
    bars: list[list[float]] = []
    base_ts = 1_700_000_000_000  # 2023-11-14
    for i, (p, v) in enumerate(zip(prices, volumes, strict=True)):
        ts = base_ts + i * 3_600_000  # +1h
        bars.append([ts, p, p + 1.0, p - 1.0, p, v])
    return OHLCVBundle(
        ticker="TESTUSDT",
        timeframe="1h",
        exchange="binance",
        pulled_at=datetime(2023, 11, 14, 12, tzinfo=UTC),
        source_url="https://www.binance.com",
        bars=bars,
    )


def test_compute_snapshot_uptrend() -> None:
    prices = [100.0 + i * 0.5 for i in range(200)]  # monotonic up
    bundle = _fixture_bundle(prices)
    snap = compute_snapshot(bundle)

    assert snap.ticker == "TESTUSDT"
    assert snap.exchange == "binance"
    assert snap.bars_used == 200
    assert snap.price == pytest.approx(prices[-1])
    assert snap.return_1h == pytest.approx(0.5 / prices[-2] * 100, rel=1e-3)
    assert snap.return_24h is not None and snap.return_24h > 0
    assert snap.return_7d is not None and snap.return_7d > 0
    assert snap.above_ema_20 is True
    assert snap.above_ema_50 is True
    assert snap.above_sma_200 is True
    assert snap.rsi_14 is not None and snap.rsi_14 > 99
    assert snap.rsi_overbought is True
    assert snap.macd_line is not None and snap.macd_line > 0
    assert snap.macd_hist_rising in (True, False)
    assert snap.bb_position in {"above-upper", "upper-mid"}
    assert snap.atr_14 is not None and snap.atr_14 > 0


def test_compute_snapshot_volume_spike_flag() -> None:
    """Volume SMA(20) is computed over the 20 preceding bars (exclusive of the
    current bar) so a true spike isn't diluted by itself."""
    n = 60
    prices = [100.0] * n
    volumes = [10.0] * (n - 1) + [50.0]  # last bar is 5x the prior baseline
    bundle = _fixture_bundle(prices, volumes)
    snap = compute_snapshot(bundle)
    assert snap.volume_sma_20 == pytest.approx(10.0)
    assert snap.volume_ratio == pytest.approx(5.0, rel=1e-6)
    assert snap.volume_spike is True


def test_compute_snapshot_short_history_partial_indicators() -> None:
    prices = [100.0 + i for i in range(30)]  # only 30 bars
    bundle = _fixture_bundle(prices)
    snap = compute_snapshot(bundle)
    assert snap.bars_used == 30
    assert snap.ema_20 is not None
    assert snap.ema_50 is None  # not enough bars
    assert snap.sma_200 is None
    assert snap.return_7d is None  # not enough bars
    assert snap.return_24h is not None


def test_compute_snapshot_empty_raises() -> None:
    bundle = OHLCVBundle(
        ticker="X",
        timeframe="1h",
        exchange="binance",
        pulled_at=datetime.now(UTC),
        source_url="https://example.com",
        bars=[],
    )
    with pytest.raises(ValueError):
        compute_snapshot(bundle)


def test_compute_snapshot_macd_post_reversal_bullish() -> None:
    """After a sustained reversal up, MACD line should sit above its signal."""
    prices = [100.0 - 0.5 * i for i in range(80)]
    prices += [prices[-1] + 1.0 * i for i in range(1, 21)]
    bundle = _fixture_bundle(prices)
    snap = compute_snapshot(bundle)
    assert snap.macd_line is not None and snap.macd_signal is not None
    assert snap.macd_line > snap.macd_signal
    assert math.isfinite(snap.macd_line)

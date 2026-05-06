import logging
from datetime import datetime
from pathlib import Path

import pandas as pd
from pydantic import BaseModel

from src.adapters.base import OHLCVBundle
from src.adapters.crypto_ccxt import CryptoCCXTAdapter
from src.db import cache
from src.ta.indicators import atr, bollinger_bands, ema, macd, returns_pct, rsi, sma

logger = logging.getLogger(__name__)


BARS_1H = 1
BARS_24H = 24
BARS_7D = 24 * 7


class TASnapshot(BaseModel):
    ticker: str
    timeframe: str
    exchange: str | None
    pulled_at: datetime
    source_url: str

    price: float
    return_1h: float | None = None
    return_24h: float | None = None
    return_7d: float | None = None

    volume: float
    volume_sma_20: float | None = None
    volume_ratio: float | None = None
    volume_spike: bool = False

    ema_20: float | None = None
    ema_50: float | None = None
    sma_200: float | None = None
    above_ema_20: bool | None = None
    above_ema_50: bool | None = None
    above_sma_200: bool | None = None

    rsi_14: float | None = None
    rsi_overbought: bool = False
    rsi_oversold: bool = False

    macd_line: float | None = None
    macd_signal: float | None = None
    macd_hist: float | None = None
    macd_hist_rising: bool | None = None
    macd_bullish_cross: bool = False
    macd_bearish_cross: bool = False

    atr_14: float | None = None

    bb_upper: float | None = None
    bb_middle: float | None = None
    bb_lower: float | None = None
    bb_position: str = "unknown"  # below-lower | lower-mid | upper-mid | above-upper

    high_24h: float
    low_24h: float
    high_7d: float
    low_7d: float

    bars_used: int


def _safe_float(value: float | None) -> float | None:
    if value is None:
        return None
    if pd.isna(value):
        return None
    return float(value)


def _bb_position(price: float, upper: float | None, mid: float | None, lower: float | None) -> str:
    if upper is None or mid is None or lower is None:
        return "unknown"
    if price > upper:
        return "above-upper"
    if price >= mid:
        return "upper-mid"
    if price >= lower:
        return "lower-mid"
    return "below-lower"


def compute_snapshot(bundle: OHLCVBundle) -> TASnapshot:
    """Compute TASnapshot from an OHLCVBundle.

    Indicators that require more bars than provided return None; the snapshot
    still ships so the caller has at least price + range data.
    """
    if not bundle.bars:
        raise ValueError("OHLCV bundle has no bars")

    df = bundle.to_dataframe()
    closes = df["close"]
    highs = df["high"]
    lows = df["low"]
    volumes = df["volume"]
    n = len(df)

    last_close = float(closes.iloc[-1])
    last_volume = float(volumes.iloc[-1])

    # Compare current volume to the SMA of the 20 preceding bars (exclusive),
    # so a real spike doesn't get diluted by including itself in the average.
    vol_sma20 = sma(volumes.shift(1), 20)
    vol_sma_last = _safe_float(vol_sma20.iloc[-1])
    vol_ratio = (last_volume / vol_sma_last) if vol_sma_last and vol_sma_last > 0 else None

    ema20 = _safe_float(ema(closes, 20).iloc[-1]) if n >= 20 else None
    ema50 = _safe_float(ema(closes, 50).iloc[-1]) if n >= 50 else None
    sma200 = _safe_float(sma(closes, 200).iloc[-1]) if n >= 200 else None

    rsi_last = _safe_float(rsi(closes, 14).iloc[-1]) if n >= 15 else None

    if n >= 35:
        macd_l, macd_s, macd_h = macd(closes)
        macd_line = _safe_float(macd_l.iloc[-1])
        macd_signal_v = _safe_float(macd_s.iloc[-1])
        macd_hist = _safe_float(macd_h.iloc[-1])
        prev_hist = _safe_float(macd_h.iloc[-2])
        hist_rising = (
            (macd_hist > prev_hist) if (macd_hist is not None and prev_hist is not None) else None
        )
        prev_line = _safe_float(macd_l.iloc[-2])
        prev_sig = _safe_float(macd_s.iloc[-2])
        if (
            macd_line is not None
            and macd_signal_v is not None
            and prev_line is not None
            and prev_sig is not None
        ):
            bullish_cross = prev_line <= prev_sig and macd_line > macd_signal_v
            bearish_cross = prev_line >= prev_sig and macd_line < macd_signal_v
        else:
            bullish_cross = bearish_cross = False
    else:
        macd_line = macd_signal_v = macd_hist = None
        hist_rising = None
        bullish_cross = bearish_cross = False

    atr_last = _safe_float(atr(highs, lows, closes, 14).iloc[-1]) if n >= 15 else None

    if n >= 20:
        bb_u, bb_m, bb_l = bollinger_bands(closes, 20, 2.0)
        bb_upper = _safe_float(bb_u.iloc[-1])
        bb_middle = _safe_float(bb_m.iloc[-1])
        bb_lower = _safe_float(bb_l.iloc[-1])
    else:
        bb_upper = bb_middle = bb_lower = None

    h24 = float(highs.iloc[-min(BARS_24H, n) :].max())
    l24 = float(lows.iloc[-min(BARS_24H, n) :].min())
    h7d = float(highs.iloc[-min(BARS_7D, n) :].max())
    l7d = float(lows.iloc[-min(BARS_7D, n) :].min())

    return TASnapshot(
        ticker=bundle.ticker,
        timeframe=bundle.timeframe,
        exchange=bundle.exchange,
        pulled_at=bundle.pulled_at,
        source_url=bundle.source_url,
        price=last_close,
        return_1h=returns_pct(closes, BARS_1H),
        return_24h=returns_pct(closes, BARS_24H),
        return_7d=returns_pct(closes, BARS_7D),
        volume=last_volume,
        volume_sma_20=vol_sma_last,
        volume_ratio=vol_ratio,
        volume_spike=bool(vol_ratio is not None and vol_ratio > 2.0),
        ema_20=ema20,
        ema_50=ema50,
        sma_200=sma200,
        above_ema_20=(last_close > ema20) if ema20 is not None else None,
        above_ema_50=(last_close > ema50) if ema50 is not None else None,
        above_sma_200=(last_close > sma200) if sma200 is not None else None,
        rsi_14=rsi_last,
        rsi_overbought=bool(rsi_last is not None and rsi_last > 70),
        rsi_oversold=bool(rsi_last is not None and rsi_last < 30),
        macd_line=macd_line,
        macd_signal=macd_signal_v,
        macd_hist=macd_hist,
        macd_hist_rising=hist_rising,
        macd_bullish_cross=bullish_cross,
        macd_bearish_cross=bearish_cross,
        atr_14=atr_last,
        bb_upper=bb_upper,
        bb_middle=bb_middle,
        bb_lower=bb_lower,
        bb_position=_bb_position(last_close, bb_upper, bb_middle, bb_lower),
        high_24h=h24,
        low_24h=l24,
        high_7d=h7d,
        low_7d=l7d,
        bars_used=n,
    )


def _ta_cache_key(ticker: str, exchange: str | None) -> str:
    return f"ta_1h:{ticker.upper()}:{(exchange or '-').lower()}"


async def get_crypto_snapshot(
    db_path: Path,
    ticker: str,
    exchange: str,
    *,
    force_refresh: bool = False,
) -> TASnapshot:
    """Return a TASnapshot for a crypto ticker, using the cache when fresh."""
    cache_key = _ta_cache_key(ticker, exchange)
    if not force_refresh:
        cached = await cache.read_if_fresh(db_path, cache_key, cache.TTL_SECONDS["ta_1h"])
        if cached is not None:
            logger.info("Cache hit: %s", cache_key)
            return TASnapshot.model_validate(cached)

    adapter = CryptoCCXTAdapter()
    try:
        bundle = await adapter.fetch_ohlcv(ticker, timeframe="1h", limit=200, exchange=exchange)
    finally:
        await adapter.close()

    snapshot = compute_snapshot(bundle)
    await cache.write(
        db_path,
        cache_key=cache_key,
        data_type="ta_1h",
        payload=snapshot.model_dump(mode="json"),
        pulled_at=snapshot.pulled_at,
        source_url=snapshot.source_url,
        ticker=ticker.upper(),
    )
    return snapshot

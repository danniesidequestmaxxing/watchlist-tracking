"""Technical indicator implementations in pandas.

Manual implementations per the Phase 2 fallback path; pandas-ta is unavailable
on Python 3.11 (0.3.x not on PyPI for 3.11, 0.4.x requires 3.12).
"""

import numpy as np
import pandas as pd


def returns_pct(closes: pd.Series, periods: int) -> float | None:
    """Percent return between the last close and the close `periods` bars back."""
    if len(closes) <= periods:
        return None
    last = closes.iloc[-1]
    prev = closes.iloc[-1 - periods]
    if prev == 0:
        return None
    return float((last - prev) / prev * 100.0)


def sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(window=length, min_periods=length).mean()


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    """Wilder's RSI using an exponential smoothing of gains and losses.

    When `avg_loss` is zero the rs term goes to +inf and the formula naturally
    yields 100. When both `avg_gain` and `avg_loss` are zero rs is NaN and the
    result is NaN.
    """
    delta = series.diff()
    gains = delta.clip(lower=0.0)
    losses = -delta.clip(upper=0.0)
    avg_gain = gains.ewm(alpha=1.0 / length, adjust=False).mean()
    avg_loss = losses.ewm(alpha=1.0 / length, adjust=False).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Return (macd_line, signal_line, histogram)."""
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    length: int = 14,
) -> pd.Series:
    """Average True Range using Wilder's smoothing."""
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / length, adjust=False).mean()


def bollinger_bands(
    close: pd.Series,
    length: int = 20,
    num_std: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Return (upper, middle, lower) Bollinger bands."""
    mid = sma(close, length)
    std = close.rolling(window=length, min_periods=length).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower

import asyncio
import logging
from datetime import UTC, datetime

import pandas as pd
import yfinance as yf

from src.adapters.base import DataAdapter, OHLCVBundle

logger = logging.getLogger(__name__)


YF_INTERVAL_MAP: dict[str, str] = {
    "1h": "1h",
    "1d": "1d",
}

# Period strings yfinance accepts. We pull ~2 months of hourly bars to fill
# the 200-bar window the TA pipeline expects (~31 trading days × 7 hours).
YF_PERIOD_MAP: dict[str, str] = {
    "1h": "60d",
    "1d": "2y",
}


def _df_to_bars(df: pd.DataFrame) -> list[list[float]]:
    bars: list[list[float]] = []
    for ts, row in df.iterrows():
        if pd.isna(row["Close"]):
            continue
        ts_ms = int(ts.timestamp() * 1000) if hasattr(ts, "timestamp") else int(ts)
        bars.append(
            [
                ts_ms,
                float(row["Open"]),
                float(row["High"]),
                float(row["Low"]),
                float(row["Close"]),
                float(row.get("Volume", 0.0) or 0.0),
            ]
        )
    return bars


class EquityYFAdapter(DataAdapter):
    """yfinance-backed adapter for US equities (Phase 4 MVP).

    yfinance is synchronous, so each fetch runs on a worker thread via
    `asyncio.to_thread` to keep the bot's event loop responsive.
    """

    name = "yfinance"

    async def fetch_ohlcv(
        self,
        ticker: str,
        timeframe: str = "1h",
        limit: int = 200,
        exchange: str | None = None,
    ) -> OHLCVBundle:
        if timeframe not in YF_INTERVAL_MAP:
            raise ValueError(f"Unsupported timeframe {timeframe!r} for yfinance adapter")

        df = await asyncio.to_thread(self._sync_fetch, ticker, timeframe)
        if df is None or df.empty:
            raise RuntimeError(f"yfinance returned no data for {ticker}")

        df = df.tail(limit)
        bars = _df_to_bars(df)
        if not bars:
            raise RuntimeError(f"yfinance returned only NaN bars for {ticker}")

        return OHLCVBundle(
            ticker=ticker.upper(),
            timeframe=timeframe,
            exchange=None,
            pulled_at=datetime.now(UTC),
            source_url=f"https://finance.yahoo.com/quote/{ticker.upper()}",
            bars=bars,
        )

    def _sync_fetch(self, ticker: str, timeframe: str) -> pd.DataFrame | None:
        period = YF_PERIOD_MAP[timeframe]
        interval = YF_INTERVAL_MAP[timeframe]
        try:
            return yf.Ticker(ticker).history(
                period=period,
                interval=interval,
                auto_adjust=True,
                actions=False,
            )
        except Exception as exc:
            logger.warning("yfinance fetch failed for %s: %s", ticker, exc)
            return None

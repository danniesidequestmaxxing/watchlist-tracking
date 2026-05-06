from abc import ABC, abstractmethod
from datetime import datetime

import pandas as pd
from pydantic import BaseModel, Field


class OHLCVBundle(BaseModel):
    """Raw OHLCV bars + provenance.

    `bars` follows the ccxt convention: each row is
    [timestamp_ms, open, high, low, close, volume].
    """

    ticker: str
    timeframe: str
    exchange: str | None = None
    pulled_at: datetime
    source_url: str
    bars: list[list[float]] = Field(default_factory=list)

    def to_dataframe(self) -> pd.DataFrame:
        df = pd.DataFrame(self.bars, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df = df.set_index("ts")
        return df


class DataAdapter(ABC):
    """Interface every market-data adapter implements."""

    name: str = "base"

    @abstractmethod
    async def fetch_ohlcv(
        self,
        ticker: str,
        timeframe: str = "1h",
        limit: int = 200,
        exchange: str | None = None,
    ) -> OHLCVBundle: ...

    async def close(self) -> None:
        return None

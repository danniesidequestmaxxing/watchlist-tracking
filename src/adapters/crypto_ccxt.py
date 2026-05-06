import logging
from datetime import UTC, datetime

import ccxt.async_support as ccxt

from src.adapters.base import DataAdapter, OHLCVBundle

logger = logging.getLogger(__name__)


SUPPORTED_EXCHANGES: tuple[str, ...] = ("binance", "okx", "hyperliquid")

EXCHANGE_URLS: dict[str, str] = {
    "binance": "https://www.binance.com",
    "okx": "https://www.okx.com",
    "hyperliquid": "https://app.hyperliquid.xyz",
}

CRYPTO_QUOTE_SUFFIXES: tuple[str, ...] = ("USDT", "USDC", "BUSD", "BTC", "ETH", "USD")


def to_ccxt_symbol(ticker: str) -> str:
    """Convert exchange-style ticker (BTCUSDT) to ccxt symbol (BTC/USDT).

    Falls back to the original ticker if no known quote suffix is recognized.
    """
    t = ticker.upper()
    for quote in CRYPTO_QUOTE_SUFFIXES:
        if t.endswith(quote) and len(t) > len(quote):
            return f"{t[: -len(quote)]}/{quote}"
    return t


class CryptoCCXTAdapter(DataAdapter):
    name = "ccxt"

    def __init__(self) -> None:
        self._clients: dict[str, ccxt.Exchange] = {}

    def _client(self, exchange: str) -> ccxt.Exchange:
        ex = exchange.lower()
        if ex not in SUPPORTED_EXCHANGES:
            raise ValueError(
                f"Unsupported exchange '{exchange}'. Use one of: {SUPPORTED_EXCHANGES}"
            )
        if ex not in self._clients:
            cls = getattr(ccxt, ex)
            self._clients[ex] = cls({"enableRateLimit": True})
        return self._clients[ex]

    async def fetch_ohlcv(
        self,
        ticker: str,
        timeframe: str = "1h",
        limit: int = 200,
        exchange: str | None = None,
    ) -> OHLCVBundle:
        if not exchange:
            raise ValueError("exchange is required for the crypto adapter")
        client = self._client(exchange)
        symbol = to_ccxt_symbol(ticker)
        bars = await client.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not bars:
            raise RuntimeError(f"No OHLCV bars returned for {symbol} on {exchange}")
        return OHLCVBundle(
            ticker=ticker.upper(),
            timeframe=timeframe,
            exchange=exchange.lower(),
            pulled_at=datetime.now(UTC),
            source_url=EXCHANGE_URLS.get(exchange.lower(), f"https://{exchange.lower()}.com"),
            bars=bars,
        )

    async def close(self) -> None:
        for client in self._clients.values():
            try:
                await client.close()
            except Exception as exc:
                logger.warning("Failed to close ccxt client: %s", exc)
        self._clients.clear()

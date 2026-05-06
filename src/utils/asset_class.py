import re

CRYPTO_QUOTE_SUFFIXES: tuple[str, ...] = ("USDT", "USDC", "BUSD", "BTC", "ETH", "USD")

BURSA_RE = re.compile(r"^\d{4,5}$")
SGX_RE = re.compile(r"^[A-Z]{1,5}\.SI$")
US_RE = re.compile(r"^[A-Z]{1,5}$")
FX_TAG_RE = re.compile(r"=X")

KNOWN_FX_PAIRS: frozenset[str] = frozenset(
    {
        "EURUSD",
        "USDJPY",
        "GBPUSD",
        "USDCHF",
        "AUDUSD",
        "NZDUSD",
        "USDCAD",
        "EURGBP",
        "EURJPY",
        "GBPJPY",
        "USDMYR",
        "USDSGD",
        "USDCNH",
        "USDHKD",
    }
)


def detect_asset_class(ticker: str | None) -> str | None:
    """Map a ticker symbol to one of the asset_class values, or None if ambiguous.

    Mirrors §5.2 of the spec. The known-FX-pair list is checked before the
    crypto-suffix rule so that fiat pairs ending in USD (e.g. EURUSD) classify
    correctly; the crypto-suffix rule still catches BTCUSD, ETHUSD, etc.
    """
    if not ticker:
        return None
    t = ticker.upper().strip()
    if not t:
        return None

    if FX_TAG_RE.search(t) or t in KNOWN_FX_PAIRS:
        return "fx"

    for suffix in CRYPTO_QUOTE_SUFFIXES:
        if t.endswith(suffix) and len(t) > len(suffix):
            return "crypto"

    if BURSA_RE.match(t):
        return "equity_my"

    if SGX_RE.match(t):
        return "equity_sg"

    if US_RE.match(t):
        return "equity_us"

    return None

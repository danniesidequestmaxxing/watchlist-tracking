import re

CRYPTO_QUOTE_SUFFIXES: tuple[str, ...] = ("USDT", "USDC", "BUSD", "BTC", "ETH", "USD")

BURSA_RE = re.compile(r"^\d{4,5}$")
SGX_RE = re.compile(r"^[A-Z]{1,5}\.SI$")
KRX_RE = re.compile(r"^\d{6}\.K[SQ]$")  # KOSPI .KS, KOSDAQ .KQ — e.g. 005930.KS (Samsung)
TSE_RE = re.compile(r"^\d{4}\.T$")  # Tokyo — e.g. 7203.T (Toyota)
TWSE_RE = re.compile(r"^\d{4}\.TWO?$")  # Taipei — e.g. 2330.TW (TSMC), .TWO for OTC
SSE_SZSE_RE = re.compile(r"^\d{6}\.S[SZ]$")  # Shanghai .SS / Shenzhen .SZ — e.g. 600519.SS
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
        "USDKRW",
        "USDTWD",
    }
)


def detect_asset_class(ticker: str | None) -> str | None:
    """Map a ticker symbol to one of the asset_class values, or None if ambiguous.

    Spec §5.2 covers crypto / equity_us / equity_my / equity_sg / fx. Asia
    extensions (KR / JP / TW / CN) require an explicit yfinance suffix on the
    ticker so that a 6-digit symbol isn't ambiguous between Korea's KOSPI
    (005930) and China's Shanghai exchange (600519).
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

    if KRX_RE.match(t):
        return "equity_kr"

    if TSE_RE.match(t):
        return "equity_jp"

    if TWSE_RE.match(t):
        return "equity_tw"

    if SSE_SZSE_RE.match(t):
        return "equity_cn"

    if US_RE.match(t):
        return "equity_us"

    return None

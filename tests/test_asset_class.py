import pytest

from src.utils.asset_class import detect_asset_class


@pytest.mark.parametrize(
    "ticker,expected",
    [
        # crypto pairs
        ("BTCUSDT", "crypto"),
        ("ETHUSDT", "crypto"),
        ("SOLUSDC", "crypto"),
        ("BTCBUSD", "crypto"),
        ("ETHBTC", "crypto"),
        ("SOLBTC", "crypto"),
        ("ETHUSD", "crypto"),
        # case folding
        ("btcusdt", "crypto"),
        ("  ethusdt  ", "crypto"),
        # Bursa Malaysia (4-5 digits)
        ("5347", "equity_my"),
        ("1234", "equity_my"),
        ("12345", "equity_my"),
        # SGX
        ("DBS.SI", "equity_sg"),
        ("ABC.SI", "equity_sg"),
        # FX
        ("EURUSD=X", "fx"),
        ("USDJPY=X", "fx"),
        ("EURUSD", "fx"),
        ("USDJPY", "fx"),
        ("USDMYR", "fx"),
        # US equities
        ("NVDA", "equity_us"),
        ("META", "equity_us"),
        ("AAPL", "equity_us"),
        ("F", "equity_us"),
        ("TSLA", "equity_us"),
        # ambiguous / unsupported
        ("", None),
        (None, None),
        ("   ", None),
        ("HELLO123", None),
        ("12.34", None),
        ("123", None),
        ("123456", None),
        ("9988.HK", None),
        ("D05.SI", None),  # SGX rule per spec is letters-only
        ("ABCDEF", None),  # 6 letters, exceeds US regex
    ],
)
def test_detect_asset_class(ticker: str | None, expected: str | None) -> None:
    assert detect_asset_class(ticker) == expected


def test_crypto_suffix_priority() -> None:
    """Suffix rule outranks the US equity letter rule."""
    assert detect_asset_class("ETHBTC") == "crypto"


def test_fx_known_pair_no_tag() -> None:
    """Known FX pairs classify as fx even without =X."""
    assert detect_asset_class("EURUSD") == "fx"
    assert detect_asset_class("USDJPY") == "fx"


def test_fx_tag_anywhere() -> None:
    assert detect_asset_class("XYZ=X") == "fx"
